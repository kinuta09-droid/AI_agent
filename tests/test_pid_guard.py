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
import subprocess
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib import pid_guard
from lib.pid_guard import (
    acquire, release, read_owner, read_record, boot_id, is_previous_boot,
)

_ALWAYS_ALIVE = lambda pid: True
_ALWAYS_DEAD = lambda pid: False

# ブート ID を固定してテストする (実測値に依存させない)。
_BOOT_NOW = 1_000_000.0
_BOOT_PREV = _BOOT_NOW - 86_400   # 1日前に起動していた前ブート


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
# 再起動またぎ (PC 再起動後に Bot が上がらない障害の回帰テスト)
# ---------------------------------------------------------

def test_boot_id_is_stable_within_a_boot():
    """同一ブート中の boot_id は (実測でも) ほぼ一定であること。"""
    first = boot_id()
    assert first is not None, "Windows/Linux では取得できるはず"
    time.sleep(0.05)
    assert abs(boot_id() - first) < 1.0, "同一ブート中に大きく揺れてはならない"
    print("✅ pid_guard: boot_id は同一ブート中で安定")


def test_is_previous_boot_needs_both_values():
    """片方でも不明ならブート判定はしない (旧フォーマット互換の安全側)。"""
    assert is_previous_boot(None, _BOOT_NOW) is False
    assert is_previous_boot(_BOOT_PREV, None) is False
    assert is_previous_boot(_BOOT_NOW, _BOOT_NOW) is False, "同一ブートは前ブートでない"
    assert is_previous_boot(_BOOT_PREV, _BOOT_NOW) is True
    assert is_previous_boot(_BOOT_NOW - 30, _BOOT_NOW) is False, \
        "NTP 補正程度のずれを別ブートとみなしてはならない"
    print("✅ pid_guard: ブート判定は両値が揃ったときだけ / 許容幅内は同一ブート")


def test_acquire_records_boot_id():
    """取得時に PID と一緒にブート ID が記録される。"""
    with _TempLock() as lock:
        assert acquire(lock.path, pid=4242, is_alive=_ALWAYS_ALIVE,
                       boot=_BOOT_NOW) is True
        pid, boot = read_record(lock.path)
        assert pid == 4242 and boot == _BOOT_NOW
        assert read_owner(lock.path) == 4242, "旧 API も PID を返し続けること"
    print("✅ pid_guard: PID とブート ID を記録 (read_owner 互換維持)")


def test_previous_boot_lock_is_stale_even_if_pid_looks_alive():
    """本命: 電源断で残ったロックは、PID が再利用されていても引き継げること。

    再起動後は PID が別プロセスに割り当てられ、is_process_alive は安全側に
    倒れて「生存」と答える。その PID を信じると acquire が永久に False を返し、
    Bot が無言で起動しなくなる (PC 起動時に Bot が上がらない障害の正体)。
    """
    with _TempLock() as lock:
        # 前ブートの残骸: release されずに残った PID ファイル
        with open(lock.path, 'w', encoding='utf-8') as f:
            f.write(f"1234\n{_BOOT_PREV!r}")
        assert acquire(lock.path, pid=5678, is_alive=_ALWAYS_ALIVE,
                       boot=_BOOT_NOW) is True, \
            "前ブートのロックは PID の生死を問わず引き継げなければならない"
        assert read_record(lock.path) == (5678, _BOOT_NOW)
    print("✅ pid_guard: 前ブートの残骸ロックを引き継ぐ (PID 再利用の誤検知を回避)")


def test_same_boot_alive_owner_still_blocks():
    """ブート判定は重複起動ガードを緩めない: 同一ブートの生存所有者は勝ち続ける。"""
    with _TempLock() as lock:
        assert acquire(lock.path, pid=1111, is_alive=_ALWAYS_ALIVE,
                       boot=_BOOT_NOW) is True
        assert acquire(lock.path, pid=2222, is_alive=_ALWAYS_ALIVE,
                       boot=_BOOT_NOW) is False, \
            "同一ブートで所有者が生存中なら従来どおり取得を拒否"
        assert read_owner(lock.path) == 1111
    print("✅ pid_guard: 同一ブート・所有者生存時は従来どおり拒否 (ガードは緩まない)")


