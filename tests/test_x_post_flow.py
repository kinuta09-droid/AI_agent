# -*- coding: utf-8 -*-
"""
X投稿承認フロー (lib/x_post_store + commands/bot_views + tools/tool_twitter_post +
tools/tool_article_project) の通し検証。個々のモジュールのユニットテストは
test_x_post_store.py / test_bot_views_x_post.py / test_tool_twitter_post.py /
test_tool_article_project.py にあるため、ここでは複数モジュールを跨ぐ一連の
流れ(生成→修正→承認→投稿→Obsidian書き戻し / 再起動復旧 / 権限)に絞って検証する。

実行: py tests/test_x_post_flow.py
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
import tools.tool_article_project as tap  # noqa: E402
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
    """queue/counter/Vault(PROJECTS_DIR・MAIN_DIR)/X APIを丸ごと一時物・スタブへ差し替える。"""

    def __enter__(self):
        self._orig_queue_path = store.QUEUE_PATH
        self._orig_counter = bv.X_POST_COUNTER
        self._orig_post = bv.post_tweet_for_approval
        self._orig_projects_dir = tap.PROJECTS_DIR
        self._orig_main_dir = tap.MAIN_DIR

        self.root = tempfile.mkdtemp(prefix="xpost_flow_test_")
        self.main_root = tempfile.mkdtemp(prefix="xpost_flow_main_test_")
        store.QUEUE_PATH = os.path.join(self.root, "queue.json")
        tap.PROJECTS_DIR = self.root
        tap.MAIN_DIR = self.main_root
        bv.X_POST_COUNTER = DailyCounter(
            path=os.path.join(self.root, "counter.json"), limit=10, name="X投稿-test", notify=False
        )
        self.post_calls = []
        self._post_result = {"outcome": "posted", "detail": "ok", "url": "https://x.com/i/web/status/1"}

        def _fake_post(text):
            self.post_calls.append(text)
            return self._post_result

        bv.post_tweet_for_approval = _fake_post
        # record_x_post_result はスタブ化せず、実際にtap.MAIN_DIR(一時ディレクトリ)へ
        # 書き込ませる — Obsidian書き戻しまで含めた通し検証のため。
        return self

    def set_post_result(self, result: dict):
        self._post_result = result

    def write_main_note(self, filename: str, body: str = "# タイトル\n\n本文です。"):
        with open(os.path.join(self.main_root, filename), "w", encoding="utf-8") as f:
            f.write(f"---\ntags: [type/permanent]\nstatus: done\n---\n\n{body}\n")

    def __exit__(self, *exc):
        store.QUEUE_PATH = self._orig_queue_path
        bv.X_POST_COUNTER = self._orig_counter
        bv.post_tweet_for_approval = self._orig_post
        tap.PROJECTS_DIR = self._orig_projects_dir
        tap.MAIN_DIR = self._orig_main_dir
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.main_root, ignore_errors=True)


def _run(coro):
    asyncio.run(coro)


# --------------------------------------------------------------------------
# 1) 生成 -> 修正 -> 承認 -> 投稿 -> Obsidian書き戻し (エンドツーエンド)
# --------------------------------------------------------------------------

def test_generate_edit_approve_posts_and_writes_back_to_obsidian():
    async def go():
        with _Sandbox() as ctx:
            filename, _ = tap.create_project("目標X")
            tap.update_project_fields(filename, status="completed", promoted_filename="記事X.md")
            ctx.write_main_note("記事X.md")

            # 生成 (Geminiはスタブ)
            import lib.lib_shared as ls
            orig_gen = ls.client.models.generate_content
            try:
                ls.client.models.generate_content = lambda model=None, contents=None, config=None: (
                    type("R", (), {"text": "生成された投稿文"})()
                )
                draft, error = tap.generate_x_post_draft(filename)
            finally:
                ls.client.models.generate_content = orig_gen
            check("draft generated without error", error is None, error)

            slot, _post_id = store.allocate_slot(draft["text"], draft["source_note"])
            store.try_transition(slot, "draft", "pending_approval")

            # 修正: ボタンで直接Xに出す前に本文を書き換える (再承認が必要)
            modal = bv.XPostEditModal(slot)
            modal.text._value = "修正後の投稿文"
            await modal.on_submit(_make_fake_interaction())
            check("edited text is pending_approval, not yet posted",
                  store.get_slot(slot)["status"] == "pending_approval")

            # 承認 -> 投稿 -> Obsidian書き戻し
            interaction = _make_fake_interaction()
            await bv._make_xpost_approve_callback(slot)(interaction)

            entry = store.get_slot(slot)
            check("final status is posted", entry["status"] == "posted", entry["status"])
            check("posted text is the edited one (not the original draft)",
                  ctx.post_calls == ["修正後の投稿文"], ctx.post_calls)
            check("obsidian_synced marked True", entry["obsidian_synced"] is True)

            with open(os.path.join(ctx.main_root, "記事X.md"), "r", encoding="utf-8") as f:
                fm, _ = tap._split_frontmatter(f.read())
            check("main note frontmatter records tweet url",
                  fm.get("x_post_url") == "https://x.com/i/web/status/1", fm.get("x_post_url"))
            check("main note frontmatter records posted text",
                  fm.get("x_post_text") == "修正後の投稿文", fm.get("x_post_text"))
    _run(go())
    print("✅ test_generate_edit_approve_posts_and_writes_back_to_obsidian")


# --------------------------------------------------------------------------
# 2) 権限: 許可されていないユーザーは承認ボタンを操作できない
# --------------------------------------------------------------------------

def test_permission_denied_user_cannot_pass_interaction_check():
    async def go():
        with _Sandbox():
            view = bv.XPostApprovalView()
            bv.init_views(lambda uid: uid == 111)
            try:
                allowed_interaction = AsyncMock()
                allowed_interaction.user.id = 111
                ok = await view.interaction_check(allowed_interaction)
                check("allowed user passes interaction_check", ok is True)

                denied_interaction = AsyncMock()
                denied_interaction.user.id = 999
                ok = await view.interaction_check(denied_interaction)
                check("denied user is rejected by interaction_check", ok is False)
                check("denied user gets ephemeral refusal message",
                      denied_interaction.response.send_message.await_args is not None)
            finally:
                bv.init_views(None)
    _run(go())
    print("✅ test_permission_denied_user_cannot_pass_interaction_check")


# --------------------------------------------------------------------------
# 3) 連打: View経由で取得したボタンcallbackを2回押しても1回しか投稿されない
# --------------------------------------------------------------------------

def test_double_click_via_view_buttons_posts_only_once():
    async def go():
        with _Sandbox() as ctx:
            slot, _ = store.allocate_slot("連打テスト本文", "n.md")
            store.try_transition(slot, "draft", "pending_approval")

            view = bv.XPostApprovalView()
            approve_button = next(
                b for b in view.children
                if getattr(b, "custom_id", None) == f"xpost_approve:{slot}"
            )
            await approve_button.callback(_make_fake_interaction())
            await approve_button.callback(_make_fake_interaction())

            check("only one post_tweet_for_approval call across 2 clicks",
                  len(ctx.post_calls) == 1, ctx.post_calls)
            check("final status is posted (not stuck)", store.get_slot(slot)["status"] == "posted")
    _run(go())
    print("✅ test_double_click_via_view_buttons_posts_only_once")


# --------------------------------------------------------------------------
# 4) 再起動復旧: postingで固まったスロットはneeds_reviewへ、再クリックは弾かれる
# --------------------------------------------------------------------------

def test_restart_recovery_blocks_reapproval_of_stuck_slot():
    async def go():
        with _Sandbox() as ctx:
            slot, _ = store.allocate_slot("再起動テスト本文", "n.md")
            store.try_transition(slot, "draft", "pending_approval")
            # Bot が投稿処理中にクラッシュした状況を模擬 (postingのまま放置)
            store.try_transition(slot, "pending_approval", "posting")

            recovered = store.recover_stuck_posting()
            check("recover_stuck_posting recovers the stuck slot", recovered == [slot], recovered)
            check("status becomes needs_review after restart", store.get_slot(slot)["status"] == "needs_review")

            # 再起動後にユーザーがもう一度承認ボタンを押しても、pending_approval起点の
            # 遷移ではないため弾かれ、二重投稿は起きない。
            interaction = _make_fake_interaction()
            await bv._make_xpost_approve_callback(slot)(interaction)
            check("re-click after recovery does not trigger a post", len(ctx.post_calls) == 0, ctx.post_calls)
            check("status stays needs_review (stays in the review queue)",
                  store.get_slot(slot)["status"] == "needs_review")
    _run(go())
    print("✅ test_restart_recovery_blocks_reapproval_of_stuck_slot")


# --------------------------------------------------------------------------
# 5) 通信失敗: needs_review化され、呼び出し元は自動再送しない
# --------------------------------------------------------------------------

def test_communication_failure_stays_needs_review_without_auto_resend():
    async def go():
        with _Sandbox() as ctx:
            slot, _ = store.allocate_slot("通信失敗テスト", "n.md")
            store.try_transition(slot, "draft", "pending_approval")
            ctx.set_post_result({"outcome": "needs_review", "detail": "通信エラー(投稿成否不明): timeout", "url": None})

            await bv._make_xpost_approve_callback(slot)(_make_fake_interaction())
            check("status is needs_review after communication failure",
                  store.get_slot(slot)["status"] == "needs_review")
            check("exactly one post attempt was made (no automatic retry)", len(ctx.post_calls) == 1, ctx.post_calls)

            # 呼び出し元 (スケジューラ等) が同じスロットに対して再度承認処理を
            # 走らせても、pending_approval起点でないため二重投稿は起きない。
            await bv._make_xpost_approve_callback(slot)(_make_fake_interaction())
            check("still exactly one post attempt (auto-resend is not triggered)", len(ctx.post_calls) == 1, ctx.post_calls)
    _run(go())
    print("✅ test_communication_failure_stays_needs_review_without_auto_resend")


if __name__ == "__main__":
    tests = [
        test_generate_edit_approve_posts_and_writes_back_to_obsidian,
        test_permission_denied_user_cannot_pass_interaction_check,
        test_double_click_via_view_buttons_posts_only_once,
        test_restart_recovery_blocks_reapproval_of_stuck_slot,
        test_communication_failure_stays_needs_review_without_auto_resend,
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
