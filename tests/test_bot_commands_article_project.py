# -*- coding: utf-8 -*-
"""
commands/bot_commands_article_project.py の回帰テスト (indexed-snacking-crab plan §4)。

対象:
  - handle_project_reply: セッション不在/本人以外は無視、本人の回答が保存成功後にのみ
    current_indexを進め、Q&Aが毎回管理ノートへ永続化される (record_qa_answer)
  - Q&A保存が失敗したら current_index を進めず、回答も記録しない (再送信で再試行できる)
  - 記事生成中(generating)の追加返信は質問配列に触れずガードされる
  - send_next_question: 全質問終了で記事生成→推敲→投稿→管理ノート保存→完成ボタン付与
  - 記事保存(_save_final_article)が失敗したらセッションを削除せず pending_save_article を
    保持し、次の返信で再試行する。手動編集競合(backup_filename)時は専用メッセージを出す
  - スレッド単位のロックにより、ほぼ同時に届いた複数の返信でも記事生成が二重に走らない
  - AdoptedSourcesView.confirm: 個人情報の絶対除外・採用資料保存失敗時の再試行導線
  - restore_sessions: 管理ノートからセッションを復元し、1件の失敗が他を止めない
  - 自然文修正フロー(_handle_revision_reply): draft_doneでのみ反映、completedは拒否
  - ArticleProjectCompleteView: 完成ボタンの冪等性、共有busyフラグを使わないため
    別スレッドの操作が待たされないこと

discord.pyの実オブジェクトは使わずAsyncMockで完結させる。外部呼び出しはモジュール属性の
差し替えでモックし、実Gemini APIは一切叩かない (ARMS_LIVE_TESTSゲート対象外)。

実行: py tests/test_bot_commands_article_project.py
"""
import os
import sys
import asyncio
from unittest.mock import AsyncMock

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import commands.bot_commands_article_project as bap  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _make_fake_interaction(user_id: int):
    interaction = AsyncMock()
    interaction.user.id = user_id
    return interaction


def _make_session(**overrides) -> dict:
    session = {
        "user_id": 1, "filename": "f.md", "goal": "g", "audience": "",
        "reference_material": "", "qa_pairs": [{"point": "p", "question": "q"}],
        "current_index": 0, "generating": False, "pending_save_article": None,
    }
    session.update(overrides)
    return session


def _make_message(thread_id: int, user_id: int, content: str) -> AsyncMock:
    message = AsyncMock()
    message.author.bot = False
    message.author.id = user_id
    message.channel.id = thread_id
    message.content = content
    message.channel.send = AsyncMock()
    return message


def _reset_state():
    bap._project_sessions.clear()
    bap._completed_projects.clear()
    bap._thread_locks.clear()


async def _run_ignores_when_no_session():
    _reset_state()
    message = _make_message(999999, 1, "何か")
    await bap.handle_project_reply(message)  # 例外を投げず戻ることだけ確認


def test_handle_project_reply_ignores_when_no_session():
    asyncio.run(_run_ignores_when_no_session())
    check("handle_project_reply ignores missing session", True)


async def _run_ignores_other_user():
    _reset_state()
    bap._project_sessions[111] = _make_session(user_id=1)
    message = _make_message(111, 2, "無関係な回答")  # 本人ではない
    await bap.handle_project_reply(message)


def test_handle_project_reply_ignores_other_user():
    asyncio.run(_run_ignores_other_user())
    check(
        "handle_project_reply ignores other user's message",
        bap._project_sessions[111]["current_index"] == 0,
    )


async def _run_records_answer_saves_and_advances():
    _reset_state()
    bap._project_sessions[222] = _make_session(
        qa_pairs=[
            {"point": "p1", "question": "q1"},
            {"point": "p2", "question": "q2"},
        ],
        current_index=0,
    )
    message = _make_message(222, 1, "回答その1")

    saved_calls = []

    def _fake_record_qa_answer(filename, qa_pairs, question, answer):
        # asyncio.to_thread に渡される前提の同期関数(record_qa_answer自身も同期)
        saved_calls.append((filename, question, answer))
        return True

    orig_record = bap.record_qa_answer
    orig_send_next = bap.send_next_question
    try:
        bap.record_qa_answer = _fake_record_qa_answer
        bap.send_next_question = AsyncMock()  # 質問送信自体は別テストの対象
        await bap.handle_project_reply(message)
    finally:
        bap.record_qa_answer = orig_record
        bap.send_next_question = orig_send_next

    return bap._project_sessions.get(222), saved_calls


def test_handle_project_reply_records_answer_saves_and_advances():
    session, saved_calls = asyncio.run(_run_records_answer_saves_and_advances())
    check("answer recorded", session["qa_pairs"][0].get("answer") == "回答その1")
    check("current_index advanced", session["current_index"] == 1)
    check("Q&A save attempted exactly once", len(saved_calls) == 1, saved_calls)
    if saved_calls:
        check("save called with question and answer", saved_calls[0][1] == "q1" and saved_calls[0][2] == "回答その1")


