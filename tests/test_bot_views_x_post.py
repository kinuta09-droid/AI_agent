# -*- coding: utf-8 -*-
"""
XPostApprovalView (commands/bot_views.py) のボタンコールバック回帰テスト。

コールバックはクロージャとして生成されるため、View インスタンスを介さず
_make_xpost_*_callback(slot) を直接呼び出してテストする(他Viewのテストに倣い、
discord.py の実ゲートウェイ接続は不要)。

X API呼び出し(post_tweet_for_approval)・Obsidian書き戻し(record_x_post_result)・
コストガード(X_POST_COUNTER)は全てスタブに差し替え、実際の外部通信・実Vault書き込み・
実カウンタファイルには一切触れない。

実行: py tests/test_bot_views_x_post.py
"""
import os
import sys
import shutil
import asyncio
import tempfile
from unittest.mock import AsyncMock

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import commands.bot_views as bv  # noqa: E402
import lib.x_post_store as store  # noqa: E402
from lib.cost_guard import DailyCounter  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _make_fake_interaction():
    interaction = AsyncMock()
    interaction.channel = AsyncMock()
    return interaction


class _Sandbox:
    """queue/counter/X API/Obsidian書き戻しを丸ごと一時物・スタブへ差し替える。"""

    def __enter__(self):
        self._orig_queue_path = store.QUEUE_PATH
        self._orig_counter = bv.X_POST_COUNTER
        self._orig_post = bv.post_tweet_for_approval
        self._orig_record = bv.record_x_post_result

        self.root = tempfile.mkdtemp(prefix="xpost_view_test_")
        store.QUEUE_PATH = os.path.join(self.root, "queue.json")
        bv.X_POST_COUNTER = DailyCounter(
            path=os.path.join(self.root, "counter.json"), limit=10, name="X投稿-test", notify=False
        )
        self.post_calls = []
        self.record_calls = []
        self._post_result = {"outcome": "posted", "detail": "ok", "url": "https://x.com/i/web/status/1"}
        self._record_result = True

        def _fake_post(text):
            self.post_calls.append(text)
            return self._post_result

        def _fake_record(*args):
            self.record_calls.append(args)
            return self._record_result

        bv.post_tweet_for_approval = _fake_post
        bv.record_x_post_result = _fake_record
        return self

    def set_post_result(self, result: dict):
        self._post_result = result

    def set_record_result(self, ok: bool):
        self._record_result = ok

    def __exit__(self, *exc):
        store.QUEUE_PATH = self._orig_queue_path
        bv.X_POST_COUNTER = self._orig_counter
        bv.post_tweet_for_approval = self._orig_post
        bv.record_x_post_result = self._orig_record
        shutil.rmtree(self.root, ignore_errors=True)


def _run(coro):
    asyncio.run(coro)


# --------------------------------------------------------------------------
# View構造 / 継承
# --------------------------------------------------------------------------

def test_view_inherits_allowed_user_view():
    check("XPostApprovalView subclasses AllowedUserView",
          issubclass(bv.XPostApprovalView, bv.AllowedUserView))


def test_view_has_15_buttons_for_5_slots():
    """slots引数無し(登録専用、bot.add_view向け)は全5スロット分。"""
    async def go():
        view = bv.XPostApprovalView()
        check("view has 15 buttons (5 slots x 3)", len(view.children) == 15, len(view.children))
    _run(go())
    print("✅ test_view_has_15_buttons_for_5_slots")


def test_view_with_single_slot_has_only_3_buttons():
    """実際にメッセージへ添付するView(slots=[N])は、そのスロットの3ボタンだけを持つこと
    (5スロット分15個が丸ごと出てしまっていた実機バグの回帰テスト)。
    """
    async def go():
        view = bv.XPostApprovalView(slots=[2])
        check("view has exactly 3 buttons for a single slot", len(view.children) == 3, len(view.children))
        custom_ids = {b.custom_id for b in view.children}
        check("buttons are all for slot 2",
              custom_ids == {"xpost_approve:2", "xpost_edit:2", "xpost_reject:2"}, custom_ids)
    _run(go())
    print("✅ test_view_with_single_slot_has_only_3_buttons")


# --------------------------------------------------------------------------
# 承認ボタン
# --------------------------------------------------------------------------

