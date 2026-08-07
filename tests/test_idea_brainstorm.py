# -*- coding: utf-8 -*-
"""
commands/bot_commands_idea.py のセッション競合まわりの回帰テスト (grill 指摘分)

対象:
  - WrapUpView.wrap_up の連打で summarize/save が二重に走らないこと
    (busy フラグを defer() より前に立てる修正の回帰)
  - WrapUpView.on_timeout が busy 中のセッションを pop しないこと
    (取りこぼし防止の on_timeout が進行中のセッションを宙に浮かせるバグの回帰)
  - /idea のファイル名生成が改行を含む入力でも壊れないこと
  - /idea の成否アイコンが is_failure_result に連動すること (✅ハードコード禁止規約)

方針: Gemini / Vault への実I/Oはすべてスタブ差し替え。interaction は
tests/test_button_views.py と同じ AsyncMock ベースの偽物を使う。

実行: py tests/test_idea_brainstorm.py
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import commands.bot_commands_idea as idea_mod


def _make_fake_interaction(user_id: int):
    interaction = AsyncMock()
    interaction.user.id = user_id
    return interaction


def _make_session(user_id=1, busy=False):
    return {
        "user_id": user_id, "seed": "テストアイデア", "turns": [],
        "pending_question": "Q1", "busy": busy,
    }


async def _run_double_click_does_not_double_process():
    thread = AsyncMock()
    thread.id = 90001
    idea_mod._brainstorm_sessions[thread.id] = _make_session()

    calls = {"n": 0}

    async def _slow_defer():
        await asyncio.sleep(0.05)

    def _slow_summarize(seed, turns):
        calls["n"] += 1
        return {"title": "T", "summary": "S", "content": "C"}

    orig_summarize, orig_create = idea_mod.summarize_idea_session, idea_mod.tool_create_note
    try:
        idea_mod.summarize_idea_session = _slow_summarize
        idea_mod.tool_create_note = lambda *a, **k: "Success: saved"

        view = idea_mod.WrapUpView(thread)
        i1, i2 = _make_fake_interaction(1), _make_fake_interaction(1)
        i1.response.defer.side_effect = _slow_defer
        i2.response.defer.side_effect = _slow_defer

        await asyncio.gather(view.wrap_up.callback(i1), view.wrap_up.callback(i2))
    finally:
        idea_mod.summarize_idea_session, idea_mod.tool_create_note = orig_summarize, orig_create
        idea_mod._brainstorm_sessions.pop(thread.id, None)

    assert calls["n"] == 1, f"busyガードが効かず二重に要約処理が走った (n={calls['n']})"


def test_double_click_does_not_double_process():
    asyncio.run(_run_double_click_does_not_double_process())
    print("✅ [1] WrapUpView.wrap_up: 連打してもsummarize_idea_sessionは1回だけ")


async def _run_on_timeout_skips_when_busy():
    thread = AsyncMock()
    thread.id = 90002
    session = _make_session(busy=True)
    idea_mod._brainstorm_sessions[thread.id] = session
    try:
        view = idea_mod.WrapUpView(thread)
        await view.on_timeout()
        assert thread.id in idea_mod._brainstorm_sessions, "busy中にon_timeoutがセッションを消してしまった"
        assert idea_mod._brainstorm_sessions[thread.id] is session
    finally:
        idea_mod._brainstorm_sessions.pop(thread.id, None)


def test_on_timeout_skips_when_busy():
    asyncio.run(_run_on_timeout_skips_when_busy())
    print("✅ [2] WrapUpView.on_timeout: busy中はセッションをpopしない (会話が宙に浮かない)")


async def _run_on_timeout_summarizes_when_idle():
    thread = AsyncMock()
    thread.id = 90003
    idea_mod._brainstorm_sessions[thread.id] = _make_session(busy=False)

    orig_summarize, orig_create = idea_mod.summarize_idea_session, idea_mod.tool_create_note
    try:
        idea_mod.summarize_idea_session = lambda seed, turns: {"title": "T", "summary": "S", "content": "C"}
        idea_mod.tool_create_note = lambda *a, **k: "Success: saved"

        view = idea_mod.WrapUpView(thread)
        view.message = AsyncMock()
        await view.on_timeout()
    finally:
        idea_mod.summarize_idea_session, idea_mod.tool_create_note = orig_summarize, orig_create
        idea_mod._brainstorm_sessions.pop(thread.id, None)

    assert thread.send.await_count == 1, "期限切れ時に自動まとめメッセージが送られていない"
    assert "まとめました" in str(thread.send.call_args)


def test_on_timeout_summarizes_when_idle():
    asyncio.run(_run_on_timeout_summarizes_when_idle())
    print("✅ [3] WrapUpView.on_timeout: busyでなければ自動要約して通知する (取りこぼし防止)")


async def _run_cmd_idea_sanitizes_newlines_in_filename():
    captured = {}

    def _fake_create_note(filename, content, summary=""):
        captured["filename"] = filename
        return "Success: ok"

    orig = idea_mod.tool_create_note
    try:
        idea_mod.tool_create_note = _fake_create_note
        interaction = _make_fake_interaction(1)
        await idea_mod.cmd_idea(interaction, "改行\nを含む\nアイデア")
    finally:
        idea_mod.tool_create_note = orig

    assert "\n" not in captured["filename"], f"ファイル名に改行が残っている: {captured['filename']!r}"


def test_cmd_idea_sanitizes_newlines_in_filename():
    asyncio.run(_run_cmd_idea_sanitizes_newlines_in_filename())
    print("✅ [4] cmd_idea: 改行を含む入力でもファイル名が壊れない")


async def _run_cmd_idea_icon_reflects_failure():
    orig = idea_mod.tool_create_note
    try:
        idea_mod.tool_create_note = lambda *a, **k: "Error creating note: disk full"
        interaction = _make_fake_interaction(1)
        await idea_mod.cmd_idea(interaction, "test")
    finally:
        idea_mod.tool_create_note = orig

    sent = interaction.followup.send.call_args[0][0]
    assert sent.startswith("❌"), f"失敗時に✅が出ている (ハードコード✅禁止規約違反): {sent}"


def test_cmd_idea_icon_reflects_failure():
    asyncio.run(_run_cmd_idea_icon_reflects_failure())
    print("✅ [5] cmd_idea: tool_create_note失敗時は❌ (✅ハードコード禁止規約)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