def test_same_pid_from_previous_boot_is_not_reentrant():
    """自分と同じ PID でも前ブートの記録なら「再入」ではなく stale。

    再起動後に同じ PID を引く可能性は十分あり、再入と誤認すると
    ロックを持たないまま True を返してしまう。
    """
    with _TempLock() as lock:
        with open(lock.path, 'w', encoding='utf-8') as f:
            f.write(f"4242\n{_BOOT_PREV!r}")
        assert acquire(lock.path, pid=4242, is_alive=_ALWAYS_ALIVE,
                       boot=_BOOT_NOW) is True
        pid, boot = read_record(lock.path)
        assert boot == _BOOT_NOW, "ブート ID が現在のものに更新されること"
    print("✅ pid_guard: 同一 PID でも前ブートの記録なら再入扱いしない")


def test_legacy_pid_file_without_boot_id_keeps_old_behavior():
    """ブート ID を持たない旧フォーマットは従来の生死判定にフォールバックする。"""
    with _TempLock() as lock:
        with open(lock.path, 'w', encoding='utf-8') as f:
            f.write("1234")  # 旧フォーマット (PID のみ)
        assert read_record(lock.path) == (1234, None)
        assert acquire(lock.path, pid=5678, is_alive=_ALWAYS_ALIVE,
                       boot=_BOOT_NOW) is False, "生存なら従来どおり拒否"
        assert acquire(lock.path, pid=5678, is_alive=_ALWAYS_DEAD,
                       boot=_BOOT_NOW) is True, "死亡なら従来どおり引き継ぐ"
    print("✅ pid_guard: 旧フォーマット (PID のみ) と後方互換")


def test_corrupt_boot_id_falls_back_to_liveness():
    """ブート ID が壊れていても PID 判定は生き続ける (ロックを失わない)。"""
    with _TempLock() as lock:
        with open(lock.path, 'w', encoding='utf-8') as f:
            f.write("1234\nこれは数値ではない")
        assert read_record(lock.path) == (1234, None)
        assert acquire(lock.path, pid=5678, is_alive=_ALWAYS_ALIVE,
                       boot=_BOOT_NOW) is False, \
            "ブート ID 破損を理由に生存所有者のロックを奪ってはならない"
    print("✅ pid_guard: ブート ID 破損時は PID の生死判定にフォールバック")


# ---------------------------------------------------------
# is_process_alive 本体 (ゾンビハンドル回帰テスト)
#
# 2026-08-03: main.py の Monitor が subprocess.Popen で起動した scheduler が
# 終了しても、親 (main.py) が Popen オブジェクト (=ハンドル) を保持し続ける
# 限り OpenProcess は成功し続けた。旧実装はハンドル取得の成否だけで生死を
# 判定していたため「生存」と誤判定し、acquire() が永久に False を返して
# scheduler が起動できないクラッシュループになった。
# ---------------------------------------------------------

def test_is_process_alive_true_for_running_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert pid_guard.is_process_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait()
    print("✅ pid_guard: 実行中プロセスは生存と判定")


def test_is_process_alive_detects_zombie_handle():
    """終了済みだが自プロセスがハンドルを保持したままの PID を正しく死亡と判定する。

    proc.wait() で確実に終了させた後も、proc (Popen オブジェクト) への参照は
    まだ生きているため、そのプロセスへの OpenProcess はハンドル取得だけなら
    成功し続ける (本番障害の再現条件)。GetExitCodeProcess で STILL_ACTIVE
    以外を確認できて初めて「死亡」と判定できることを検証する。
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert pid_guard.is_process_alive(proc.pid) is False, \
        "終了済みだが親がハンドルを保持中のプロセスを生存と誤判定してはならない"
    print("✅ pid_guard: ゾンビハンドル (終了済み・親がハンドル保持中) を正しく死亡判定")


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
    """stale 引き継ぎの競合 — 「削除 → 作成」の隙で二重取得しないこと。

    この不変条件は「開いているファイルは削除できない」という Windows の
    セマンティクスに依存する (pid_guard の docstring 参照)。POSIX は所有者が
    fd を開いたままでも remove が成功するため、この競合では複数が勝ちうる。
    本番環境は Windows なので、判定は Windows でのみ行う。
    """
    if os.name != 'nt':
        print("⏭️ pid_guard: stale 競合テストは Windows 専用のためスキップ")
        return
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


def test_is_process_alive_false_for_non_python_pid_reuse():
    """PID が無関係プロセスに再利用された場合は死亡扱い (2026-09-28 IME 誤kill)。"""
    proc = subprocess.Popen(["ping", "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL)
    try:
        assert pid_guard.is_process_alive(proc.pid) is False
    finally:
        proc.kill()
        proc.wait()
