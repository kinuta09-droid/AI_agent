# -*- coding: utf-8 -*-
"""commands/bot_commands_chat.py の /save コマンド (cmd_save) の回帰テスト。

対象:
  - セッション無し/history空 → archive_chat_session を呼ばず警告のみ返す
  - Success/Skipped は session["history"] をリセットする (非対称設計の核心:
    Skipped でも「ユーザー明示操作なのでリセットする」仕様)
  - Error(文字列)/例外は history を保持しセッション継続、例外は外へ伝播させない
    (is_failure_result(None) が False を返すことで誤ってリセット側に落ちない回帰)
  - snapshot 分だけが切り落とされ、呼び出し中に追加された要素は保持される
  - IDLE_ARCHIVE_MINUTES == 15 の軽量アサート

方針: Gemini / Vault への実I/Oはすべてスタブ差し替え。interaction は
tests/test_idea_brainstorm.py と同じ AsyncMock ベースの偽物を使う。

実行: py tests/test_chat_save.py
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, patch

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import commands.bot_commands_chat as chat_mod

_FAIL = False


def _make_fake_interaction(user_id: int):
    interaction = AsyncMock()
    interaction.user.id = user_id
    return interaction


def _make_history(n=2):
    return [{"role": "user", "content": f"msg{i}"} for i in range(n)]


def _check(name, cond, detail=""):
    global _FAIL
    if cond:
        print(f"✅ [{name}]")
    else:
        _FAIL = True
        print(f"❌ [{name}] {detail}")


async def _run_no_session():
    chat_mod._chat_sessions.pop(999, None)
    interaction = _make_fake_interaction(999)
    with patch.object(chat_mod, "archive_chat_session") as fake_archive:
        await chat_mod.cmd_save(interaction)
    interaction.response.send_message.assert_awaited_once()
    _check(
        "1 セッション無しは警告のみ・archive_chat_sessionは呼ばれない",
        not fake_archive.called,
    )
    warn_text = interaction.response.send_message.call_args.args[0]
    _check("1 警告文にセッション/保存に触れる内容がある", "セッション" in warn_text or "保存" in warn_text)


async def _run_empty_history():
    user_id = 998
    chat_mod._chat_sessions[user_id] = {"history": []}
    interaction = _make_fake_interaction(user_id)
    try:
        with patch.object(chat_mod, "archive_chat_session") as fake_archive:
            await chat_mod.cmd_save(interaction)
        interaction.response.send_message.assert_awaited_once()
        _check(
            "2 history空も警告のみ・archive_chat_sessionは呼ばれない",
            not fake_archive.called,
        )
    finally:
        chat_mod._chat_sessions.pop(user_id, None)


async def _run_success():
    user_id = 1
    chat_mod._chat_sessions[user_id] = {"history": _make_history(2)}
    interaction = _make_fake_interaction(user_id)
    try:
        with patch.object(
            chat_mod, "archive_chat_session",
            lambda h, lenient=False: "Success: Archived chat session to [[Foo]]",
        ):
            await chat_mod.cmd_save(interaction)
        _check(
            "3 Successでhistoryが空にリセットされる",
            chat_mod._chat_sessions[user_id]["history"] == [],
        )
        interaction.followup.send.assert_awaited_once()
        sent = interaction.followup.send.call_args.args[0]
        _check("3 応答文に「リセット」が含まれる", "リセット" in sent)
    finally:
        chat_mod._chat_sessions.pop(user_id, None)


async def _run_skipped():
    user_id = 2
    chat_mod._chat_sessions[user_id] = {"history": _make_history(1)}
    interaction = _make_fake_interaction(user_id)
    try:
        with patch.object(
            chat_mod, "archive_chat_session",
            lambda h, lenient=False: "Skipped: turns=1 (閾値2未満)",
        ):
            await chat_mod.cmd_save(interaction)
        _check(
            "4 Skippedでもhistoryは空にリセットされる (非対称設計の核心)",
            chat_mod._chat_sessions[user_id]["history"] == [],
        )
        sent = interaction.followup.send.call_args.args[0]
        _check("4 応答文に「保存の必要なし」「リセット」が含まれる",
               "保存の必要なし" in sent and "リセット" in sent)
    finally:
        chat_mod._chat_sessions.pop(user_id, None)


async def _run_error_string():
    user_id = 3
    original = _make_history(2)
    chat_mod._chat_sessions[user_id] = {"history": list(original)}
    interaction = _make_fake_interaction(user_id)
    try:
        with patch.object(
            chat_mod, "archive_chat_session",
            lambda h, lenient=False: "Error: Gemini budget exceeded",
        ):
            await chat_mod.cmd_save(interaction)
        _check(
            "5 Error文字列時はhistoryが元のまま保持される",
            chat_mod._chat_sessions[user_id]["history"] == original,
        )
        sent = interaction.followup.send.call_args.args[0]
        _check("5 応答文に「失敗」「継続」が含まれる", "失敗" in sent and "継続" in sent)
    finally:
        chat_mod._chat_sessions.pop(user_id, None)


async def _run_exception():
    user_id = 4
    original = _make_history(2)
    chat_mod._chat_sessions[user_id] = {"history": list(original)}
    interaction = _make_fake_interaction(user_id)
    try:
        with patch.object(chat_mod, "archive_chat_session", side_effect=RuntimeError("boom")), \
             patch.object(chat_mod, "log_command_error") as fake_log:
            # cmd_save 自身が例外を伝播させないことを確認する。
            await chat_mod.cmd_save(interaction)
        _check("6 例外はcmd_save内で吸収され外へ伝播しない (到達すれば成立)", True)
        _check(
            "6 例外時はhistoryが元のまま保持される (is_failure_result(None)==Falseの回帰)",
            chat_mod._chat_sessions[user_id]["history"] == original,
        )
        _check("6 log_command_errorが呼ばれる", fake_log.called)
    finally:
        chat_mod._chat_sessions.pop(user_id, None)


async def _run_snapshot_safety():
    """呼び出し前に積んであった分だけが切り落とされ、呼び出し後に history に
    追加された要素は snapshot に含まれないので保持される、という「snapshot分だけ
    切り落とされる」契約を直接検証する (session["history"][len(snapshot):] の実装通り)。
    """
    user_id = 5
    chat_mod._chat_sessions[user_id] = {"history": _make_history(2)}
    interaction = _make_fake_interaction(user_id)
    try:
        # archive呼び出しの「後」に追加されたことを模した状態を作るため、
        # archive_chat_session 内で新規メッセージを追加してから返す。
        # (asyncio.to_thread はスレッド実行だが同期関数内の単純な list.append は
        #  ここでは競合を起こさないので直接検証で十分)
        def _fake_archive(snapshot, lenient=False):
            chat_mod._chat_sessions[user_id]["history"].append(
                {"role": "user", "content": "new-during-archive"}
            )
            return "Success: saved"

        with patch.object(chat_mod, "archive_chat_session", _fake_archive):
            await chat_mod.cmd_save(interaction)

        remaining = chat_mod._chat_sessions[user_id]["history"]
        _check(
            "7 snapshot分だけ切り落とされ、後から追加された要素は残る",
            remaining == [{"role": "user", "content": "new-during-archive"}],
            detail=f"remaining={remaining}",
        )
    finally:
        chat_mod._chat_sessions.pop(user_id, None)


def test_idle_archive_minutes_is_15():
    _check("8 IDLE_ARCHIVE_MINUTES == 15", chat_mod.IDLE_ARCHIVE_MINUTES == 15)


async def _run_lenient_flag_passed():
    """/save は archive_chat_session に lenient=True を渡すこと (雑談スキップ回避の要)。"""
    user_id = 6
    chat_mod._chat_sessions[user_id] = {"history": _make_history(2)}
    interaction = _make_fake_interaction(user_id)
    try:
        with patch.object(chat_mod, "archive_chat_session") as fake_archive:
            fake_archive.return_value = "Success: saved"
            await chat_mod.cmd_save(interaction)
        args = fake_archive.call_args.args
        _check(
            "9 archive_chat_sessionにlenient=Trueが渡される",
            len(args) >= 2 and args[1] is True,
            detail=f"call_args={fake_archive.call_args}",
        )
    finally:
        chat_mod._chat_sessions.pop(user_id, None)


async def _run_lock_serializes_concurrent_saves():
    """/save が2回同時に飛んできても _get_archive_lock により直列化され、
    2回目は1回目のarchive完了後に history を取り直して「保存する会話が無い」
    と正しく判定すること (grillで指摘された /save と _idle_archive_loop の
    競合によるデータ消失リスクの回帰確認、_idle_archive_loop 側も同じロックを
    使うため cmd_save 同士の直列化で代表して検証する)。"""
    user_id = 8
    chat_mod._chat_sessions[user_id] = {"history": _make_history(2)}
    interaction1 = _make_fake_interaction(user_id)
    interaction2 = _make_fake_interaction(user_id)

    call_count = {"n": 0}

    def _slow_archive(snapshot, lenient=False):
        call_count["n"] += 1
        time.sleep(0.05)  # asyncio.to_thread 経由なのでイベントループはブロックしない
        return "Success: saved"

    try:
        with patch.object(chat_mod, "archive_chat_session", _slow_archive):
            await asyncio.gather(
                chat_mod.cmd_save(interaction1),
                chat_mod.cmd_save(interaction2),
            )
        _check(
            "10 同時実行でもarchive呼び出しはロックで直列化され1回だけ実行される",
            call_count["n"] == 1,
            detail=f"call_count={call_count['n']}",
        )
        texts = [
            interaction1.followup.send.call_args.args[0],
            interaction2.followup.send.call_args.args[0],
        ]
        _check(
            "10 片方は保存成功、もう片方はhistory再取得により保存対象無しと応答する",
            any("リセット" in t for t in texts) and any("保存する会話がありませんでした" in t for t in texts),
            detail=texts,
        )
    finally:
        chat_mod._chat_sessions.pop(user_id, None)


def main():
    asyncio.run(_run_no_session())
    asyncio.run(_run_empty_history())
    asyncio.run(_run_success())
    asyncio.run(_run_skipped())
    asyncio.run(_run_error_string())
    asyncio.run(_run_exception())
    asyncio.run(_run_snapshot_safety())
    asyncio.run(_run_lenient_flag_passed())
    asyncio.run(_run_lock_serializes_concurrent_saves())
    test_idle_archive_minutes_is_15()


if __name__ == "__main__":
    main()
    sys.exit(1 if _FAIL else 0)