def test_approve_callback_success_transitions_to_posted():
    async def go():
        with _Sandbox() as ctx:
            slot, _ = store.allocate_slot("投稿本文", "30_Main/x.md")
            store.try_transition(slot, "draft", "pending_approval")

            interaction = _make_fake_interaction()
            await bv._make_xpost_approve_callback(slot)(interaction)

            entry = store.get_slot(slot)
            check("status becomes posted", entry["status"] == "posted", entry["status"])
            check("tweet_url recorded", entry["tweet_url"] == "https://x.com/i/web/status/1")
            check("obsidian_synced marked True", entry["obsidian_synced"] is True)
            check("post_tweet_for_approval called with slot text", ctx.post_calls == ["投稿本文"], ctx.post_calls)
            check("record_x_post_result called once", len(ctx.record_calls) == 1, ctx.record_calls)

            sent_msg = interaction.channel.send.await_args.args[0]
            check("success message mentions URL", "https://x.com/i/web/status/1" in sent_msg, sent_msg)
    _run(go())
    print("✅ test_approve_callback_success_transitions_to_posted")


def test_approve_callback_double_click_second_is_rejected():
    async def go():
        with _Sandbox() as ctx:
            slot, _ = store.allocate_slot("t", "n.md")
            store.try_transition(slot, "draft", "pending_approval")
            callback = bv._make_xpost_approve_callback(slot)

            await callback(_make_fake_interaction())
            check("first click posts", store.get_slot(slot)["status"] == "posted")

            interaction2 = _make_fake_interaction()
            await callback(interaction2)
            sent_msg = interaction2.channel.send.await_args.args[0]
            check("second click rejected message", ("既に処理済み" in sent_msg) or ("処理中" in sent_msg), sent_msg)
            check("post_tweet_for_approval called only once (no double post)", len(ctx.post_calls) == 1, ctx.post_calls)
    _run(go())
    print("✅ test_approve_callback_double_click_second_is_rejected")


def test_approve_callback_needs_review_on_communication_failure():
    async def go():
        with _Sandbox() as ctx:
            slot, _ = store.allocate_slot("t", "n.md")
            store.try_transition(slot, "draft", "pending_approval")
            ctx.set_post_result({"outcome": "needs_review", "detail": "通信エラー(投稿成否不明): timeout", "url": None})

            interaction = _make_fake_interaction()
            await bv._make_xpost_approve_callback(slot)(interaction)

            entry = store.get_slot(slot)
            check("status becomes needs_review", entry["status"] == "needs_review", entry["status"])
            check("record_x_post_result NOT called on needs_review", len(ctx.record_calls) == 0, ctx.record_calls)
            sent_msg = interaction.channel.send.await_args.args[0]
            check("message mentions 要確認 (no auto-resend)", "要確認" in sent_msg, sent_msg)
    _run(go())
    print("✅ test_approve_callback_needs_review_on_communication_failure")


def test_approve_callback_failed_response():
    async def go():
        with _Sandbox() as ctx:
            slot, _ = store.allocate_slot("t", "n.md")
            store.try_transition(slot, "draft", "pending_approval")
            ctx.set_post_result({"outcome": "failed", "detail": "400 Bad Request", "url": None})

            interaction = _make_fake_interaction()
            await bv._make_xpost_approve_callback(slot)(interaction)

            entry = store.get_slot(slot)
            check("status becomes failed", entry["status"] == "failed", entry["status"])
            check("record_x_post_result NOT called on failed", len(ctx.record_calls) == 0)
    _run(go())
    print("✅ test_approve_callback_failed_response")


def test_approve_callback_cost_guard_exceeded_reverts_to_pending_approval():
    async def go():
        with _Sandbox() as ctx:
            counter = DailyCounter(path=os.path.join(ctx.root, "full.json"), limit=1, name="X投稿-test", notify=False)
            counter.check_and_increment()  # 上限(1)を使い切っておく
            bv.X_POST_COUNTER = counter

            slot, _ = store.allocate_slot("t", "n.md")
            store.try_transition(slot, "draft", "pending_approval")

            interaction = _make_fake_interaction()
            await bv._make_xpost_approve_callback(slot)(interaction)

            entry = store.get_slot(slot)
            check("status reverts to pending_approval (no post attempt, safe to retry tomorrow)",
                  entry["status"] == "pending_approval", entry["status"])
            check("post_tweet_for_approval NOT called", len(ctx.post_calls) == 0, ctx.post_calls)
            sent_msg = interaction.channel.send.await_args.args[0]
            check("message mentions 上限", "上限" in sent_msg, sent_msg)
    _run(go())
    print("✅ test_approve_callback_cost_guard_exceeded_reverts_to_pending_approval")