async def _run_qa_save_failure_does_not_advance():
    """レビュー指摘3の回帰: Q&A保存が失敗したら current_index を進めず、回答も記録しない。"""
    _reset_state()
    bap._project_sessions[223] = _make_session(
        qa_pairs=[{"point": "p1", "question": "q1"}], current_index=0,
    )
    message = _make_message(223, 1, "保存に失敗する回答")

    orig_record = bap.record_qa_answer
    orig_send_next = bap.send_next_question
    try:
        bap.record_qa_answer = lambda filename, qa_pairs, question, answer: False
        bap.send_next_question = AsyncMock()
        await bap.handle_project_reply(message)
    finally:
        bap.record_qa_answer = orig_record
        bap.send_next_question = orig_send_next

    return bap._project_sessions.get(223), message


def test_qa_save_failure_does_not_advance_or_call_next_question():
    session, message = asyncio.run(_run_qa_save_failure_does_not_advance())
    check("current_index untouched on save failure", session["current_index"] == 0)
    check("answer reverted to None on save failure", session["qa_pairs"][0].get("answer") is None)
    check("user asked to resend", message.channel.send.await_count == 1)


async def _run_reply_guarded_while_generating():
    _reset_state()
    bap._project_sessions[444] = _make_session(current_index=1, generating=True)
    message = _make_message(444, 1, "生成中に届いた余計な返信")

    await bap.handle_project_reply(message)
    return bap._project_sessions.get(444), message


def test_reply_guarded_while_generating():
    session, message = asyncio.run(_run_reply_guarded_while_generating())
    check("current_index untouched while generating", session["current_index"] == 1)
    check("user notified generation in progress", message.channel.send.await_count == 1)


async def _run_reply_guarded_when_index_out_of_range():
    _reset_state()
    bap._project_sessions[555] = _make_session(
        qa_pairs=[{"point": "p", "question": "q"}], current_index=1, generating=False,
    )
    message = _make_message(555, 1, "隙間に届いた返信")

    await bap.handle_project_reply(message)  # IndexError を投げないことが本体の検証


def test_reply_guarded_when_index_out_of_range():
    asyncio.run(_run_reply_guarded_when_index_out_of_range())
    check("no IndexError when current_index is out of range", True)


async def _run_send_next_question_completes_and_saves_draft():
    _reset_state()
    thread_id = 333
    bap._project_sessions[thread_id] = _make_session(
        filename="proj.md", goal="目標G", audience="初心者向け", reference_material="参考資料",
        qa_pairs=[{"point": "p1", "question": "q1", "answer": "a1"}], current_index=1,
    )
    thread = AsyncMock()
    thread.id = thread_id

    calls = {}

    def _fake_generate_final_article(draft_text, qa_pairs, reference_material="", audience=""):
        calls["generate_args"] = (draft_text, qa_pairs, reference_material, audience)
        return "# 初稿"

    def _fake_reflection_loop(draft_article, draft_text, qa_pairs, max_loops=2, reference_material="", audience=""):
        calls["reflection_args"] = (draft_article, draft_text, qa_pairs, max_loops, reference_material, audience)
        return "# 完成記事"

    async def _fake_save_final_article(filename, final_article):
        calls["save_args"] = (filename, final_article)
        return True, None

    orig_gen = bap.generate_final_article
    orig_reflect = bap.run_article_reflection_loop
    orig_save = bap._save_final_article
    try:
        bap.generate_final_article = _fake_generate_final_article
        bap.run_article_reflection_loop = _fake_reflection_loop
        bap._save_final_article = _fake_save_final_article
        await bap.send_next_question(thread)
    finally:
        bap.generate_final_article = orig_gen
        bap.run_article_reflection_loop = orig_reflect
        bap._save_final_article = orig_save

    return calls, thread_id


def test_send_next_question_completes_and_saves_draft():
    calls, thread_id = asyncio.run(_run_send_next_question_completes_and_saves_draft())
    check("generate_final_article draft_text = goal", calls["generate_args"][0] == "目標G")
    check("generate_final_article received audience", calls["generate_args"][3] == "初心者向け")
    check("reflection_loop received reference_material", calls["reflection_args"][4] == "参考資料")
    check("reflection_loop received audience", calls["reflection_args"][5] == "初心者向け")
    check("save called with final article", calls["save_args"][1] == "# 完成記事")
    check("session cleaned up after completion", thread_id not in bap._project_sessions)
    check("thread lock cleaned up after completion", thread_id not in bap._thread_locks)
    check("project routed to completed cache", bap._completed_projects.get(thread_id) == "proj.md")


