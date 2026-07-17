# -*- coding: utf-8 -*-
"""
lib/file_lock.py + tool_crypto._update_trade_log のテスト:
- FileLock の相互排他 (並行スレッドで lost-update が起きないこと)
- タイムアウト時の FileLockTimeout 送出
- ロック保持中でも save_json (os.replace) が動くこと
- _update_trade_log 並行追記で全レコードが残ること
"""
import sys
import os
import json
import tempfile
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from lib.file_lock import FileLock, FileLockTimeout
from lib.lib_shared import load_json, save_json
import tools.tool_crypto as tc

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _tmp_json_path() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    os.unlink(tmp.name)
    return tmp.name


def _cleanup(path):
    for p in (path, path + ".lock"):
        if os.path.exists(p):
            os.unlink(p)


def test_basic_acquire_release():
    print("[Test] FileLock 基本動作")
    path = _tmp_json_path()
    try:
        with FileLock(path):
            pass
        # 解放後は即再取得できる
        with FileLock(path, timeout=1.0):
            check("解放後の再取得", True)

        # ロック保持中でも対象ファイルの save_json (os.replace) は成功する
        # (.lock を別ファイルにしている設計の検証)
        with FileLock(path):
            save_json(path, {"k": 1})
        check("ロック保持中の save_json", load_json(path) == {"k": 1})
    finally:
        _cleanup(path)


def test_timeout():
    print("[Test] FileLock タイムアウト")
    path = _tmp_json_path()
    try:
        holder = FileLock(path)
        holder.__enter__()
        try:
            raised = False
            try:
                with FileLock(path, timeout=0.4, poll_interval=0.05):
                    pass
            except FileLockTimeout:
                raised = True
            check("保持中の二重取得 → FileLockTimeout", raised)
        finally:
            holder.__exit__(None, None, None)

        # 解放後は取得できる
        with FileLock(path, timeout=1.0):
            check("解放後は取得成功", True)
    finally:
        _cleanup(path)


def test_no_lost_update():
    print("[Test] FileLock 並行 read-modify-write")
    path = _tmp_json_path()
    save_json(path, {"count": 0})
    N_THREADS, N_ITER = 8, 25

    def worker():
        for _ in range(N_ITER):
            with FileLock(path, timeout=30.0):
                data = load_json(path)
                data["count"] += 1
                save_json(path, data)

    try:
        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = load_json(path)["count"]
        check(f"lost-updateなし ({N_THREADS}スレッド×{N_ITER}回 = {final})",
              final == N_THREADS * N_ITER)
    finally:
        _cleanup(path)


def test_update_trade_log_concurrent():
    print("[Test] _update_trade_log 並行追記")
    path = _tmp_json_path()
    original = tc.TRADE_LOG_PATH
    tc.TRADE_LOG_PATH = path
    N_THREADS, N_ITER = 6, 20

    def worker(tid):
        for i in range(N_ITER):
            rec = {"id": f"{tid}-{i}", "status": "OPEN"}
            tc._update_trade_log(lambda logs, r=rec: logs.append(r))

    try:
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        logs = load_json(path, default=[])
        ids = {r["id"] for r in logs}
        check(f"全追記が残る ({len(logs)}件)", len(logs) == N_THREADS * N_ITER)
        check("id重複・欠落なし", len(ids) == N_THREADS * N_ITER)
    finally:
        tc.TRADE_LOG_PATH = original
        _cleanup(path)


if __name__ == "__main__":
    test_basic_acquire_release()
    test_timeout()
    test_no_lost_update()
    test_update_trade_log_concurrent()
    print(f"\n{'='*40}\nResult: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
