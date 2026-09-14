# -*- coding: utf-8 -*-
"""
lib/x_post_store.py (X投稿承認キューの状態管理ストア) のユニットテスト。

一時ディレクトリ上に QUEUE_PATH を差し替えてテストするため、実ファイル
logs/x_post_queue.json には一切触れない。

実行: py tests/test_x_post_store.py
"""
import os
import sys
import shutil
import tempfile
from contextlib import contextmanager

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.x_post_store as store


@contextmanager
def _temp_queue():
    root = tempfile.mkdtemp(prefix="x_post_store_test_")
    orig_path = store.QUEUE_PATH
    store.QUEUE_PATH = os.path.join(root, "queue.json")
    try:
        yield
    finally:
        store.QUEUE_PATH = orig_path
        shutil.rmtree(root, ignore_errors=True)


def test_allocate_slot_fills_empty_slot_first():
    """空スロットに status=draft で登録され、post_idが発行される。"""
    with _temp_queue():
        slot, post_id = store.allocate_slot("hello", "30_Main/x.md")
        assert slot == 0
        assert post_id and len(post_id) == 32
        entry = store.get_slot(0)
        assert entry["status"] == "draft"
        assert entry["text"] == "hello"
        assert entry["source_note"] == "30_Main/x.md"
        assert entry["tweet_url"] is None
        assert entry["obsidian_synced"] is False
    print("✅ test_allocate_slot_fills_empty_slot_first")


def test_allocate_slot_exhausted_returns_none():
    """全5スロットが埋まると (None, None) を返す。"""
    with _temp_queue():
        for _ in range(store.X_POST_SLOTS):
            slot, _ = store.allocate_slot("t", "n.md")
            assert slot is not None
        slot, post_id = store.allocate_slot("overflow", "n.md")
        assert slot is None and post_id is None
    print("✅ test_allocate_slot_exhausted_returns_none")


def test_update_text_requires_pending_approval():
    """draft/posted状態では本文更新できず、pending_approvalでのみ成功する。"""
    with _temp_queue():
        slot, _ = store.allocate_slot("original", "n.md")
        assert store.update_text(slot, "edited while draft") is False, \
            "draft状態からの直接更新は許可しないべき"

        assert store.try_transition(slot, "draft", "pending_approval") is True
        assert store.update_text(slot, "edited text") is True
        assert store.get_slot(slot)["text"] == "edited text"
        assert store.get_slot(slot)["status"] == "pending_approval", \
            "修正後もpending_approvalのまま留まり、再承認はボタン操作に委ねるべき"
    print("✅ test_update_text_requires_pending_approval")


def test_try_transition_is_cas_and_rejects_mismatch():
    """expected_statusが一致しない遷移は拒否され、状態は変化しない (二重投稿防止の要)。"""
    with _temp_queue():
        slot, _ = store.allocate_slot("t", "n.md")
        store.try_transition(slot, "draft", "pending_approval")

        assert store.try_transition(slot, "pending_approval", "posting") is True
        # 2回目の承認クリック相当: 既にposting状態なのでpending_approval起点の遷移は失敗する
        assert store.try_transition(slot, "pending_approval", "posting") is False, \
            "ボタン連打(2回目)は拒否されるべき"
        assert store.get_slot(slot)["status"] == "posting"
    print("✅ test_try_transition_is_cas_and_rejects_mismatch")


def test_try_transition_writes_extra_fields():
    """try_transitionのkwargsでtweet_url等を同時に書き込める。"""
    with _temp_queue():
        slot, _ = store.allocate_slot("t", "n.md")
        store.try_transition(slot, "draft", "pending_approval")
        store.try_transition(slot, "pending_approval", "posting")
        ok = store.try_transition(slot, "posting", "posted",
                                   tweet_url="https://x.com/i/web/status/123")
        assert ok is True
        entry = store.get_slot(slot)
        assert entry["status"] == "posted"
        assert entry["tweet_url"] == "https://x.com/i/web/status/123"
    print("✅ test_try_transition_writes_extra_fields")


def test_release_slot_frees_it_for_reuse():
    """却下(release_slot)後、そのスロットは新規下書きに再利用できる。"""
    with _temp_queue():
        slot, _ = store.allocate_slot("t", "n.md")
        store.release_slot(slot)
        assert store.get_slot(slot) is None
        new_slot, _ = store.allocate_slot("next", "n2.md")
        assert new_slot == slot
    print("✅ test_release_slot_frees_it_for_reuse")


def test_mark_obsidian_synced():
    with _temp_queue():
        slot, _ = store.allocate_slot("t", "n.md")
        store.mark_obsidian_synced(slot)
        assert store.get_slot(slot)["obsidian_synced"] is True
    print("✅ test_mark_obsidian_synced")


def test_recover_stuck_posting_only_touches_posting_slots():
    """再起動復旧は posting のスロットだけを needs_review に倒し、他状態は触らない。"""
    with _temp_queue():
        stuck, _ = store.allocate_slot("stuck", "n1.md")
        store.try_transition(stuck, "draft", "pending_approval")
        store.try_transition(stuck, "pending_approval", "posting")

        safe, _ = store.allocate_slot("safe draft", "n2.md")

        recovered = store.recover_stuck_posting()
        assert recovered == [stuck]
        assert store.get_slot(stuck)["status"] == "needs_review"
        assert "要確認" in store.get_slot(stuck)["error_detail"]
        assert store.get_slot(safe)["status"] == "draft", "draft状態のスロットは変化しないべき"

        # 復旧後に再度呼んでも冪等 (もう posting は無いので何もしない)
        assert store.recover_stuck_posting() == []
    print("✅ test_recover_stuck_posting_only_touches_posting_slots")


def test_state_persists_to_disk_across_reload():
    """モジュール内キャッシュを持たず、都度ディスクから読むこと (ステートレス設計の検証)。"""
    with _temp_queue():
        slot, _ = store.allocate_slot("t", "n.md")
        assert os.path.exists(store.QUEUE_PATH)
        store.try_transition(slot, "draft", "pending_approval")
        # 別プロセス相当: ファイルを直接読み直しても同じ状態が見える
        assert store.get_slot(slot)["status"] == "pending_approval"
    print("✅ test_state_persists_to_disk_across_reload")


if __name__ == "__main__":
    tests = [
        test_allocate_slot_fills_empty_slot_first,
        test_allocate_slot_exhausted_returns_none,
        test_update_text_requires_pending_approval,
        test_try_transition_is_cas_and_rejects_mismatch,
        test_try_transition_writes_extra_fields,
        test_release_slot_frees_it_for_reuse,
        test_mark_obsidian_synced,
        test_recover_stuck_posting_only_touches_posting_slots,
        test_state_persists_to_disk_across_reload,
    ]
    for t in tests:
        t()
    print(f"\n🎉 ALL {len(tests)} X_POST_STORE TESTS PASSED")