async def _run_send_next_question_keeps_session_when_save_fails():
    _reset_state()
    thread_id = 666
    bap._project_sessions[thread_id] = _make_session(
        filename="proj.md", goal="目標G",
        qa_pairs=[{"point": "p1", "question": "q1", "answer": "a1"}], current_index=1,
    )
    thread = AsyncMock()
    thread.id = thread_id

    orig_gen = bap.generate_final_article
    orig_reflect = bap.run_article_reflection_loop
    orig_save = bap._save_final_article
    try:
        bap.generate_final_article = lambda *a, **k: "# 初稿"
        bap.run_article_reflection_loop = lambda *a, **k: "# 完成記事"

        async def _failing_save(filename, article):
            return False, None
        bap._save_final_article = _failing_save

        await bap.send_next_question(thread)
    finally:
        bap.generate_final_article = orig_gen
        bap.run_article_reflection_loop = orig_reflect
        bap._save_final_article = orig_save

    return bap._project_sessions.get(thread_id)


def test_send_next_question_keeps_session_when_save_fails():
    session = asyncio.run(_run_send_next_question_keeps_session_when_save_fails())
    check("session preserved when save fails", session is not None)
    if session:
        check("pending_save_article holds the finished article", session["pending_save_article"] == "# 完成記事")
        check("generating flag cleared for retry", session["generating"] is False)


async def _run_send_next_question_reports_conflict_backup():
    """手動編集検知: save_final_draftが競合を検知した場合、backup_filenameを含む
    専用メッセージを出し、pending_save_articleで再試行に備える。"""
    _reset_state()
    thread_id = 999
    bap._project_sessions[thread_id] = _make_session(
        filename="proj.md", goal="目標G",
        qa_pairs=[{"point": "p1", "question": "q1", "answer": "a1"}], current_index=1,
    )
    thread = AsyncMock()
    thread.id = thread_id

    orig_gen = bap.generate_final_article
    orig_reflect = bap.run_article_reflection_loop
    orig_save = bap._save_final_article
    try:
        bap.generate_final_article = lambda *a, **k: "# 初稿"
        bap.run_article_reflection_loop = lambda *a, **k: "# 完成記事"

        async def _conflicting_save(filename, article):
            return False, "proj_Draft_conflict.md"
        bap._save_final_article = _conflicting_save

        await bap.send_next_question(thread)
    finally:
        bap.generate_final_article = orig_gen
        bap.run_article_reflection_loop = orig_reflect
        bap._save_final_article = orig_save

    return bap._project_sessions.get(thread_id), thread


def test_send_next_question_reports_conflict_backup():
    session, thread = asyncio.run(_run_send_next_question_reports_conflict_backup())
    check("session preserved on conflict", session is not None)
    sent_texts = [c.args[0] for c in thread.send.await_args_list if c.args]
    check("backup filename mentioned to user", any("proj_Draft_conflict.md" in t for t in sent_texts), sent_texts)


async def _run_pending_save_retry_succeeds():
    _reset_state()
    thread_id = 777
    bap._project_sessions[thread_id] = _make_session(
        filename="proj.md", pending_save_article="# 完成記事",
    )
    message = _make_message(thread_id, 1, "もう一度試して")

    orig_save = bap._save_final_article
    try:
        async def _succeeding_save(filename, article):
            return True, None
        bap._save_final_article = _succeeding_save
        await bap.handle_project_reply(message)
    finally:
        bap._save_final_article = orig_save

    return message


def test_pending_save_retry_succeeds_and_cleans_up_session():
    message = asyncio.run(_run_pending_save_retry_succeeds())
    check("session removed after successful retry", 777 not in bap._project_sessions)
    check("user notified of success", message.channel.send.await_count == 1)
    check("project routed to completed cache after retry", bap._completed_projects.get(777) == "proj.md")


async def _run_concurrent_replies_do_not_double_trigger_generation():
    """レビュー指摘4の回帰: Q&A保存の待機中にもう一つ返信が届いても、
    スレッド単位のロックにより記事生成(generate_final_article)は1回しか走らない。"""
    _reset_state()
    thread_id = 888
    bap._project_sessions[thread_id] = _make_session(
        filename="proj.md", goal="目標G",
        qa_pairs=[{"point": "p1", "question": "q1"}], current_index=0,
    )

    generate_calls = []

    def _slow_record_qa_answer(filename, qa_pairs, question, answer):
        import time
        time.sleep(0.05)  # to_thread経由なので同期sleepでよい(保存中に2件目が割り込む余地)
        return True

    def _fake_generate(*a, **k):
        generate_calls.append(1)
        return "# 初稿"

    message1 = _make_message(thread_id, 1, "回答A")
    message2 = _make_message(thread_id, 1, "回答B(ほぼ同時に届いた2通目)")

    orig_record = bap.record_qa_answer
    orig_gen = bap.generate_final_article
    orig_reflect = bap.run_article_reflection_loop
    orig_save_final = bap._save_final_article
    try:
        bap.record_qa_answer = _slow_record_qa_answer
        bap.generate_final_article = _fake_generate
        bap.run_article_reflection_loop = lambda *a, **k: "# 完成記事"
        bap._save_final_article = AsyncMock(return_value=(True, None))

        await asyncio.gather(
            bap.handle_project_reply(message1),
            bap.handle_project_reply(message2),
        )
    finally:
        bap.record_qa_answer = orig_record
        bap.generate_final_article = orig_gen
        bap.run_article_reflection_loop = orig_reflect
        bap._save_final_article = orig_save_final

    return generate_calls


