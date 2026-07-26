# -*- coding: utf-8 -*-
"""
lib/service_control.py のユニットテスト

主眼は「意図的な再起動」と「crash loop」を取り違えないこと:
  - フラグは1回だけ消費される (消し忘れると本物の起動失敗を見逃す)
  - 殺せなかった / 殺す必要がなかったときはフラグを残さない
  - スーパーバイザが居ないときは絶対に子を落とさない (落とすと復帰不能)

実行: py tests/test_service_control.py
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

from lib import service_control as sc
from lib import watchdog


class _Sandbox:
    """SERVICES / LAUNCHER_PID_FILE を tmpdir 上の定義に差し替えるコンテキスト。

    本物の PID ファイル (稼働中の Bot のロック) を実行中のテストが触ると、
    最悪ここから本番プロセスを殺しかねないので必ずこれを通す。
    """

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="svcctl_test_")
        self.logs = os.path.join(self.dir, "logs")
        os.makedirs(self.logs, exist_ok=True)
        self._orig = (sc.SERVICES, sc.LAUNCHER_PID_FILE, sc.LOG_DIR,
                      watchdog.kill_process, sc.RESTART_NOTICE_FILE)
        sc.SERVICES = {
            "discord_bot": {
                "label": "Discord Bot",
                "script": "bot_discord.py",
                "hb_file": os.path.join(self.logs, "bot_discord.hb"),
                "pid_file": os.path.join(self.dir, "bot_discord.pid"),
                "restart_flag": os.path.join(self.logs, "discord_bot.restart"),
            },
        }
        sc.LAUNCHER_PID_FILE = os.path.join(self.dir, "arms_launcher.pid")
        sc.LOG_DIR = self.logs
        sc.RESTART_NOTICE_FILE = os.path.join(self.logs, "restart_notice.json")
        self.killed = []
        return self

    def __exit__(self, *exc):
        (sc.SERVICES, sc.LAUNCHER_PID_FILE, sc.LOG_DIR, watchdog.kill_process,
         sc.RESTART_NOTICE_FILE) = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    # --- ヘルパー ---
    def write_pid(self, path, pid):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(str(pid))

    def launcher_up(self):
        self.write_pid(sc.LAUNCHER_PID_FILE, os.getpid())

    def service_up(self, pid=None):
        self.write_pid(sc.SERVICES["discord_bot"]["pid_file"], pid or os.getpid())

    def beat(self, age=0):
        hb = sc.SERVICES["discord_bot"]["hb_file"]
        with open(hb, 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
        if age:
            past = time.time() - age
            os.utime(hb, (past, past))

    def fake_kill(self, success=True):
        def _kill(pid):
            self.killed.append(pid)
            return success
        watchdog.kill_process = _kill

    def flag_exists(self):
        return os.path.exists(sc.SERVICES["discord_bot"]["restart_flag"])

    def notice_exists(self):
        return os.path.exists(sc.RESTART_NOTICE_FILE)


def test_resolve_aliases():
    assert sc.resolve("bot") == "discord_bot"
    assert sc.resolve("sched") == "scheduler"
    assert sc.resolve("scheduler") == "scheduler"
    print("✅ resolve: 人間向けの別名を内部名に正規化する")


def test_restart_flag_consumed_once():
    with _Sandbox() as box:
        assert sc.consume_restart_flag("bot") is False, "無いフラグを消費できてはいけない"
        assert sc.set_restart_flag("bot") is True
        assert box.flag_exists()
        assert sc.consume_restart_flag("bot") is True
        assert not box.flag_exists()
        assert sc.consume_restart_flag("bot") is False, "2回目は False (消費は1回だけ)"
    print("✅ restart flag: 1回だけ消費される")


def test_status_states():
    with _Sandbox() as box:
        st = sc.status("bot")
        assert st["state"] == "stopped" and st["pid"] is None, st

        box.service_up()
        box.beat()
        st = sc.status("bot")
        assert st["state"] == "running", st
        assert st["pid"] == os.getpid() and st["hb_age"] < 5, st

        box.beat(age=watchdog.STALE_THRESHOLD + 60)
        st = sc.status("bot")
        assert st["state"] == "stale", st
        print("✅ status: stopped / running / stale を heartbeat 鮮度で判定する")


def test_status_without_heartbeat_is_stale_not_crash():
    """heartbeat ファイルが1度も作られていなくても例外にせず stale と報告する。"""
    with _Sandbox() as box:
        box.service_up()
        st = sc.status("bot")
        assert st["hb_age"] is None and st["state"] == "stale", st
    print("✅ status: heartbeat 不在でも落ちず stale として報告する")


def test_request_restart_refuses_without_launcher():
    with _Sandbox() as box:
        box.service_up()
        box.beat()
        box.fake_kill()
        ok, msg = sc.request_restart("bot")
        assert ok is False, msg
        assert box.killed == [], "スーパーバイザ不在で kill してはいけない"
        assert not box.flag_exists(), "拒否したのにフラグを残してはいけない"
        assert "main.py" in msg
    print("✅ request_restart: スーパーバイザ不在なら子を落とさない")


def test_request_restart_kills_and_flags():
    with _Sandbox() as box:
        box.launcher_up()
        box.service_up()
        box.beat()
        box.fake_kill(success=True)
        ok, msg = sc.request_restart("bot")
        assert ok is True, msg
        assert box.killed == [os.getpid()], box.killed
        assert box.flag_exists(), "意図的な再起動の印が残っていない"
    print("✅ request_restart: フラグを立ててから実体を kill する")


def test_request_restart_cleans_flag_on_kill_failure():
    with _Sandbox() as box:
        box.launcher_up()
        box.service_up()
        box.beat()
        box.fake_kill(success=False)
        ok, _msg = sc.request_restart("bot")
        assert ok is False
        assert not box.flag_exists(), "kill 失敗のフラグが残ると次の異常終了を誤読する"
    print("✅ request_restart: kill 失敗時はフラグを残さない")


def test_request_restart_when_already_stopped():
    with _Sandbox() as box:
        box.launcher_up()
        box.fake_kill()
        ok, msg = sc.request_restart("bot")
        assert ok is True and box.killed == [], msg
        assert not box.flag_exists(), "停止済みならフラグ不要 (次の死亡を誤読させない)"
    print("✅ request_restart: 既に停止済みなら kill もフラグも行わない")


def test_restart_flag_expires():
    """置き去りのフラグが後日の本物のクラッシュを覆い隠さない。

    フラグを立てた直後に停止が失敗する経路 (Discord への応答送信エラー等) が
    あり、期限を持たないと「意図的な再起動」と誤読され続けて crash loop
    検知が無効化される。
    """
    with _Sandbox() as box:
        sc.set_restart_flag("bot")
        path = sc.SERVICES["discord_bot"]["restart_flag"]
        old = time.time() - sc.FLAG_MAX_AGE - 60
        os.utime(path, (old, old))
        assert sc.consume_restart_flag("bot") is False, "期限切れは意図的扱いにしない"
        assert not box.flag_exists(), "期限切れでもフラグは片付ける"
    print("✅ restart flag: 期限切れは意図的扱いにせず片付ける")


def test_restart_notice_round_trip_and_single_consumption():
    """完了報告の宛先は1回だけ読める。

    on_ready は再接続のたびに発火するので、消さずに読むと
    「問題なく再起動しました」が何度も投稿されてしまう。
    """
    with _Sandbox() as box:
        assert sc.pop_restart_notice() is None, "書き置きが無ければ None"
        assert sc.write_restart_notice(123456789, "bot") is True
        assert box.notice_exists()

        notice = sc.pop_restart_notice()
        assert notice is not None
        assert notice["channel_id"] == 123456789 and notice["target"] == "bot"
        assert 0 <= notice["elapsed"] < 5, notice["elapsed"]
        assert not box.notice_exists(), "読んだら消えていること"
        assert sc.pop_restart_notice() is None, "2回目は None (多重投稿を防ぐ)"
    print("✅ restart notice: 書いて読んで消える (消費は1回だけ)")


def test_restart_notice_expires():
    """電源断などで何時間も後に起動したら、今さら完了報告はしない。"""
    with _Sandbox() as box:
        sc.write_restart_notice(1, "bot",
                                requested_at=time.time() - sc.NOTICE_MAX_AGE - 60)
        assert sc.pop_restart_notice() is None, "期限切れは None"
        assert not box.notice_exists(), "期限切れでもファイルは片付ける"
    print("✅ restart notice: 期限切れは投稿せず片付ける")


def test_restart_notice_broken_file_is_discarded():
    with _Sandbox() as box:
        with open(sc.RESTART_NOTICE_FILE, 'w', encoding='utf-8') as f:
            f.write("{ not json")
        assert sc.pop_restart_notice() is None
        assert not box.notice_exists(), "壊れた書き置きを残すと毎回読み直してしまう"
    print("✅ restart notice: 壊れた書き置きは捨てる")


def test_format_status_lines_warns_when_launcher_down():
    with _Sandbox() as box:
        lines = sc.format_status_lines()
        assert any("not running" in ln for ln in lines), lines
        box.launcher_up()
        box.service_up()
        box.beat()
        lines = sc.format_status_lines()
        assert lines[0].startswith("🟢"), lines
        assert any("running" in ln and "PID" in ln for ln in lines[1:]), lines
    print("✅ format_status_lines: launcher 不在を最初の行で警告する")


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