# --------------------------------------------------------------------------
# 修正ボタン / Modal
# --------------------------------------------------------------------------

def test_edit_callback_rejects_when_not_pending_approval():
    async def go():
        with _Sandbox():
            slot, _ = store.allocate_slot("t", "n.md")  # draftのまま(pending_approvalに未遷移)

            interaction = _make_fake_interaction()
            await bv._make_xpost_edit_callback(slot)(interaction)

            check("send_modal NOT called for non-pending slot", not interaction.response.send_modal.called)
            check("error message sent instead", interaction.response.send_message.await_args is not None)
    _run(go())
    print("✅ test_edit_callback_rejects_when_not_pending_approval")


def test_edit_modal_updates_text_and_keeps_pending_approval():
    async def go():
        with _Sandbox():
            slot, _ = store.allocate_slot("元の本文", "n.md")
            store.try_transition(slot, "draft", "pending_approval")

            modal = bv.XPostEditModal(slot)
            modal.text._value = "修正後の本文"
            interaction = _make_fake_interaction()
            await modal.on_submit(interaction)

            entry = store.get_slot(slot)
            check("text updated", entry["text"] == "修正後の本文", entry["text"])
            check("status stays pending_approval (re-approval required)",
                  entry["status"] == "pending_approval", entry["status"])
            check("edit_message called to re-render", interaction.response.edit_message.await_args is not None)
    _run(go())
    print("✅ test_edit_modal_updates_text_and_keeps_pending_approval")


def test_edit_modal_rejects_after_already_posting():
    async def go():
        with _Sandbox():
            slot, _ = store.allocate_slot("元の本文", "n.md")
            store.try_transition(slot, "draft", "pending_approval")
            store.try_transition(slot, "pending_approval", "posting")

            modal = bv.XPostEditModal(slot)
            modal.text._value = "横から修正"
            interaction = _make_fake_interaction()
            await modal.on_submit(interaction)

            entry = store.get_slot(slot)
            check("text NOT overwritten while posting", entry["text"] == "元の本文", entry["text"])
            check("error message sent", interaction.response.send_message.await_args is not None)
    _run(go())
    print("✅ test_edit_modal_rejects_after_already_posting")


# --------------------------------------------------------------------------
# 却下ボタン
# --------------------------------------------------------------------------

def test_reject_callback_releases_slot():
    async def go():
        with _Sandbox():
            slot, _ = store.allocate_slot("t", "n.md")
            store.try_transition(slot, "draft", "pending_approval")

            interaction = _make_fake_interaction()
            await bv._make_xpost_reject_callback(slot)(interaction)

            check("slot released", store.get_slot(slot) is None)
            content = interaction.response.edit_message.await_args.kwargs.get("content", "")
            check("edit_message content mentions 却下", "却下" in content, content)
    _run(go())
    print("✅ test_reject_callback_releases_slot")


def test_reject_callback_refuses_after_posted():
    async def go():
        with _Sandbox():
            slot, _ = store.allocate_slot("t", "n.md")
            store.try_transition(slot, "draft", "pending_approval")
            store.try_transition(slot, "pending_approval", "posting")
            store.try_transition(slot, "posting", "posted", tweet_url="https://x.com/i/web/status/9")

            interaction = _make_fake_interaction()
            await bv._make_xpost_reject_callback(slot)(interaction)

            check("posted slot is not released by reject", store.get_slot(slot) is not None)
    _run(go())
    print("✅ test_reject_callback_refuses_after_posted")


if __name__ == "__main__":
    tests = [
        test_view_inherits_allowed_user_view,
        test_view_has_15_buttons_for_5_slots,
        test_view_with_single_slot_has_only_3_buttons,
        test_approve_callback_success_transitions_to_posted,
        test_approve_callback_double_click_second_is_rejected,
        test_approve_callback_needs_review_on_communication_failure,
        test_approve_callback_failed_response,
        test_approve_callback_cost_guard_exceeded_reverts_to_pending_approval,
        test_edit_callback_rejects_when_not_pending_approval,
        test_edit_modal_updates_text_and_keeps_pending_approval,
        test_edit_modal_rejects_after_already_posting,
        test_reject_callback_releases_slot,
        test_reject_callback_refuses_after_posted,
    ]
    for t in tests:
        t()

    print("\n" + "=" * 60)
    print(f"結果: {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("失敗:")
        for f in _FAIL:
            print(f"  - {f}")
    sys.exit(1 if _FAIL else 0)