def test_concurrent_replies_do_not_double_trigger_generation():
    generate_calls = asyncio.run(_run_concurrent_replies_do_not_double_trigger_generation())
    check("generation triggered exactly once despite concurrent replies", len(generate_calls) == 1, generate_calls)


async def _run_confirm_excludes_unremovable_personal_info():
    view = bap.AdoptedSourcesView(
        "proj.md", "目標G", user_id=1, candidate_filenames=["User_Profile.md", "Clean.md"],
        audience="初心者向け",
    )
    view.message = AsyncMock()

    interaction = _make_fake_interaction(1)
    interaction.channel = AsyncMock()

    captured = {}

    async def _fake_start_qa_phase(thread, filename, goal, user_id, reference_material, audience=""):
        captured["reference_material"] = reference_material
        captured["audience"] = audience

    orig_sanitize = bap.sanitize_reference_material
    orig_save_adopted = bap.save_adopted_sources
    orig_start = bap._start_qa_phase
    try:
        # sanitize_reference_material 自体は tool_article_project 側のテストで
        # 別途検証済み。ここでは「除去できなかったファイルが reference_material に
        # 混入せず、呼び出し元が正しく除外扱いすること」を確認する。
        bap.sanitize_reference_material = lambda filenames: ("Clean.mdの本文のみ", ["User_Profile.md"])
        bap.save_adopted_sources = lambda filename, adopted, log_text: True
        bap._start_qa_phase = _fake_start_qa_phase
        await view.confirm.callback(interaction)
    finally:
        bap.sanitize_reference_material = orig_sanitize
        bap.save_adopted_sources = orig_save_adopted
        bap._start_qa_phase = orig_start

    return captured, interaction


def test_adopted_sources_view_confirm_excludes_unremovable_personal_info():
    captured, interaction = asyncio.run(_run_confirm_excludes_unremovable_personal_info())
    check(
        "excluded file's content absent from reference_material",
        "User_Profile" not in captured.get("reference_material", ""),
        captured,
    )
    check("clean file content preserved", captured.get("reference_material") == "Clean.mdの本文のみ")
    check("audience forwarded to qa phase", captured.get("audience") == "初心者向け")
    sent_texts = [c.args[0] for c in interaction.channel.send.await_args_list if c.args]
    check(
        "user notified which file was excluded",
        any("User_Profile" in t for t in sent_texts),
        sent_texts,
    )


async def _run_confirm_blocks_qa_phase_when_save_adopted_sources_fails():
    view = bap.AdoptedSourcesView(
        "proj.md", "目標G", user_id=1, candidate_filenames=["Clean.md"], audience="",
    )
    view.message = AsyncMock()

    interaction = _make_fake_interaction(1)
    interaction.channel = AsyncMock()

    started = {"called": False}

    async def _fake_start_qa_phase(*a, **k):
        started["called"] = True

    orig_sanitize = bap.sanitize_reference_material
    orig_save_adopted = bap.save_adopted_sources
    orig_start = bap._start_qa_phase
    try:
        bap.sanitize_reference_material = lambda filenames: ("Clean.mdの本文", [])
        bap.save_adopted_sources = lambda filename, adopted, log_text: False
        bap._start_qa_phase = _fake_start_qa_phase
        await view.confirm.callback(interaction)
    finally:
        bap.sanitize_reference_material = orig_sanitize
        bap.save_adopted_sources = orig_save_adopted
        bap._start_qa_phase = orig_start

    return started, interaction, view


def test_confirm_blocks_qa_phase_and_keeps_button_active_when_save_adopted_sources_fails():
    started, interaction, view = asyncio.run(_run_confirm_blocks_qa_phase_when_save_adopted_sources_fails())
    check("qa phase NOT started when adoption save fails", started["called"] is False)
    check("view not disabled/stopped, button can be pressed again", not any(item.disabled for item in view.children))
    sent_texts = [c.args[0] for c in interaction.channel.send.await_args_list if c.args]
    check("user asked to press confirm again", any("もう一度" in t for t in sent_texts), sent_texts)


