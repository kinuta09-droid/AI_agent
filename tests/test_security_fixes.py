# -*- coding: utf-8 -*-
"""
セキュリティ・堅牢性修正の回帰テスト (2026-07-15)

対象:
  S2: tool_browser_use の URL スキーム検証
  S3: tool_obsidian の移動先パスサニタイズ
  R1: agent_scheduler の PID ガード (stale/live/破損の3分岐)
  R2: agent_scheduler のタスクロック (多重実行スキップ)

実行: py tests/test_security_fixes.py
"""
import os
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def test_url_scheme_validation():
    from tools.tool_browser_use import _validate_url_scheme
    for bad in ["file:///C:/Windows/win.ini", "javascript:alert(1)", "ftp://x/y", "about:blank", "example.com"]:
        try:
            _validate_url_scheme(bad)
            raise AssertionError(f"should reject: {bad}")
        except ValueError:
            pass
    for good in ["https://example.com", "http://localhost:8080/x", "HTTPS://EXAMPLE.COM"]:
        _validate_url_scheme(good)
    print("✅ S2: URL scheme validation")


def test_vault_path_sanitize():
    from tools.tool_obsidian import _sanitize_vault_rel_path
    cases = {
        '30_Main/note.md': '30_Main/note.md',
        '../../../Windows/evil.md': 'Windows/evil.md',
        '30_Main/a" & calc & ".md': '30_Main/a_ & calc & _.md',
        'a|b:c<d>.md': 'a_b_c_d_.md',
        '..': '',
    }
    for src, expect in cases.items():
        got = _sanitize_vault_rel_path(src)
        assert got == expect, f"{src!r}: expected {expect!r}, got {got!r}"
    print("✅ S3: vault path sanitize")


def test_pid_guard():
    import agent_scheduler as sched
    test_pid = os.path.join(tempfile.gettempdir(), "test_scheduler_guard.pid")
    orig = sched.PID_FILE
    try:
        sched.PID_FILE = test_pid

        # 生存 PID (自分以外) → exit しつつ PID ファイルは残る
        # ※ 自プロセス PID は「自分自身なら素通し」の早期 return に入るため親 PID を使う
        with open(test_pid, 'w', encoding='utf-8') as f:
            f.write(str(os.getppid()))
        try:
            sched._check_existing_scheduler()
            raise AssertionError("should exit for live PID")
        except SystemExit:
            assert os.path.exists(test_pid), "live PID file must survive"

        # 死んだ PID → ファイル削除して続行
        with open(test_pid, 'w', encoding='utf-8') as f:
            f.write("999999")
        sched._check_existing_scheduler()
        assert not os.path.exists(test_pid), "stale PID file must be removed"

        # 破損ファイル → 削除して続行
        with open(test_pid, 'w', encoding='utf-8') as f:
            f.write("not-a-pid")
        sched._check_existing_scheduler()
        assert not os.path.exists(test_pid), "corrupt PID file must be removed"
    finally:
        sched.PID_FILE = orig
        if os.path.exists(test_pid):
            os.remove(test_pid)
    print("✅ R1: PID guard branches")


def test_task_lock_skip():
    import agent_scheduler as sched
    orig_notify = sched.send_discord_notification
    sched.send_discord_notification = lambda msg: None
    try:
        ran = []

        def slow():
            ran.append(1)
            time.sleep(1.0)
            return "done"

        task = {"name": "TestTask", "func": slow, "_lock": threading.Lock()}
        t1 = threading.Thread(target=sched._execute_task, args=(task,))
        t2 = threading.Thread(target=sched._execute_task, args=(task,))
        t1.start()
        time.sleep(0.2)
        t2.start()
        t1.join()
        t2.join()
        assert len(ran) == 1, f"expected 1 run, got {len(ran)}"
    finally:
        sched.send_discord_notification = orig_notify
    print("✅ R2: task lock skips concurrent re-trigger")


if __name__ == "__main__":
    test_url_scheme_validation()
    test_vault_path_sanitize()
    test_pid_guard()
    test_task_lock_skip()
    print("\n🎉 ALL SECURITY REGRESSION TESTS PASSED")
