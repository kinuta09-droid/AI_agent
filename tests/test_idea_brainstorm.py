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
    # cmd_idea は tool_save_idea (00_inbox 保存) を呼ぶ。入力の改行そのものはファイル名生成に
    # 使わなくなった (フェーズ2で tool_save_idea 側に移設) が、実I/Oを一切させないことを保証する。
    captured = {}

    def _fake_save_idea(text):
        captured["text"] = text
        return "Success: ok"

    orig = idea_mod.tool_save_idea
    try:
        idea_mod.tool_save_idea = _fake_save_idea
        interaction = _make_fake_interaction(1)
        await idea_mod.cmd_idea(interaction, "改行\nを含む\nアイデア")
    finally:
        idea_mod.tool_save_idea = orig

    assert captured.get("text") == "改行\nを含む\nアイデア", f"tool_save_idea に渡す text が壊れている: {captured!r}"


def test_cmd_idea_sanitizes_newlines_in_filename():
    asyncio.run(_run_cmd_idea_sanitizes_newlines_in_filename())
    print("✅ [4] cmd_idea: 改行を含む入力でも実I/Oを一切せずtool_save_ideaに委譲する")


async def _run_cmd_idea_icon_reflects_failure():
    orig = idea_mod.tool_save_idea
    try:
        idea_mod.tool_save_idea = lambda *a, **k: "Error saving idea: disk full"
        interaction = _make_fake_interaction(1)
        await idea_mod.cmd_idea(interaction, "test")
    finally:
        idea_mod.tool_save_idea = orig

    sent = interaction.followup.send.call_args[0][0]
    assert sent.startswith("❌"), f"失敗時に✅が出ている (ハードコード✅禁止規約違反): {sent}"


def test_cmd_idea_icon_reflects_failure():
    asyncio.run(_run_cmd_idea_icon_reflects_failure())
    print("✅ [5] cmd_idea: tool_save_idea失敗時は❌ (✅ハードコード禁止規約)")


async def _run_idea_autocomplete_filters_and_prefixes():
    orig = idea_mod.tool_list_open_ideas
    try:
        idea_mod.tool_list_open_ideas = lambda *a, **k: [
            {"title": "音声作品", "mtime": 2}, {"title": "催眠もの", "mtime": 1}
        ]
        choices = await idea_mod.idea_autocomplete(_make_fake_interaction(1), "音声")
    finally:
        idea_mod.tool_list_open_ideas = orig

    assert len(choices) == 1, f"部分一致フィルタが効いていない: {choices}"
    assert choices[0].value == "idea:音声作品", choices[0].value


def test_idea_autocomplete_filters_and_prefixes():
    asyncio.run(_run_idea_autocomplete_filters_and_prefixes())
    print("✅ [6] idea_autocomplete: 部分一致でフィルタし idea: prefixを付ける")


async def _run_cmd_brainstorm_loads_idea_seed():
    interaction = _make_fake_interaction(1)
    thread = AsyncMock()
    thread.id = 90010
    interaction.channel.create_thread = AsyncMock(return_value=thread)

    orig_read, orig_gen = idea_mod.tool_read_note, idea_mod.generate_next_question
    idea_mod.tool_read_note = lambda title: "元ノートの本文"
    idea_mod.generate_next_question = lambda seed, turns: "Q1"
    try:
        await idea_mod.cmd_brainstorm(interaction, "idea:音声作品")
        session = idea_mod._brainstorm_sessions.get(thread.id)
        assert session is not None, "セッションが作られていない"
        assert session["seed"] == "元ノートの本文", f"seedがtool_read_noteの結果でない: {session['seed']!r}"
        assert session["source_title"] == "音声作品", session["source_title"]
    finally:
        idea_mod.tool_read_note = orig_read
        idea_mod.generate_next_question = orig_gen
        idea_mod._brainstorm_sessions.pop(thread.id, None)


def test_cmd_brainstorm_loads_idea_seed():
    asyncio.run(_run_cmd_brainstorm_loads_idea_seed())
    print("✅ [7] cmd_brainstorm: idea:prefixで元Fleeting Noteを読み込みseedにする")


async def _run_summarize_and_save_links_and_deletes_source():
    session = {"seed": "seed", "turns": [], "source_title": "音声作品"}
    thread = AsyncMock()
    created, deleted = {}, {}

    orig_summarize, orig_create, orig_delete, orig_find_related = (
        idea_mod.summarize_idea_session, idea_mod.tool_create_note, idea_mod.tool_delete_idea,
        idea_mod.tool_find_related_notes,
    )
    try:
        idea_mod.summarize_idea_session = lambda seed, turns: {"title": "T", "summary": "S", "content": "本文"}

        def _fake_create(filename, content, summary=""):
            created["content"] = content
            return "Success: saved"

        idea_mod.tool_create_note = _fake_create

        def _fake_delete(title):
            deleted["title"] = title
            return "Success: deleted"

        idea_mod.tool_delete_idea = _fake_delete
        # 関連ノート通知が実Vault検索を引き起こさないよう空候補にスタブする
        # (候補0件ならthread.sendは呼ばれない設計。この関数の主目的の検証を汚さない)
        idea_mod.tool_find_related_notes = lambda *a, **k: []
        result = await idea_mod._summarize_and_save(session, thread)
    finally:
        idea_mod.summarize_idea_session, idea_mod.tool_create_note, idea_mod.tool_delete_idea = (
            orig_summarize, orig_create, orig_delete
        )
        idea_mod.tool_find_related_notes = orig_find_related

    assert result == "Success: saved", result
    assert "> Source: [[音声作品]]" in created.get("content", ""), created
    assert deleted.get("title") == "音声作品", f"元アイデアが削除呼び出しされていない: {deleted}"
    assert thread.send.await_count == 0, "候補0件なのに関連ノート通知が送られた"