# ---------------------------------------------------------
# restore_sessions — Bot再起動後の復元 (要件④)
# ---------------------------------------------------------

class _FakeBotClient:
    """get_channel/fetch_channel を差し替え可能にする最小のBotクライアントスタブ。"""

    def __init__(self, channels: dict):
        self._channels = channels  # thread_id -> AsyncMock(thread) or Exception instance
        self.get_channel_calls = []
        self.fetch_channel_calls = []

    def get_channel(self, thread_id):
        self.get_channel_calls.append(thread_id)
        return None  # 常にキャッシュ未充填として fetch_channel へフォールバックさせる

    async def fetch_channel(self, thread_id):
        self.fetch_channel_calls.append(thread_id)
        result = self._channels.get(thread_id)
        if isinstance(result, Exception):
            raise result
        return result


async def _run_restore_sessions_rebuilds_qa_state():
    _reset_state()
    thread = AsyncMock()
    thread.id = 1001
    bot_client = _FakeBotClient({1001: thread})

    projects = [
        {
            "filename": "proj_qa.md",
            "frontmatter": {
                "thread_id": 1001, "status": "qa", "user_id": 42,
                "goal": "目標X", "audience": "初心者向け",
                "adopted_source_files": [],
                "qa_pairs": [
                    {"point": "p1", "question": "q1", "answer": "a1"},
                    {"point": "p2", "question": "q2", "answer": None},
                ],
            },
        },
    ]

    orig_list = bap.list_projects
    orig_sanitize = bap.sanitize_reference_material
    orig_send_next = bap.send_next_question
    try:
        bap.list_projects = lambda statuses=None: projects
        bap.sanitize_reference_material = lambda filenames: ("", [])
        bap.send_next_question = AsyncMock()
        await bap.restore_sessions(bot_client)
    finally:
        bap.list_projects = orig_list
        bap.sanitize_reference_material = orig_sanitize
        bap.send_next_question = orig_send_next

    return bap._project_sessions.get(1001), thread


def test_restore_sessions_rebuilds_qa_state_and_resumes():
    session, thread = asyncio.run(_run_restore_sessions_rebuilds_qa_state())
    check("session rebuilt for qa status", session is not None)
    if session:
        check("current_index derived from first unanswered qa", session["current_index"] == 1)
        check("goal restored", session["goal"] == "目標X")
        check("user_id restored", session["user_id"] == 42)
    check("thread notified of restoration", thread.send.await_count >= 1)


async def _run_restore_sessions_skips_goal_set_without_resuming():
    _reset_state()
    thread = AsyncMock()
    thread.id = 1002
    bot_client = _FakeBotClient({1002: thread})

    projects = [
        {"filename": "proj_goal.md", "frontmatter": {"thread_id": 1002, "status": "goal_set", "user_id": 1}},
    ]
    orig_list = bap.list_projects
    try:
        bap.list_projects = lambda statuses=None: projects
        await bap.restore_sessions(bot_client)
    finally:
        bap.list_projects = orig_list

    return thread


def test_restore_sessions_does_not_resume_goal_set():
    thread = asyncio.run(_run_restore_sessions_skips_goal_set_without_resuming())
    check("goal_set project not added to sessions", 1002 not in bap._project_sessions)
    sent_texts = [c.args[0] for c in thread.send.await_args_list if c.args]
    check("user told to restart via /article-project", any("/article-project" in t for t in sent_texts), sent_texts)


async def _run_restore_sessions_registers_completed_without_notifying():
    _reset_state()
    thread = AsyncMock()
    thread.id = 1003
    bot_client = _FakeBotClient({1003: thread})

    projects = [
        {"filename": "proj_done.md", "frontmatter": {"thread_id": 1003, "status": "draft_done", "user_id": 1}},
    ]
    orig_list = bap.list_projects
    try:
        bap.list_projects = lambda statuses=None: projects
        await bap.restore_sessions(bot_client)
    finally:
        bap.list_projects = orig_list

    return thread


def test_restore_sessions_registers_completed_projects_silently():
    thread = asyncio.run(_run_restore_sessions_registers_completed_without_notifying())
    check("draft_done project routed to completed cache", bap._completed_projects.get(1003) == "proj_done.md")
    check("no notification spam sent for already-completed projects", thread.send.await_count == 0)


async def _run_restore_sessions_one_failure_does_not_block_others():
    """1件のfetch_channel失敗が他プロジェクトの復元を止めないこと。"""
    _reset_state()
    good_thread = AsyncMock()
    good_thread.id = 2002
    bot_client = _FakeBotClient({2001: RuntimeError("channel gone"), 2002: good_thread})

    projects = [
        {"filename": "proj_bad.md", "frontmatter": {"thread_id": 2001, "status": "draft_done", "user_id": 1}},
        {"filename": "proj_good.md", "frontmatter": {"thread_id": 2002, "status": "draft_done", "user_id": 1}},
    ]
    orig_list = bap.list_projects
    try:
        bap.list_projects = lambda statuses=None: projects
        await bap.restore_sessions(bot_client)
    finally:
        bap.list_projects = orig_list


