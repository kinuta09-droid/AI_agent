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

from lib.watchdog import (
    beat, age_seconds, is_stale, kill_process, STALE_THRESHOLD,
    is_fast_exit, restart_delay, should_alert,
    FAST_EXIT_SECONDS, RESTART_DELAY, MAX_RESTART_DELAY, ALERT_AFTER_FAILURES,
)


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


def test_kill_process_falls_back_on_permission_error():
    """os.kill が WinError 5 で失敗しても PROCESS_TERMINATE で開き直す。

    CPython の os.kill は OpenProcess(PROCESS_ALL_ACCESS) を要求するため、
    タスクスケジューラが /rl highest で起動したスタックを非管理者の
    scripts/arms_ctl.py から停止しようとすると権限エラーになる。
    終了に必要なのは PROCESS_TERMINATE だけなので、そちらで再挑戦する。
    """
    import lib.watchdog as wd
    orig_kill, orig_fallback = os.kill, wd._terminate_via_handle
    calls = []
    try:
        def _denied(pid, sig):
            raise PermissionError(5, "アクセスが拒否されました。")
        os.kill = _denied
        wd._terminate_via_handle = lambda pid: (calls.append(pid), True)[1]
        assert wd.kill_process(4321) is True
        assert calls == [4321], calls

        wd._terminate_via_handle = lambda pid: False
        assert wd.kill_process(4321) is False, "代替経路も失敗したら False"
    finally:
        os.kill, wd._terminate_via_handle = orig_kill, orig_fallback
    print("✅ watchdog: 権限エラー時は PROCESS_TERMINATE で終了し直す")


def test_beat_creates_missing_parent_dir():
    """logs/ が無い環境でも beat が親ディレクトリごと作成する。

    自動作成しない実装だと heartbeat が永久に書かれず、watchdog が健全な
    プロセスを300秒毎に殺し続ける無限再起動ループになる (grill で検出)。
    """
    base = tempfile.mkdtemp(prefix="watchdog_test_")
    try:
        hb = os.path.join(base, "logs", "test.hb")
        beat(hb)
        assert os.path.exists(hb), "親ディレクトリごと作成されるべき"
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("✅ watchdog: 親ディレクトリ欠落時は beat が自動作成")


def test_beat_failure_is_swallowed():
    """書き込めないパスでも beat は例外を漏らさない (送出側ループを壊さない)。"""
    # ディレクトリは自動作成されるため、「ファイル名の位置に既にディレクトリが
    # ある」という作成不能ケースで失敗させる
    base = tempfile.mkdtemp(prefix="watchdog_test_")
    try:
        os.makedirs(os.path.join(base, "test.hb"))
        beat(os.path.join(base, "test.hb"))  # 例外が飛ばなければ OK
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("✅ watchdog: beat の書き込み失敗は握り潰される")


# ---------------------------------------------------------
# 起動失敗 (crash loop) の検知 — heartbeat の守備範囲外だった領域
# ---------------------------------------------------------

def test_fast_exit_classification():
    """起動直後の終了だけを「起動失敗」と数える。"""
    assert is_fast_exit(0) is True, "即 exit は起動失敗"
    assert is_fast_exit(FAST_EXIT_SECONDS - 1) is True
    assert is_fast_exit(FAST_EXIT_SECONDS) is False, "閾値ちょうどは失敗ではない"
    # watchdog kill は stale 判定 (300秒) を経るので必ず猶予より長く生きている
    assert is_fast_exit(STALE_THRESHOLD) is False, \
        "ハング kill による終了を起動失敗と数えてはならない"
    print("✅ watchdog: 起動直後の終了だけを起動失敗として分類")


def test_restart_delay_backs_off_and_caps():
    """連続失敗で待ち時間が倍化し、上限で頭打ちになる。"""
    assert restart_delay(0) == RESTART_DELAY, "正常稼働後の再起動は基準値のまま"
    assert restart_delay(1) == RESTART_DELAY, "1回目は即座に再試行 (一時障害の想定)"
    assert restart_delay(2) == RESTART_DELAY * 2
    assert restart_delay(3) == RESTART_DELAY * 4
    assert restart_delay(99) == MAX_RESTART_DELAY, "上限で頭打ち"
    # 上限が有限であること = 回復可能な原因 (ブート直後のネット未接続) から
    # 自力で復帰できること
    assert MAX_RESTART_DELAY < float('inf')
    print("✅ watchdog: 再起動間隔は指数バックオフし上限で頭打ち")


def test_should_alert_is_quiet_then_periodic():
    """1〜2回は黙って再試行し、閾値到達で通知、以降は間引いて再通知。"""
    for n in range(ALERT_AFTER_FAILURES):
        assert should_alert(n) is False, f"{n}回目で通知してはならない"
    assert should_alert(ALERT_AFTER_FAILURES) is True, "閾値到達で通知"
    assert should_alert(ALERT_AFTER_FAILURES + 1) is False, "毎回鳴らさない"
    assert should_alert(ALERT_AFTER_FAILURES + 10) is True, "一定間隔で再通知"
    print("✅ watchdog: 起動失敗の通知は閾値到達時と一定間隔のみ")


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