def test_summarize_and_save_links_and_deletes_source():
    asyncio.run(_run_summarize_and_save_links_and_deletes_source())
    print("✅ [8] _summarize_and_save: Sourceリンクを足し保存成功後に元アイデアを削除する")


async def _run_summarize_and_save_delete_failure_does_not_fail_result():
    session = {"seed": "seed", "turns": [], "source_title": "音声作品"}
    thread = AsyncMock()
    orig_summarize, orig_create, orig_delete, orig_find_related = (
        idea_mod.summarize_idea_session, idea_mod.tool_create_note, idea_mod.tool_delete_idea,
        idea_mod.tool_find_related_notes,
    )
    try:
        idea_mod.summarize_idea_session = lambda seed, turns: {"title": "T", "summary": "S", "content": "本文"}
        idea_mod.tool_create_note = lambda *a, **k: "Success: saved"
        idea_mod.tool_delete_idea = lambda title: "Error: not found"
        idea_mod.tool_find_related_notes = lambda *a, **k: []
        result = await idea_mod._summarize_and_save(session, thread)
    finally:
        idea_mod.summarize_idea_session, idea_mod.tool_create_note, idea_mod.tool_delete_idea = (
            orig_summarize, orig_create, orig_delete
        )
        idea_mod.tool_find_related_notes = orig_find_related
    assert result == "Success: saved", f"削除失敗が要約の成功結果に影響してしまった: {result}"


def test_summarize_and_save_delete_failure_does_not_fail_result():
    asyncio.run(_run_summarize_and_save_delete_failure_does_not_fail_result())
    print("✅ [9] _summarize_and_save: 元アイデア削除の失敗は要約の成功結果に影響しない")


async def _run_summarize_and_save_notifies_related_notes():
    session = {"seed": "seed", "turns": [], "source_title": None}
    thread = AsyncMock()
    orig_summarize, orig_create, orig_delete, orig_find_related = (
        idea_mod.summarize_idea_session, idea_mod.tool_create_note, idea_mod.tool_delete_idea,
        idea_mod.tool_find_related_notes,
    )
    try:
        idea_mod.summarize_idea_session = lambda seed, turns: {"title": "T", "summary": "S", "content": "本文"}
        idea_mod.tool_create_note = lambda *a, **k: "Success: Created [[T]] with template."
        idea_mod.tool_delete_idea = lambda title: "Success: deleted"
        idea_mod.tool_find_related_notes = lambda *a, **k: [
            {"filename": "既存ノート.md", "rel_path": "30_Main/既存ノート.md"}
        ]
        result = await idea_mod._summarize_and_save(session, thread)
    finally:
        idea_mod.summarize_idea_session, idea_mod.tool_create_note, idea_mod.tool_delete_idea = (
            orig_summarize, orig_create, orig_delete
        )
        idea_mod.tool_find_related_notes = orig_find_related

    assert result == "Success: Created [[T]] with template.", result
    assert thread.send.await_count == 1, f"候補ありなのに関連ノート通知が送られていない: {thread.send.await_count}"
    sent_body = thread.send.call_args[0][0]
    assert "[[既存ノート]]" in sent_body, sent_body


def test_summarize_and_save_notifies_related_notes():
    asyncio.run(_run_summarize_and_save_notifies_related_notes())
    print("✅ [10] _summarize_and_save: 保存成功後、候補があれば関連ノート通知をthreadに送る")


async def _run_summarize_and_save_notify_exception_does_not_fail_result():
    session = {"seed": "seed", "turns": [], "source_title": None}
    thread = AsyncMock()
    orig_summarize, orig_create, orig_delete, orig_find_related = (
        idea_mod.summarize_idea_session, idea_mod.tool_create_note, idea_mod.tool_delete_idea,
        idea_mod.tool_find_related_notes,
    )

    def _boom(*a, **k):
        raise RuntimeError("vault検索が壊れた")

    try:
        idea_mod.summarize_idea_session = lambda seed, turns: {"title": "T", "summary": "S", "content": "本文"}
        idea_mod.tool_create_note = lambda *a, **k: "Success: Created [[T]] with template."
        idea_mod.tool_delete_idea = lambda title: "Success: deleted"
        idea_mod.tool_find_related_notes = _boom
        result = await idea_mod._summarize_and_save(session, thread)
    finally:
        idea_mod.summarize_idea_session, idea_mod.tool_create_note, idea_mod.tool_delete_idea = (
            orig_summarize, orig_create, orig_delete
        )
        idea_mod.tool_find_related_notes = orig_find_related

    assert result == "Success: Created [[T]] with template.", (
        f"関連ノート通知の例外が要約の成功結果に影響してしまった: {result}"
    )


def test_summarize_and_save_notify_exception_does_not_fail_result():
    asyncio.run(_run_summarize_and_save_notify_exception_does_not_fail_result())
    print("✅ [11] _summarize_and_save: 関連ノート通知が例外を出しても戻り値は保存成功のまま")


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