def test_restore_sessions_one_failure_does_not_block_others():
    asyncio.run(_run_restore_sessions_one_failure_does_not_block_others())
    check("failed project not registered", 2001 not in bap._completed_projects)
    check("subsequent project still restored", bap._completed_projects.get(2002) == "proj_good.md")


# ---------------------------------------------------------
# 自然文修正フロー (_handle_revision_reply) — 要件④
# ---------------------------------------------------------

async def _run_revision_reply_applies_on_draft_done():
    _reset_state()
    bap._completed_projects[3001] = "proj_r.md"
    message = _make_message(3001, 1, "もっと具体的な数値を入れて")

    fake_project = {
        "frontmatter": {
            "status": "draft_done", "user_id": 1, "audience": "初心者向け",
            "adopted_source_files": [],
        },
    }
    calls = {}

    orig_load = bap.load_project
    orig_load_draft = bap.load_draft_article
    orig_sanitize = bap.sanitize_reference_material
    orig_apply = bap.apply_user_revision
    orig_save = bap._save_final_article
    try:
        bap.load_project = lambda filename: fake_project
        bap.load_draft_article = lambda filename: "# 元の記事"
        bap.sanitize_reference_material = lambda filenames: ("", [])

        def _fake_apply(article, revision_request, reference_material="", audience=""):
            calls["apply_args"] = (article, revision_request, reference_material, audience)
            return "# 修正後の記事"
        bap.apply_user_revision = _fake_apply

        async def _fake_save(filename, article):
            calls["save_args"] = (filename, article)
            return True, None
        bap._save_final_article = _fake_save

        await bap.handle_project_reply(message)
    finally:
        bap.load_project = orig_load
        bap.load_draft_article = orig_load_draft
        bap.sanitize_reference_material = orig_sanitize
        bap.apply_user_revision = orig_apply
        bap._save_final_article = orig_save

    return calls, message


def test_revision_reply_applies_on_draft_done():
    calls, message = asyncio.run(_run_revision_reply_applies_on_draft_done())
    check("apply_user_revision called with original article", calls.get("apply_args", (None,))[0] == "# 元の記事")
    check("apply_user_revision called with user's request", calls.get("apply_args", (None, None))[1] == "もっと具体的な数値を入れて")
    check("apply_user_revision received audience", calls.get("apply_args", (None,) * 4)[3] == "初心者向け")
    check("revised article saved", calls.get("save_args", (None, None))[1] == "# 修正後の記事")


async def _run_revision_reply_rejects_when_completed():
    _reset_state()
    bap._completed_projects[3002] = "proj_c.md"
    message = _make_message(3002, 1, "これも直して")

    fake_project = {"frontmatter": {"status": "completed", "user_id": 1}}
    apply_called = {"n": 0}

    orig_load = bap.load_project
    orig_apply = bap.apply_user_revision
    try:
        bap.load_project = lambda filename: fake_project

        def _counting_apply(*a, **k):
            apply_called["n"] += 1
            return "should not be reached"
        bap.apply_user_revision = _counting_apply

        await bap.handle_project_reply(message)
    finally:
        bap.load_project = orig_load
        bap.apply_user_revision = orig_apply

    return apply_called, message


def test_revision_reply_rejects_when_completed_without_calling_llm():
    apply_called, message = asyncio.run(_run_revision_reply_rejects_when_completed())
    check("apply_user_revision NOT called for completed project", apply_called["n"] == 0)
    sent_texts = [c.args[0] for c in message.channel.send.await_args_list if c.args]
    check("user told project is already completed", any("完成済み" in t for t in sent_texts), sent_texts)


async def _run_revision_reply_completed_links_to_promoted_note():
    """要件⑤: completed案内は_Draft.mdではなく正式ノート(promoted_filename)を指すこと。"""
    _reset_state()
    bap._completed_projects[3004] = "proj_d.md"
    message = _make_message(3004, 1, "ここを直したい")

    fake_project = {"frontmatter": {"status": "completed", "user_id": 1, "promoted_filename": "記事タイトルD.md"}}
    orig_load = bap.load_project
    try:
        bap.load_project = lambda filename: fake_project
        await bap.handle_project_reply(message)
    finally:
        bap.load_project = orig_load

    return message


def test_revision_reply_completed_message_links_to_promoted_note():
    message = asyncio.run(_run_revision_reply_completed_links_to_promoted_note())
    sent_texts = [c.args[0] for c in message.channel.send.await_args_list if c.args]
    check("user directed to the promoted main note, not the draft", any("記事タイトルD" in t for t in sent_texts), sent_texts)


