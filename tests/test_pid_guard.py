# -*- coding: utf-8 -*-
"""
lib/pid_guard.py のユニットテスト (重複起動ガードのアトミック性)

背景: 2026-07-17、Discord Bot が2重にゲートウェイ接続し、1回の /agent が
2本の実行として並走して logs/skill_observations.jsonl に同一秒の重複記録を
生んだ。原因は旧ガードの「存在チェック → 書き込み」が非アトミックだったこと。
本テストの主眼は test_concurrent_* の2件で、殺到しても勝者が必ず1つに
なることを保証する。

実行: py tests/test_pid_guard.py
"""
import os
import sys
import shutil
import tempfile
import threading

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib import pid_guard
from lib.pid_guard import acquire, release, read_owner

_ALWAYS_ALIVE = lambda pid: True
_ALWAYS_DEAD = lambda pid: False


class _TempLock:
    """テスト毎に独立した PID ファイルパスを与え、後始末する。"""

    def __enter__(self):
        self._dir = tempfile.mkdtemp(prefix="pidguard_test_")
        self.path = os.path.join(self._dir, "test.pid")
        return self

    def __exit__(self, *exc):
        # 保持中の fd を閉じないと Windows ではディレクトリごと消せない
        fd = pid_guard._HELD.pop(os.path.abspath(self.path), None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(self._dir, ignore_errors=True)


# ---------------------------------------------------------
# 基本動作
# ---------------------------------------------------------

def test_acquire_on_clean_path():
    with _TempLock() as lock:
        assert acquire(lock.path, pid=4242, is_alive=_ALWAYS_ALIVE) is True
        assert read_owner(lock.path) == 4242, "取得後は自分の PID が記録される"
    print("✅ pid_guard: 空きロックを取得して PID を記録")


def test_acquire_is_reentrant_for_same_pid():
    with _TempLock() as lock:
        assert acquire(lock.path, pid=4242, is_alive=_ALWAYS_ALIVE) is True
        # 自分自身の PID を見つけたら「他インスタンス」とみなしてはならない
        assert acquire(lock.path, pid=4242, is_alive=_ALWAYS_ALIVE) is True
    print("✅ pid_guard: 自分が所有者なら再取得できる (再入)")


def test_acquire_rejected_when_owner_alive():
    with _TempLock() as lock:
        assert acquire(lock.path, pid=1111, is_alive=_ALWAYS_ALIVE) is True
        assert acquire(lock.path, pid=2222, is_alive=_ALWAYS_ALIVE) is False, \
            "生存インスタンスがいる間は取得できてはならない"
        assert read_owner(lock.path) == 1111, "敗者は所有者の PID を書き換えない"
    print("✅ pid_guard: 所有者が生存中なら取得を拒否")


def test_acquire_takes_over_stale_lock():
    with _TempLock() as lock:
        with open(lock.path, 'w', encoding='utf-8') as f:
            f.write("999999")  # 既に死んでいるプロセスの PID
        assert acquire(lock.path, pid=2222, is_alive=_ALWAYS_DEAD) is True, \
            "死んだプロセスのロックは引き継げなければならない"
        assert read_owner(lock.path) == 2222
    print("✅ pid_guard: stale ロック (プロセス死亡) を引き継ぐ")


def test_acquire_takes_over_corrupt_lock():
    with _TempLock() as lock:
        with open(lock.path, 'w', encoding='utf-8') as f:
            f.write("これはPIDではない")
        assert read_owner(lock.path) is None, "数字でない内容は None"
        assert acquire(lock.path, pid=2222, is_alive=_ALWAYS_ALIVE) is True, \
            "破損ロックは回復不能なので引き継ぐ"
        assert read_owner(lock.path) == 2222
    print("✅ pid_guard: 破損したロックファイルを引き継ぐ")


def test_release_only_by_owner():
    with _TempLock() as lock:
        assert acquire(lock.path, pid=1111, is_alive=_ALWAYS_ALIVE) is True
        # 他人のロックを消すと次回起動でガードが素通りしてしまう
        assert release(lock.path, pid=2222) is False, "非所有者は release できない"
        assert os.path.exists(lock.path), "非所有者の release でファイルが消えてはならない"
        assert release(lock.path, pid=1111) is True
        assert not os.path.exists(lock.path), "所有者の release でファイルが消える"
    print("✅ pid_guard: release は所有者のみ (他人のロックを消さない)")


def test_release_allows_next_acquire():
    with _TempLock() as lock:
        assert acquire(lock.path, pid=1111, is_alive=_ALWAYS_ALIVE) is True
        assert release(lock.path, pid=1111) is True
        # release 後に fd が残っていると Windows で次の取得が失敗する
        assert acquire(lock.path, pid=2222, is_alive=_ALWAYS_ALIVE) is True, \
            "release 後は次のインスタンスが取得できる"
    print("✅ pid_guard: release 後に次のインスタンスが取得できる")


# ---------------------------------------------------------
# 本命: 並行取得で勝者が1つであること (旧実装の TOCTOU 回帰テスト)
# ---------------------------------------------------------

def _race(lock_path, pids, is_alive):
    """全スレッドを Barrier で揃えて一斉に acquire させ、結果を返す。"""
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(pids))

    def worker(pid):
        barrier.wait()  # 取得タイミングを最大限ぶつける
        got = acquire(lock_path, pid=pid, is_alive=is_alive)
        with lock:
            results.append((pid, got))

    threads = [threading.Thread(target=worker, args=(p,)) for p in pids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in threads), "acquire がハングした"
    return results


def test_concurrent_acquire_has_exactly_one_winner():
    """空きロックに同時殺到 — 旧実装はここで複数プロセスが勝ってしまった。"""
    pids = list(range(9001, 9009))
    with _TempLock() as lock:
        results = _race(lock.path, pids, _ALWAYS_ALIVE)
        winners = [pid for pid, got in results if got]
        assert len(results) == len(pids), "全スレッドが結果を返すこと"
        assert len(winners) == 1, f"勝者はちょうど1つであるべき: {winners}"
        assert read_owner(lock.path) == winners[0], "ロックの中身は勝者の PID"
    print(f"✅ pid_guard: {len(pids)}並行取得の勝者はちょうど1つ (TOCTOU 回帰)")


def test_concurrent_acquire_over_stale_lock_has_one_winner():
    """stale 引き継ぎの競合 — 「削除 → 作成」の隙で二重取得しないこと。"""
    dead_pid = 999999
    pids = list(range(9101, 9109))
    with _TempLock() as lock:
        with open(lock.path, 'w', encoding='utf-8') as f:
            f.write(str(dead_pid))
        results = _race(lock.path, pids, lambda p: p != dead_pid)
        winners = [pid for pid, got in results if got]
        assert len(winners) == 1, f"stale 引き継ぎでも勝者は1つ: {winners}"
        assert read_owner(lock.path) == winners[0]
    print(f"✅ pid_guard: stale ロックへの{len(pids)}並行殺到でも勝者は1つ")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
