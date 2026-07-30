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
    """R1: agent_scheduler が PID ガードを lib.pid_guard に委譲していること。

    2026-07-30 に書き換えた。旧版は `agent_scheduler._check_existing_scheduler()` を
    直接呼んで stale/live/破損の3分岐を検査していたが、この関数は
    lib.pid_guard.acquire() (O_EXCL によるアトミック取得) への移行で削除されており、
    **移行以降このセキュリティ回帰テストはずっと AttributeError で死んでいた**。
    誰も sweep を回していなかったため気付けなかった (無言の死がテスト層で起きていた)。

    3分岐そのものは tests/test_pid_guard.py が本家として検査している
    (stale / live / 破損 / 前ブート / 並行取得の16ケース)。ここで見るべきは
    **agent_scheduler がそれを実際に使っているか** という配線であり、
    重複させても本家と一緒に腐るだけなので、配線だけを ast で検査する。
    """
    import ast
    import agent_scheduler as sched
    from lib import pid_guard

    # 委譲先の関数がガードとして機能すること (最小限の実挙動確認)
    probe = os.path.join(tempfile.gettempdir(), "test_scheduler_guard.pid")
    if os.path.exists(probe):
        os.remove(probe)
    try:
        assert pid_guard.acquire(probe), "空きロックを取得できるべき"
        assert not pid_guard.acquire(probe, pid=os.getpid() + 1, is_alive=lambda p: True), \
            "生存中の他インスタンスがいるなら取得を拒否するべき"
        assert pid_guard.release(probe), "所有者は release できるべき"
    finally:
        if os.path.exists(probe):
            os.remove(probe)

    # agent_scheduler 側の配線 — import 済みかつ main() から呼ばれているか
    assert hasattr(sched, "PID_FILE"), "agent_scheduler.PID_FILE が無い"
    src_path = os.path.join(_PROJECT_ROOT, "agent_scheduler.py")
    with open(src_path, encoding='utf-8') as f:
        tree = ast.parse(f.read(), src_path)

    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "lib.pid_guard"
        for alias in node.names
    }
    assert "acquire" in {a.split(".")[-1] for a in imported} or any(
        n.endswith("acquire_pid_lock") for n in imported
    ), f"agent_scheduler が lib.pid_guard.acquire を import していない ({imported})"

    called = {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert called & {n for n in imported}, \
        "import しただけで acquire/release を呼んでいない (ガードが無効化されている)"
    print("✅ R1: PID guard は lib.pid_guard に委譲され、agent_scheduler から呼ばれている")


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