async def _run_revision_reply_ignored_for_unrelated_thread():
    _reset_state()
    message = _make_message(3003, 1, "無関係なスレッドへの発言")
    await bap.handle_project_reply(message)


def test_handle_project_reply_ignores_unrelated_thread():
    asyncio.run(_run_revision_reply_ignored_for_unrelated_thread())
    check("no session and no completed-cache entry -> silently ignored", True)


# ---------------------------------------------------------
# ArticleProjectCompleteView — 完成ボタンの冪等性・非共有busyフラグ
# ---------------------------------------------------------

async def _run_complete_button_opens_reflection_modal():
    """レビュー要件⑤: 🏁ボタンは直接completedにはせず、振り返り入力Modalを開く
    (完成確定と正式ノート化を1操作にまとめ、中間状態を作らないため)。"""
    _reset_state()
    bap._completed_projects[4001] = "proj_done.md"
    view = bap.ArticleProjectCompleteView()
    interaction = _make_fake_interaction(1)
    interaction.channel = AsyncMock()
    interaction.channel.id = 4001
    interaction.message = AsyncMock()

    fake_project = {"frontmatter": {"status": "draft_done", "user_id": 1, "draft_filename": "proj_done_Draft.md"}}

    orig_load = bap.load_project
    try:
        bap.load_project = lambda filename: fake_project
        await view.complete.callback(interaction)
    finally:
        bap.load_project = orig_load

    return interaction


def test_complete_button_opens_reflection_modal_instead_of_completing_directly():
    interaction = asyncio.run(_run_complete_button_opens_reflection_modal())
    check("send_modal called instead of direct completion", interaction.response.send_modal.await_count == 1)
    check("update_project_fields NOT called directly by the button", interaction.response.defer.await_count == 0)
    modal = interaction.response.send_modal.await_args.args[0]
    check("modal is ArticleProjectReflectionModal", isinstance(modal, bap.ArticleProjectReflectionModal))
    check("modal targets correct filename", modal.filename == "proj_done.md")


async def _run_reflection_modal_submit_promotes_and_disables_button():
    view = bap.ArticleProjectCompleteView()
    button = view.complete
    message = AsyncMock()
    modal = bap.ArticleProjectReflectionModal("proj_done.md", view, button, message)
    modal.learning._value = "学んだこと"
    modal.next_action._value = "次に試すこと"

    interaction = _make_fake_interaction(1)

    calls = {}

    def _fake_promote(filename, learning, next_action):
        calls["args"] = (filename, learning, next_action)
        return "記事タイトル.md", None

    orig_promote = bap.promote_and_reflect
    try:
        bap.promote_and_reflect = _fake_promote
        await modal.on_submit(interaction)
    finally:
        bap.promote_and_reflect = orig_promote

    return calls, interaction, button, message


def test_reflection_modal_submit_promotes_and_disables_button():
    calls, interaction, button, message = asyncio.run(_run_reflection_modal_submit_promotes_and_disables_button())
    check("promote_and_reflect called with modal inputs", calls["args"] == ("proj_done.md", "学んだこと", "次に試すこと"))
    check("button disabled after successful promotion", button.disabled is True)
    check("message edited to reflect disabled button", message.edit.await_count == 1)
    sent_texts = [c.args[0] for c in interaction.followup.send.await_args_list if c.args]
    check("user notified with promoted note link", any("記事タイトル" in t for t in sent_texts), sent_texts)


async def _run_reflection_modal_submit_failure_keeps_button_active():
    view = bap.ArticleProjectCompleteView()
    button = view.complete
    message = AsyncMock()
    modal = bap.ArticleProjectReflectionModal("proj_done.md", view, button, message)
    modal.learning._value = "学んだこと"
    modal.next_action._value = ""

    interaction = _make_fake_interaction(1)

    orig_promote = bap.promote_and_reflect
    try:
        bap.promote_and_reflect = lambda filename, learning, next_action: (None, "boom")
        await modal.on_submit(interaction)
    finally:
        bap.promote_and_reflect = orig_promote

    return interaction, button, message


def test_reflection_modal_submit_failure_keeps_button_active_for_retry():
    interaction, button, message = asyncio.run(_run_reflection_modal_submit_failure_keeps_button_active())
    check("button NOT disabled when promotion fails", button.disabled is False)
    check("message NOT edited when promotion fails", message.edit.await_count == 0)
    sent_texts = [c.args[0] for c in interaction.followup.send.await_args_list if c.args]
    check("user asked to retry", any("もう一度" in t for t in sent_texts), sent_texts)


