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


def _iter_bot_layer_sources():
    """bot 層 (root の bot_discord.py + commands/) の .py を (相対パス, ソース) で返す。"""
    targets = [os.path.join(_PROJECT_ROOT, "bot_discord.py")]
    commands_dir = os.path.join(_PROJECT_ROOT, "commands")
    for name in sorted(os.listdir(commands_dir)):
        if name.endswith(".py"):
            targets.append(os.path.join(commands_dir, name))
    for path in targets:
        with open(path, encoding="utf-8") as f:
            yield os.path.relpath(path, _PROJECT_ROOT), f.read()


def _declares_persistent_timeout(class_node) -> bool:
    """クラス内に super().__init__(timeout=None) 相当の呼び出しがあるか。"""
    import ast
    for node in ast.walk(class_node):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "__init__"):
            continue
        for kw in node.keywords:
            if kw.arg == "timeout" and isinstance(kw.value, ast.Constant) and kw.value.value is None:
                return True
    return False


def test_persistent_views_are_registered():
    """timeout=None の View は必ず bot.add_view() で登録されていること。

    所見3 の回帰テスト (2026-07-26)。timeout=None は「Bot 再起動をまたいで生きる」
    という宣言だが、add_view() が無ければ再起動後は押しても無反応で、
    ユーザーにもログにも何も出ない (無言の死)。永続化できない View は
    有限 timeout にすること — この2択以外を許さないための静的スキャン。

    既知の限界:
      - 登録側は `bot.add_view(SomeView())` の形しか読めない。変数に入れてから
        渡す書き方に変えると未登録として落ちる (安全側の誤検知なので許容)。
      - 走査対象は bot_discord.py と commands/ のみ。View を他の階層に置くなら
        _iter_bot_layer_sources() に足すこと。
    """
    import ast
    import re

    with open(os.path.join(_PROJECT_ROOT, "bot_discord.py"), encoding="utf-8") as f:
        bot_src = f.read()
    registered = set(re.findall(r"add_view\(\s*(?:[\w.]+\.)?(\w+)\(", bot_src))
    assert registered, "bot_discord.py に add_view() の呼び出しが1つも見つかりません"

    violations = []
    checked = 0
    for rel_path, src in _iter_bot_layer_sources():
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = [
                b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "")
                for b in node.bases
            ]
            if not any(name.endswith("View") for name in base_names):
                continue
            if not _declares_persistent_timeout(node):
                continue
            checked += 1
            if node.name not in registered:
                violations.append(f"{rel_path}:{node.lineno} {node.name}")

    assert not violations, (
        "timeout=None なのに bot.add_view() されていない View があります "
        "(Bot 再起動でボタンが無言で死にます)。永続化するか有限 timeout にしてください:\n  "
        + "\n  ".join(violations)
    )
    assert checked >= 2, f"永続View の検出数が想定より少ない ({checked}) — スキャンが壊れている可能性"
    print(f"✅ persistent views (timeout=None) are all registered via add_view ({checked} views)")


def test_client_review_view_has_finite_timeout():
    """ClientReviewLoopView は instance 状態を持つので永続化できない = 有限 timeout。"""
    from commands.bot_commands_client import (
        VIEW_TIMEOUT_SECONDS, LOOP_GRACE_SECONDS, INTERACTION_TOKEN_LIFETIME,
    )

    assert isinstance(VIEW_TIMEOUT_SECONDS, (int, float)) and VIEW_TIMEOUT_SECONDS > 0
    assert LOOP_GRACE_SECONDS > 0, "改訂ループ中の期限延長が無いと処理中に on_timeout が発火する"
    assert VIEW_TIMEOUT_SECONDS < INTERACTION_TOKEN_LIFETIME, (
        "View の期限が interaction token の寿命(15分)以上だと、on_timeout の "
        "期限切れ通知(WebhookMessage.edit)がトークン失効で必ず失敗する"
    )

    async def _check():
        from commands.bot_commands_client import ClientReviewLoopView
        view = ClientReviewLoopView("text", "job", "other", {"status": "revision_needed"})
        assert view.timeout == VIEW_TIMEOUT_SECONDS, "timeout=None (嘘の永続) に戻っています"
        assert view.message is None, "on_timeout 用の message 属性が初期化されていません"

        # on_timeout: message 未設定でも例外を投げず、全ボタンを無効化すること
        await view.on_timeout()
        assert all(item.disabled for item in view.children), "期限切れ後もボタンが押せる状態です"

        # message があれば編集して理由を表示する
        view2 = ClientReviewLoopView("text", "job", "other", {"status": "revision_needed"})
        view2.message = AsyncMock()
        await view2.on_timeout()
        view2.message.edit.assert_awaited_once()
        _, kwargs = view2.message.edit.call_args
        assert "有効期限" in kwargs.get("content", ""), "期限切れの理由が表示されていません"

    asyncio.run(_check())
    print("✅ ClientReviewLoopView finite timeout + on_timeout disables buttons")


def test_client_review_loop_tracks_message_and_restores_timeout():
    """btn_loop 実行後に (1) 期限が通常値へ戻り (2) 最新メッセージを掴んでいること。

    どちらが欠けても on_timeout が「無い/古いメッセージ」を編集しに行って
    期限切れ通知が届かなくなる。Gemini 呼び出しは差し替えて課金ゼロで回す。
    """
    import commands.bot_commands_client as cc

    async def _check():
        sent = object()
        interaction = AsyncMock()
        interaction.followup.send.return_value = sent

        view = cc.ClientReviewLoopView("draft", "job", "other", {"status": "fail"})
        view.message = "初回メッセージ"

        orig = (cc.revise_deliverable, cc.evaluate_deliverable, cc._save_improvement_log)
        try:
            cc.revise_deliverable = lambda *a, **k: "改訂後テキスト"
            # status=fail 固定: pass にすると Vault へ実ファイルを書いてしまう
            cc.evaluate_deliverable = lambda *a, **k: {"status": "fail", "scores": {}}
            cc._save_improvement_log = lambda *a, **k: None

            await view.btn_loop.callback(interaction)
        finally:
            cc.revise_deliverable, cc.evaluate_deliverable, cc._save_improvement_log = orig

        assert view.press_count == 1
        assert view.timeout == cc.VIEW_TIMEOUT_SECONDS, (
            f"作業中の延長({cc.LOOP_GRACE_SECONDS}秒)が finally で戻っていません: {view.timeout}"
        )
        assert view.message is sent, "on_timeout の編集対象が最新メッセージに更新されていません"

    asyncio.run(_check())
    print("✅ btn_loop restores idle timeout and tracks the latest message")


if __name__ == "__main__":
    test_custom_id_consistency()
    test_button_row_structure()
    test_load_top_features_freshness_gate()
    test_allowed_user_view_check()
    test_scheduler_log_tail_multibyte_safe()
    test_persistent_views_are_registered()
    test_client_review_view_has_finite_timeout()
    test_client_review_loop_tracks_message_and_restores_timeout()
    print("\n🎉 ALL BUTTON/VIEW REGRESSION TESTS PASSED")
