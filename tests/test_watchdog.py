# -*- coding: utf-8 -*-
"""
lib/watchdog.py のユニットテスト (ハング検知の鮮度判定)

判定の核心は「生存証明 = max(heartbeat の mtime, プロセス起動時刻)」。
これにより (1) 起動直後の未beat期間、(2) 前回稼働の古い .hb 残留、の両方で
誤kill しないことを now 注入の純関数テストで保証する。

実行: py tests/test_watchdog.py
"""
import os
import sys
import shutil
import tempfile
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.watchdog import beat, age_seconds, is_stale, kill_process, STALE_THRESHOLD


class _TempHb:
    """テスト毎に独立した heartbeat ファイルパスを与え、後始末する。"""

    def __enter__(self):
        self._dir = tempfile.mkdtemp(prefix="watchdog_test_")
        self.path = os.path.join(self._dir, "test.hb")
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self._dir, ignore_errors=True)

    def set_mtime(self, ts):
        """heartbeat の最終更新時刻を偽装する。"""
        if not os.path.exists(self.path):
            beat(self.path)
        os.utime(self.path, (ts, ts))


def test_beat_writes_and_updates_mtime():
    """beat() は heartbeat ファイルを作成し、mtime を前進させる。"""
    with _TempHb() as hb:
        beat(hb.path)
        assert os.path.exists(hb.path), "beat がファイルを作成すること"
        hb.set_mtime(time.time() - 100)
        old = os.path.getmtime(hb.path)
        beat(hb.path)
        assert os.path.getmtime(hb.path) > old, "beat が mtime を更新すること"
    print("✅ watchdog: beat がファイル作成 + mtime 更新")


def test_fresh_beat_is_not_stale():
    """直近に beat していれば stale ではない。"""
    now = 10_000.0
    with _TempHb() as hb:
        hb.set_mtime(now - 5)
        assert not is_stale(hb.path, started_at=now - 3600, now=now)
    print("✅ watchdog: 新鮮な heartbeat は not stale")


def test_old_beat_is_stale():
    """閾値を超えて beat が止まっていれば stale。"""
    now = 10_000.0
    with _TempHb() as hb:
        hb.set_mtime(now - STALE_THRESHOLD - 1)
        assert is_stale(hb.path, started_at=now - 3600, now=now)
        assert not is_stale(hb.path, started_at=now - 3600, now=now,
                            threshold=STALE_THRESHOLD + 10), "閾値は引数で可変"
    print("✅ watchdog: 閾値超過の heartbeat は stale")


def test_missing_file_within_grace_is_not_stale():
    """ファイル未作成でも起動直後 (重量級 import 中) は猶予される。"""
    now = 10_000.0
    missing = os.path.join(tempfile.gettempdir(), "watchdog_test_no_such.hb")
    assert not is_stale(missing, started_at=now - 60, now=now), \
        "起動60秒後・未beat は猶予内"
    print("✅ watchdog: ファイル無し + 起動猶予内は not stale")


def test_missing_file_beyond_grace_is_stale():
    """起動から閾値を超えても一度も beat しない = イベントループが上がっていない。"""
    now = 10_000.0
    missing = os.path.join(tempfile.gettempdir(), "watchdog_test_no_such.hb")
    assert is_stale(missing, started_at=now - STALE_THRESHOLD - 1, now=now)
    print("✅ watchdog: ファイル無し + 猶予超過は stale")


def test_leftover_hb_from_previous_run_is_not_stale():
    """前回稼働の古い .hb が残っていても、新プロセス起動直後は誤killしない。

    max(mtime, started_at) を取らない実装 (mtime だけ見る) だとここで FAIL する。
    """
    now = 10_000.0
    with _TempHb() as hb:
        hb.set_mtime(now - 7200)  # 2時間前 = 前回稼働の残骸
        assert not is_stale(hb.path, started_at=now - 10, now=now), \
            "起動10秒後は古い残骸ではなく started_at 起点で判定すべき"
        assert age_seconds(hb.path, started_at=now - 10, now=now) == 10
    print("✅ watchdog: 古い .hb 残留 + 新プロセスは not stale (max() の検証)")


def test_kill_process_dead_pid_returns_false():
    """死亡済み PID への kill は False (WinError 87 を握り潰す既知バグパターン)。"""
    assert kill_process(999_999_999) is False
    assert kill_process(None) is False
    assert kill_process(0) is False, "PID 0 (Idle) を殺そうとしないこと"
    assert kill_process(-1) is False
    print("✅ watchdog: 死PID/無効PID への kill は False (OSError 捕捉)")


def test_beat_failure_is_swallowed():
    """書き込めないパスでも beat は例外を漏らさない (送出側ループを壊さない)。"""
    bad_path = os.path.join(tempfile.gettempdir(),
                            "watchdog_no_such_dir_xyz", "test.hb")
    beat(bad_path)  # 例外が飛ばなければ OK
    print("✅ watchdog: beat の書き込み失敗は握り潰される")


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
