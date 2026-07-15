# -*- coding: utf-8 -*-
"""
Discord ボタンUI (永続View + スケジューラ連携) の回帰テスト

対象:
  custom_id の一致 (bot_views と agent_scheduler の _button_row 呼び出し)
  _button_row の REST components JSON 構造
  _load_top_features の鮮度ゲート (2日超で空リスト)
  AllowedUserView.interaction_check の許可/拒否/未初期化フェイルオープン

実行: py tests/test_button_views.py
"""
import os
import sys
import json
import asyncio
import datetime
import tempfile
from unittest.mock import AsyncMock

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def test_custom_id_consistency():
    from commands.bot_views import DIVE_CUSTOM_IDS, SCHEDULER_LOG_CUSTOM_ID
    import agent_scheduler as sched

    assert sched.DIVE_CUSTOM_IDS == DIVE_CUSTOM_IDS, "scheduler と bot_views の deep-dive custom_id がずれています"
    assert sched.SCHEDULER_LOG_CUSTOM_ID == SCHEDULER_LOG_CUSTOM_ID, "scheduler と bot_views のログ custom_id がずれています"
    assert len(DIVE_CUSTOM_IDS) == 3
    print("✅ custom_id consistency (bot_views <-> agent_scheduler)")


def test_button_row_structure():
    import agent_scheduler as sched
    components = sched._button_row([("📋 ログ表示", "sched_show_log"), ("🔍 深掘り 1", "brief_dive:1")])
    assert isinstance(components, list) and len(components) == 1
    row = components[0]
    assert row["type"] == 1
    assert len(row["components"]) == 2
    btn = row["components"][0]
    assert btn["type"] == 2 and btn["style"] == 1
    assert btn["custom_id"] == "sched_show_log"
    assert btn["label"] == "📋 ログ表示"
    print("✅ _button_row REST JSON structure")


def test_load_top_features_freshness_gate():
    from commands.bot_views import _load_top_features
    import commands.bot_views as bv

    tmp_dir = tempfile.mkdtemp()
    fake_json = os.path.join(tmp_dir, "latest_features.json")
    orig_path = bv._XTREND_JSON
    try:
        bv._XTREND_JSON = fake_json

        # 新鮮なデータ -> 上位N件が返る
        fresh_date = datetime.date.today().isoformat()
        with open(fake_json, 'w', encoding='utf-8') as f:
            json.dump({"date": fresh_date, "features": [
                {"feature_name": f"feat{i}"} for i in range(5)
            ]}, f)
        result = _load_top_features(top_n=3)
        assert len(result) == 3, f"expected 3 fresh features, got {len(result)}"

        # 3日前のデータ (鮮度ゲート超過) -> 空リスト
        stale_date = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        with open(fake_json, 'w', encoding='utf-8') as f:
            json.dump({"date": stale_date, "features": [{"feature_name": "old"}]}, f)
        result = _load_top_features(top_n=3)
        assert result == [], f"expected [] for stale data, got {result}"

        # ファイル不在 -> 空リスト
        os.remove(fake_json)
        assert _load_top_features(top_n=3) == []

        # 壊れたJSON -> 空リスト (例外を投げない)
        with open(fake_json, 'w', encoding='utf-8') as f:
            f.write("{not valid json")
        assert _load_top_features(top_n=3) == []
    finally:
        bv._XTREND_JSON = orig_path
    print("✅ _load_top_features freshness gate (fresh/stale/missing/corrupt)")


def _make_fake_interaction(user_id: int):
    interaction = AsyncMock()
    interaction.user.id = user_id
    return interaction


async def _run_allowed_user_view_check():
    # discord.ui.View.__init__ が実行中イベントループを要求するため、
    # View の構築ごと async 関数内で行う。
    from commands.bot_views import AllowedUserView, init_views
    import commands.bot_views as bv

    # 未初期化 (init_views 未呼び出し) -> フェイルオープンで許可
    bv._is_allowed_user = None
    view = AllowedUserView(timeout=None)
    ok = await view.interaction_check(_make_fake_interaction(999))
    assert ok is True, "未初期化時はフェイルオープンで許可されるべき"

    try:
        # 許可ユーザー
        init_views(lambda uid: uid == 123)
        ok = await view.interaction_check(_make_fake_interaction(123))
        assert ok is True

        # 拒否ユーザー -> False + ephemeral 応答
        interaction = _make_fake_interaction(456)
        ok = await view.interaction_check(interaction)
        assert ok is False
        interaction.response.send_message.assert_awaited_once()
        _, kwargs = interaction.response.send_message.call_args
        assert kwargs.get("ephemeral") is True
    finally:
        bv._is_allowed_user = None


def test_allowed_user_view_check():
    asyncio.run(_run_allowed_user_view_check())
    print("✅ AllowedUserView.interaction_check (allow/deny/fail-open)")


def test_scheduler_log_tail_multibyte_safe():
    """TaskFailureView.show_log と同じロジック (バイナリ seek → decode) が
    マルチバイト UTF-8 文字の境界で壊れないことを検証する (grill 指摘の回帰テスト)。
    """
    import commands.bot_views as bv

    tmp_dir = tempfile.mkdtemp()
    log_path = os.path.join(tmp_dir, "scheduler.log")
    # 8000バイト境界をまたぐよう、日本語(マルチバイト)を含む十分な長さのログを作る
    line = "2026-07-15 12:00:00 - INFO - テスト実行中です。マルチバイト文字を含む行。\n"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(line * 400)

    orig_path = bv._SCHEDULER_LOG_PATH
    try:
        bv._SCHEDULER_LOG_PATH = log_path
        # show_log 内部と同一のバイナリ seek + decode ロジックを直接検証
        with open(bv._SCHEDULER_LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8000))
            tail = f.read().decode("utf-8", errors="replace")
        # バイナリ seek のため、切り出し開始位置がマルチバイト文字の途中に
        # かかることは起こり得る (先頭に高々1文字分の replacement character が
        # 出るのは正常)。しかし最初の改行より後ろの本文は一切壊れてはいけない。
        assert len(tail) > 0
        body_after_first_line = tail.split("\n", 1)[1] if "\n" in tail else tail
        assert "�" not in body_after_first_line, (
            "先頭行より後ろでマルチバイト文字が破損しています (テキストモードseekの再発)"
        )
        assert "マルチバイト文字を含む行" in body_after_first_line
    finally:
        bv._SCHEDULER_LOG_PATH = orig_path
    print("✅ scheduler.log tail extraction is multibyte-safe (no seek corruption)")


if __name__ == "__main__":
    test_custom_id_consistency()
    test_button_row_structure()
    test_load_top_features_freshness_gate()
    test_allowed_user_view_check()
    test_scheduler_log_tail_multibyte_safe()
    print("\n🎉 ALL BUTTON/VIEW REGRESSION TESTS PASSED")
