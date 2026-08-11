# -*- coding: utf-8 -*-
"""
/agent, /chat agent への 👍/👎 フィードバックボタン (lib/agent_feedback.py +
commands/bot_views.py の AgentFeedbackView/AgentFeedbackReasonModal) の回帰テスト。

対象:
  1. save_feedback() が JSONL に1行追記し、期待する構造 (event/item/source/
     item_type/meta) を持つこと。log_path を DI し、本番 logs/agent_feedback.jsonl
     には一切触れないこと (CLAUDE.md: テストが本番と共有する可変資源を汚染しない)。
  2. AgentFeedbackView の timeout が有限 (3600) であり None でないこと
     (test_client_review_view_has_finite_timeout に倣う)。
  3. 👍ボタン (good) が save_feedback を正しい引数で呼び、全ボタンを disabled にすること。
  4. 👎ボタン (bad) が AgentFeedbackReasonModal を send_modal すること。
  5. AgentFeedbackReasonModal.on_submit が save_feedback("bad", ..., reason=...) を
     正しい reason で呼び、親 View のボタンを disabled にすること (reason 空文字も含む)。
  6. [静的検査] /agent (cmd_agent) と /chat agent (_dispatch_agent) の両方の入口が
     send_feedback_prompt を呼んでいること。CLAUDE.md の既知パターン
     (「/agent だけ直して /chat agent を取り残した」) と同型の再発を防ぐため、
     動的テストでは片方の呼び出し側しか守れない箇所を ast で静的に横断する。
  7. 🕐「最新情報で再検索」ボタン (recency_retry, plans/recency-detection-reliability.md)。
     tool_web_search_grounded(self._goal) を呼び、👍/👎とは独立にこのボタンだけ
     disabled になり (_finish() は呼ばない)、失敗時は ❌ アイコンになること。

実行: py tests/test_agent_feedback.py
"""
import os
import sys
import ast
import json
import asyncio
import tempfile
from unittest.mock import AsyncMock

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.agent_feedback import save_feedback, summarize_feedback, AGENT_FEEDBACK_LOG_PATH


def _make_fake_interaction(user_id: int = 111, channel_id: int = 222):
    interaction = AsyncMock()
    interaction.user.id = user_id
    interaction.channel_id = channel_id
    return interaction


# ---------------------------------------------------------
# 1. save_feedback() の JSONL 書き込み
# ---------------------------------------------------------