async def _run_complete_button_idempotent_on_double_click():
    _reset_state()
    bap._completed_projects[4002] = "proj_done2.md"
    view = bap.ArticleProjectCompleteView()
    interaction = _make_fake_interaction(1)
    interaction.channel = AsyncMock()
    interaction.channel.id = 4002

    # 2回目のクリック時点では既にcompletedになっている状態を模す
    fake_project = {"frontmatter": {"status": "completed", "user_id": 1}}
    orig_load = bap.load_project
    try:
        bap.load_project = lambda filename: fake_project
        await view.complete.callback(interaction)
    finally:
        bap.load_project = orig_load

    return interaction


def test_complete_button_idempotent_when_already_completed():
    interaction = asyncio.run(_run_complete_button_idempotent_on_double_click())
    check("response sent (not an error) for already-completed click", interaction.response.send_message.await_count == 1)


async def _run_complete_button_does_not_share_busy_across_threads():
    """設計時に発見した罠の回帰: 永続Viewはクラス単位でcustom_id登録されるため、
    WrapUpView型のインスタンス変数busyフラグを使うと全スレッド共有になり、
    Aプロジェクトの処理中にBプロジェクトの操作まで弾かれてしまう。
    ArticleProjectCompleteViewはインスタンス変数busyを持たないことを直接確認する。"""
    view = bap.ArticleProjectCompleteView()
    return not hasattr(view, "busy")


def test_complete_view_has_no_shared_busy_flag():
    result = asyncio.run(_run_complete_button_does_not_share_busy_across_threads())
    check("ArticleProjectCompleteView instance has no busy attribute (no shared-state trap)", result)


# ---------------------------------------------------------
# /article-project-list — 進捗ダッシュボード (要件⑤)
# ---------------------------------------------------------

async def _run_cmd_article_project_list_calls_send_long_message():
    projects = [{"filename": "a.md", "frontmatter": {"status": "goal_set", "goal": "目標A"}}]
    interaction = _make_fake_interaction(1)

    calls = {}

    orig_list = bap.list_projects
    orig_send = bap.send_long_message
    try:
        bap.list_projects = lambda: projects

        async def _fake_send_long_message(interaction_arg, text, title=None):
            calls["args"] = (interaction_arg, text, title)
        bap.send_long_message = _fake_send_long_message

        await bap.cmd_article_project_list(interaction)
    finally:
        bap.list_projects = orig_list
        bap.send_long_message = orig_send

    return calls, interaction


def test_cmd_article_project_list_calls_send_long_message_with_dashboard_text():
    calls, interaction = asyncio.run(_run_cmd_article_project_list_calls_send_long_message())
    check("interaction.response.defer called (ephemeral)", interaction.response.defer.await_count == 1)
    check("send_long_message called with the interaction", calls["args"][0] is interaction)
    check("send_long_message received dashboard text mentioning the goal", "目標A" in calls["args"][1], calls["args"][1])
    check("title passed to send_long_message", calls["args"][2] == "記事プロジェクト一覧")


if __name__ == "__main__":
    test_handle_project_reply_ignores_when_no_session()
    test_handle_project_reply_ignores_other_user()
    test_handle_project_reply_records_answer_saves_and_advances()
    test_qa_save_failure_does_not_advance_or_call_next_question()
    test_reply_guarded_while_generating()
    test_reply_guarded_when_index_out_of_range()
    test_send_next_question_completes_and_saves_draft()
    test_send_next_question_keeps_session_when_save_fails()
    test_send_next_question_reports_conflict_backup()
    test_pending_save_retry_succeeds_and_cleans_up_session()
    test_concurrent_replies_do_not_double_trigger_generation()
    test_adopted_sources_view_confirm_excludes_unremovable_personal_info()
    test_confirm_blocks_qa_phase_and_keeps_button_active_when_save_adopted_sources_fails()
    test_restore_sessions_rebuilds_qa_state_and_resumes()
    test_restore_sessions_does_not_resume_goal_set()
    test_restore_sessions_registers_completed_projects_silently()
    test_restore_sessions_one_failure_does_not_block_others()
    test_revision_reply_applies_on_draft_done()
    test_revision_reply_rejects_when_completed_without_calling_llm()
    test_revision_reply_completed_message_links_to_promoted_note()
    test_complete_button_opens_reflection_modal_instead_of_completing_directly()
    test_reflection_modal_submit_promotes_and_disables_button()
    test_reflection_modal_submit_failure_keeps_button_active_for_retry()
    test_handle_project_reply_ignores_unrelated_thread()
    test_complete_button_idempotent_when_already_completed()
    test_complete_view_has_no_shared_busy_flag()
    test_cmd_article_project_list_calls_send_long_message_with_dashboard_text()

    print("\n" + "=" * 60)
    print(f"結果: {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("失敗:")
        for f in _FAIL:
            print(f"  - {f}")
    sys.exit(1 if _FAIL else 0)