def test_save_feedback_writes_expected_jsonl_entry():
    tmp_dir = tempfile.mkdtemp()
    log_path = os.path.join(tmp_dir, "agent_feedback.jsonl")
    assert log_path != AGENT_FEEDBACK_LOG_PATH, "本番ログパスと衝突しています"
    assert not os.path.exists(AGENT_FEEDBACK_LOG_PATH) or True  # 存在有無は問わないが書き込みはしない

    before_exists = os.path.exists(AGENT_FEEDBACK_LOG_PATH)
    before_size = os.path.getsize(AGENT_FEEDBACK_LOG_PATH) if before_exists else None

    ok = save_feedback(
        "good", "テスト用ゴール", "テスト用結果", reason="",
        user_id=123, channel_id=456, log_path=log_path,
    )
    assert ok is True

    assert os.path.exists(log_path)
    with open(log_path, encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    assert len(lines) == 1, f"期待は1行、実際は{len(lines)}行"

    entry = json.loads(lines[0])
    assert entry["event"] == "good"
    assert entry["item"] == "テスト用ゴール"
    assert entry["source"] == "agent_feedback"
    assert entry["item_type"] == "agent_answer"
    assert entry["meta"] == {
        "result": "テスト用結果", "reason": "", "user_id": 123, "channel_id": 456,
    }

    # 本番ログファイルには一切触れていないこと
    after_exists = os.path.exists(AGENT_FEEDBACK_LOG_PATH)
    if before_exists:
        assert os.path.getsize(AGENT_FEEDBACK_LOG_PATH) == before_size, (
            "本番 logs/agent_feedback.jsonl が書き換えられています"
        )
    else:
        assert not after_exists, "本番 logs/agent_feedback.jsonl が新規作成されています"

    print("✅ save_feedback() writes expected JSONL entry (isolated log_path, prod untouched)")


def test_save_feedback_bad_rating_includes_reason():
    tmp_dir = tempfile.mkdtemp()
    log_path = os.path.join(tmp_dir, "agent_feedback.jsonl")

    save_feedback(
        "bad", "ゴール2", "結果2", reason="期待と違いました",
        user_id=1, channel_id=2, log_path=log_path,
    )
    with open(log_path, encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["event"] == "bad"
    assert entry["meta"]["reason"] == "期待と違いました"
    print("✅ save_feedback() bad rating carries the reason text through meta")


# ---------------------------------------------------------
# 1b. summarize_feedback() の集計
# ---------------------------------------------------------

def test_summarize_feedback_empty_log():
    tmp_dir = tempfile.mkdtemp()
    log_path = os.path.join(tmp_dir, "agent_feedback.jsonl")
    text = summarize_feedback(log_path=log_path)
    assert "エントリがありません" in text
    print("✅ summarize_feedback() handles an empty log without raising")


def test_summarize_feedback_counts_ratio_reasons_and_users():
    tmp_dir = tempfile.mkdtemp()
    log_path = os.path.join(tmp_dir, "agent_feedback.jsonl")

    save_feedback("good", "ゴール1", "結果1", user_id=1, log_path=log_path)
    save_feedback("good", "ゴール2", "結果2", user_id=1, log_path=log_path)
    save_feedback("bad", "ゴール3", "結果3", reason="情報が古い", user_id=2, log_path=log_path)
    save_feedback("bad", "ゴール4", "結果4", reason="情報が古い", user_id=2, log_path=log_path)
    save_feedback("bad", "ゴール5", "結果5", reason="", user_id=3, log_path=log_path)

    text = summarize_feedback(days=30, log_path=log_path)
    assert "good: 2 (40%)" in text, text
    assert "bad: 3 (60%)" in text, text
    assert "(2) 情報が古い" in text, text
    assert "(1) (理由未記入)" in text, text
    assert "- 1: 2" in text, text
    assert "- 2: 2" in text, text
    assert "- 3: 1" in text, text
    print("✅ summarize_feedback() reports good/bad ratio, bad-reason frequency, and per-user counts")


def test_summarize_feedback_whitespace_only_reason_counts_as_unfilled():
    """空白のみのreasonは「(理由未記入)」に丸め、空白だけの別バケツを作らないこと (/grill 指摘)。"""
    tmp_dir = tempfile.mkdtemp()
    log_path = os.path.join(tmp_dir, "agent_feedback.jsonl")

    save_feedback("bad", "ゴール1", "結果1", reason="   ", user_id=1, log_path=log_path)
    save_feedback("bad", "ゴール2", "結果2", reason="", user_id=2, log_path=log_path)

    text = summarize_feedback(log_path=log_path)
    assert "(2) (理由未記入)" in text, text
    assert '"   "' not in text
    print("✅ summarize_feedback() folds whitespace-only reasons into (理由未記入)")


def test_summarize_feedback_prod_log_untouched():
    before_exists = os.path.exists(AGENT_FEEDBACK_LOG_PATH)
    before_size = os.path.getsize(AGENT_FEEDBACK_LOG_PATH) if before_exists else None

    tmp_dir = tempfile.mkdtemp()
    log_path = os.path.join(tmp_dir, "agent_feedback.jsonl")
    save_feedback("good", "g", "r", log_path=log_path)
    summarize_feedback(log_path=log_path)

    after_exists = os.path.exists(AGENT_FEEDBACK_LOG_PATH)
    if before_exists:
        assert os.path.getsize(AGENT_FEEDBACK_LOG_PATH) == before_size
    else:
        assert not after_exists
    print("✅ summarize_feedback() never touches the production log")


# ---------------------------------------------------------
# 2. AgentFeedbackView の timeout
# ---------------------------------------------------------

def test_agent_feedback_view_has_finite_timeout():
    from commands.bot_views import AgentFeedbackView, _FEEDBACK_VIEW_TIMEOUT

    async def _check():
        view = AgentFeedbackView("goal", "result")
        assert view.timeout == _FEEDBACK_VIEW_TIMEOUT
        assert view.timeout is not None, "timeout=None (永続化) は instance 状態を持つ View には不可"
        assert view.timeout == 3600
        assert view.message is None

    asyncio.run(_check())
    print("✅ AgentFeedbackView has a finite timeout (3600s), not None")


# ---------------------------------------------------------
# 3. 👍ボタン (good)
# ---------------------------------------------------------

def test_agent_feedback_good_button_saves_and_disables():
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("見て欲しいゴール", "こういう結果でした")
        interaction = _make_fake_interaction(user_id=42, channel_id=99)

        calls = []
        orig = None
        import lib.agent_feedback as af
        orig = af.save_feedback

        def fake_save_feedback(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        af.save_feedback = fake_save_feedback
        try:
            await view.good.callback(interaction)
        finally:
            af.save_feedback = orig

        assert len(calls) == 1, f"save_feedback が {len(calls)} 回呼ばれました (期待 1)"
        args, kwargs = calls[0]
        assert args[0] == "good"
        assert args[1] == "見て欲しいゴール"
        assert args[2] == "こういう結果でした"
        assert kwargs.get("user_id") == 42
        assert kwargs.get("channel_id") == 99

        assert all(item.disabled for item in view.children), "評価後もボタンが押せる状態です"
        interaction.response.send_message.assert_awaited_once()
        _, send_kwargs = interaction.response.send_message.call_args
        assert send_kwargs.get("ephemeral") is True

    asyncio.run(_check())
    print("✅ AgentFeedbackView.good calls save_feedback('good', ...) and disables all buttons")


def test_agent_feedback_good_button_warns_on_save_failure():
    """save_feedback が False (保存失敗) を返したら、成功メッセージを出してはいけない。

    例外を投げない失敗を「例外が出なかった=成功」と決めつけて ✅ を出す食い違いは
    CLAUDE.md の Known Bug Patterns で繰り返し潰してきた形 (/grill 指摘・2026-08-03)。
    """
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("ゴールF", "結果F")
        interaction = _make_fake_interaction()

        import lib.agent_feedback as af
        orig = af.save_feedback
        af.save_feedback = lambda *a, **kw: False
        try:
            await view.good.callback(interaction)
        finally:
            af.save_feedback = orig

        interaction.response.send_message.assert_awaited_once()
        args, _ = interaction.response.send_message.call_args
        assert "失敗" in args[0], f"保存失敗時に成功文言のままです: {args[0]!r}"
        assert "ありがとうございます" not in args[0]

    asyncio.run(_check())
    print("✅ AgentFeedbackView.good shows a failure message when save_feedback returns False")


# ---------------------------------------------------------
# 4. 👎ボタン (bad) -> モーダル表示
# ---------------------------------------------------------

def test_agent_feedback_bad_button_opens_reason_modal():
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("ゴールX", "結果X")
        interaction = _make_fake_interaction()

        await view.bad.callback(interaction)

        interaction.response.send_modal.assert_awaited_once()
        args, _ = interaction.response.send_modal.call_args
        modal = args[0]
        assert isinstance(modal, bv.AgentFeedbackReasonModal)
        assert modal._goal == "ゴールX"
        assert modal._result == "結果X"
        assert modal._parent_view is view

    asyncio.run(_check())
    print("✅ AgentFeedbackView.bad opens AgentFeedbackReasonModal (no direct save_feedback call)")


# ---------------------------------------------------------
# 5. AgentFeedbackReasonModal.on_submit
# ---------------------------------------------------------

def test_agent_feedback_reason_modal_on_submit_with_reason():
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("ゴールY", "結果Y")
        modal = bv.AgentFeedbackReasonModal("ゴールY", "結果Y", view)
        modal.reason._value = "もっと具体的にしてほしかった"

        interaction = _make_fake_interaction(user_id=7, channel_id=8)

        calls = []
        import lib.agent_feedback as af
        orig = af.save_feedback

        def fake_save_feedback(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        af.save_feedback = fake_save_feedback
        try:
            await modal.on_submit(interaction)
        finally:
            af.save_feedback = orig

        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] == "bad"
        assert args[1] == "ゴールY"
        assert args[2] == "結果Y"
        assert kwargs.get("reason") == "もっと具体的にしてほしかった"
        assert kwargs.get("user_id") == 7
        assert kwargs.get("channel_id") == 8

        assert all(item.disabled for item in view.children), "モーダル送信後も親Viewのボタンが押せる状態です"
        interaction.response.send_message.assert_awaited_once()

    asyncio.run(_check())
    print("✅ AgentFeedbackReasonModal.on_submit saves reason and disables parent view buttons")


def test_agent_feedback_reason_modal_on_submit_empty_reason():
    """理由未入力 (任意項目) でも例外を投げず、空文字のまま記録されること。"""
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("ゴールZ", "結果Z")
        modal = bv.AgentFeedbackReasonModal("ゴールZ", "結果Z", view)
        # reason.value は TextInput 未入力時デフォルトで空文字 ("")

        interaction = _make_fake_interaction()

        calls = []
        import lib.agent_feedback as af
        orig = af.save_feedback

        def fake_save_feedback(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        af.save_feedback = fake_save_feedback
        try:
            await modal.on_submit(interaction)
        finally:
            af.save_feedback = orig

        assert len(calls) == 1
        _, kwargs = calls[0]
        assert kwargs.get("reason") == "", f"未入力時は空文字を期待: {kwargs.get('reason')!r}"

    asyncio.run(_check())
    print("✅ AgentFeedbackReasonModal.on_submit tolerates an empty (optional) reason")


def test_agent_feedback_reason_modal_warns_on_save_failure():
    """モーダル送信時も save_feedback が False を返したら失敗を伝えること (good と同型)。"""
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("ゴールG", "結果G")
        modal = bv.AgentFeedbackReasonModal("ゴールG", "結果G", view)
        interaction = _make_fake_interaction()

        import lib.agent_feedback as af
        orig = af.save_feedback
        af.save_feedback = lambda *a, **kw: False
        try:
            await modal.on_submit(interaction)
        finally:
            af.save_feedback = orig

        interaction.response.send_message.assert_awaited_once()
        args, _ = interaction.response.send_message.call_args
        assert "失敗" in args[0], f"保存失敗時に成功文言のままです: {args[0]!r}"

    asyncio.run(_check())
    print("✅ AgentFeedbackReasonModal.on_submit shows a failure message when save_feedback returns False")


# ---------------------------------------------------------
# 6b. 🕐「最新情報で再検索」ボタン (recency_retry)
# ---------------------------------------------------------

def test_recency_retry_button_calls_grounded_search_and_disables_only_itself():
    """クリック後、recency_retry ボタンだけ disabled になり 👍👎 は押せるままであること。

    _finish() を呼ぶと 👍👎 も道連れで disabled になってしまう。「評価」とは別の
    操作なので独立に無効化する設計 (plans/recency-detection-reliability.md)。
    """
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("今日のガソリン価格が急に下がった理由", "旧い回答でした")
        interaction = _make_fake_interaction()

        import tools.tool_web as tw
        orig = tw.tool_web_search_grounded
        calls = []

        def fake_grounded(query):
            calls.append(query)
            return "本文...\n参照元: ..."

        tw.tool_web_search_grounded = fake_grounded
        try:
            await view.recency_retry.callback(interaction)
        finally:
            tw.tool_web_search_grounded = orig

        assert calls == ["今日のガソリン価格が急に下がった理由"], (
            f"tool_web_search_grounded(self._goal) が期待通り呼ばれていません: {calls}"
        )

        good_btn = next(i for i in view.children if getattr(i, "label", "") == "👍 良い回答")
        bad_btn = next(i for i in view.children if getattr(i, "label", "") == "👎 期待と違う")
        retry_btn = next(i for i in view.children if i.label == "🕐 最新情報で再検索")
        assert retry_btn.disabled is True, "クリック後もボタンが押せる状態です"
        assert good_btn.disabled is False, "👍 が巻き添えで disabled になっています"
        assert bad_btn.disabled is False, "👎 が巻き添えで disabled になっています"

        interaction.response.edit_message.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        (msg,), _ = interaction.followup.send.call_args
        assert msg.startswith("🕐"), f"成功時のアイコンが🕐ではありません: {msg[:20]!r}"
        assert "本文..." in msg

    asyncio.run(_check())
    print("✅ AgentFeedbackView.recency_retry calls tool_web_search_grounded(self._goal) "
          "and disables only itself")


def test_recency_retry_button_shows_failure_icon_on_error():
    """tool_web_search_grounded が Error: ... を返したとき ❌ アイコンになること。

    「例外が出なかった＝成功」と決めつけない、という既存パターンをこのボタンにも適用する。
    """
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("今日の急な出来事", "旧い回答")
        interaction = _make_fake_interaction()

        import tools.tool_web as tw
        orig = tw.tool_web_search_grounded
        tw.tool_web_search_grounded = lambda query: "Error: grounding call failed"
        try:
            await view.recency_retry.callback(interaction)
        finally:
            tw.tool_web_search_grounded = orig

        interaction.followup.send.assert_awaited_once()
        (msg,), _ = interaction.followup.send.call_args
        assert msg.startswith("❌"), f"失敗時のアイコンが❌ではありません: {msg[:20]!r}"

    asyncio.run(_check())
    print("✅ AgentFeedbackView.recency_retry shows ❌ when tool_web_search_grounded fails")


def test_recency_retry_button_truncates_long_result():
    """Discordのメッセージ本文は2000字上限。切り詰めずに送ると followup.send が
    HTTPException で無言に落ちる (/grill 指摘・2026-08-04)。長い結果 (本文+複数の
    引用元URLを想定) でも例外にならず、本文が上限内に収まることを確認する。
    """
    import commands.bot_views as bv

    async def _check():
        view = bv.AgentFeedbackView("今日のニュースまとめ", "旧い回答")
        interaction = _make_fake_interaction()

        long_result = "本文段落。" * 500 + "\n参照元: " + " / ".join(
            f"https://example.com/article-{i}" for i in range(5)
        )
        assert len(long_result) > 2000, "テスト前提: 元の結果が2000字を超えていること"

        import tools.tool_web as tw
        orig = tw.tool_web_search_grounded
        tw.tool_web_search_grounded = lambda query: long_result
        try:
            await view.recency_retry.callback(interaction)
        finally:
            tw.tool_web_search_grounded = orig

        interaction.followup.send.assert_awaited_once()
        (msg,), _ = interaction.followup.send.call_args
        assert len(msg) <= 2000, f"送信メッセージがDiscordの上限を超えています: {len(msg)}字"

    asyncio.run(_check())
    print("✅ AgentFeedbackView.recency_retry truncates long results to Discord's 2000-char limit")


# ---------------------------------------------------------
# 6. [静的検査] /agent と /chat agent の両方が send_feedback_prompt を呼ぶこと
# ---------------------------------------------------------

def _calls_send_feedback_prompt(func_node) -> bool:
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "send_feedback_prompt":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "send_feedback_prompt":
            return True
    return False


def _find_func(tree, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_both_agent_entry_points_call_send_feedback_prompt():
    """/agent (cmd_agent) と /chat agent (_dispatch_agent) の両方が
    send_feedback_prompt を呼んでいることを ast で静的に確認する。

    CLAUDE.md 既知バグパターン「/agent だけ直して /chat agent を取り残した」と
    同じ形の再発 (新しい呼び出し側で同じ実装漏れをする) を、動的テストではなく
    呼び出し元コードそのものを走査して防ぐ。
    """
    targets = {
        os.path.join(_PROJECT_ROOT, "commands", "bot_commands_agent.py"): "cmd_agent",
        os.path.join(_PROJECT_ROOT, "commands", "bot_commands_chat.py"): "_dispatch_agent",
    }

    missing = []
    checked = 0
    for path, func_name in targets.items():
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        func_node = _find_func(tree, func_name)
        assert func_node is not None, f"{path} に関数 {func_name} が見つかりません (関数名が変わった?)"
        checked += 1
        if not _calls_send_feedback_prompt(func_node):
            missing.append(f"{os.path.relpath(path, _PROJECT_ROOT)}::{func_name}")

    assert checked == 2
    assert not missing, (
        "send_feedback_prompt を呼んでいない /agent 系の入口があります "
        "(片方だけフィードバックボタンが出ない食い違いになります):\n  " + "\n  ".join(missing)
    )
    print("✅ both /agent (cmd_agent) and /chat agent (_dispatch_agent) call send_feedback_prompt")


# ---------------------------------------------------------
# 7. send_feedback_prompt(channel=None) は例外を投げず何もしない
# ---------------------------------------------------------

def test_send_feedback_prompt_skips_when_channel_is_none():
    """interaction.channel がキャッシュ切れ等で None のとき、AttributeError を伝播させない。

    bot_views.BriefingDeepDiveView に既にある `if channel is None: return` と同じ先例に
    倣う (/grill 指摘・2026-08-03)。
    """
    from commands.bot_commands_agent import send_feedback_prompt

    asyncio.run(send_feedback_prompt(None, "ゴール", "結果"))
    print("✅ send_feedback_prompt(channel=None) returns without raising")


if __name__ == "__main__":
    test_save_feedback_writes_expected_jsonl_entry()
    test_save_feedback_bad_rating_includes_reason()
    test_summarize_feedback_empty_log()
    test_summarize_feedback_counts_ratio_reasons_and_users()
    test_summarize_feedback_whitespace_only_reason_counts_as_unfilled()
    test_summarize_feedback_prod_log_untouched()
    test_agent_feedback_view_has_finite_timeout()
    test_agent_feedback_good_button_saves_and_disables()
    test_agent_feedback_good_button_warns_on_save_failure()
    test_agent_feedback_bad_button_opens_reason_modal()
    test_agent_feedback_reason_modal_on_submit_with_reason()
    test_agent_feedback_reason_modal_on_submit_empty_reason()
    test_agent_feedback_reason_modal_warns_on_save_failure()
    test_recency_retry_button_calls_grounded_search_and_disables_only_itself()
    test_recency_retry_button_shows_failure_icon_on_error()
    test_recency_retry_button_truncates_long_result()
    test_both_agent_entry_points_call_send_feedback_prompt()
    test_send_feedback_prompt_skips_when_channel_is_none()
    print("\n🎉 ALL AGENT FEEDBACK REGRESSION TESTS PASSED")
