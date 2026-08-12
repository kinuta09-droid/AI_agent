# -*- coding: utf-8 -*-
"""起動時 catch-up の判定ロジック検査 (日次ジョブ対応 + ハンドラ二重登録ガード)

背景 (2026-08-12 の実障害):
  Windows Update (TrustedInstaller) が 05:00 に無人で再起動し、ログオンまで
  7時間 ARMS が不在だった。scheduler のメインループは
  `current_time == task["time"]` の完全一致でしか発火しないため、
  06:00〜08:00 の生成系ジョブ (Daily Note / Morning Briefing / Daily News 等) が
  例外もログも残さずその日の分を丸ごと落とした。
  catch_up は当時 weekday 必須 = 週次ジョブ専用で、日次は対象外だった。

このテストが守る不変条件:
  [1] weekday=None (日次) で「直近の HH:MM」が求まる
  [2] weekday 指定 (週次) の既存挙動が変わっていない
  [3] 障害当日のシナリオで「発火すべき」と判定される
  [4] 同日中の再起動では二重実行しない
  [5] 朝の生成系ジョブに catch_up が付いている / 相場系には付いていない
  [6] agent_scheduler を再 import してもログハンドラが増えない
  [7] 「今の分」は catch-up 対象にしない (メインループとの二重発火防止)
  [8] catch-up の逐次実行は1ジョブの例外で打ち切られない

実行: py tests/test_scheduler_catch_up.py
"""
import datetime
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import agent_scheduler

_FAIL = False


def check(label, ok, detail=""):
    global _FAIL
    if ok:
        print(f"✅ {label}")
    else:
        _FAIL = True
        print(f"❌ FAILED: {label}" + (f" — {detail}" if detail else ""))


def _dt(s):
    return datetime.datetime.fromisoformat(s)


def test_daily_occurrence():
    """[1] weekday=None は「直近の HH:MM」を返す。"""
    occ = agent_scheduler._last_scheduled_occurrence
    # 12:05 の時点では今日の 07:00 が直近
    check("[1a] 予定時刻を過ぎていれば当日を返す",
          occ(_dt("2026-08-12T12:05:00"), None, 7, 0) == _dt("2026-08-12T07:00:00"))
    # 06:00 の時点ではまだ今日の 07:00 は来ていないので前日
    check("[1b] 予定時刻より前なら前日を返す",
          occ(_dt("2026-08-12T06:00:00"), None, 7, 0) == _dt("2026-08-11T07:00:00"))
    # ちょうど同時刻は「その回は既に来ている」扱い (当日)
    check("[1c] 同時刻ちょうどは当日扱い",
          occ(_dt("2026-08-12T07:00:00"), None, 7, 0) == _dt("2026-08-12T07:00:00"))
    # 日付をまたぐ深夜ジョブ
    check("[1d] 深夜ジョブが日をまたいでも正しい",
          occ(_dt("2026-08-12T00:30:00"), None, 23, 30) == _dt("2026-08-11T23:30:00"))


def test_weekly_occurrence_unchanged():
    """[2] weekday 指定の既存挙動を壊していない。"""
    occ = agent_scheduler._last_scheduled_occurrence
    # 2026-08-12 は水曜 (weekday=2)。日曜=6 の 23:30 の直近は 08-09。
    check("[2a] 週次は直前のその曜日を返す",
          occ(_dt("2026-08-12T12:05:00"), 6, 23, 30) == _dt("2026-08-09T23:30:00"))
    # 当日が該当曜日でも、予定時刻前なら1週間前
    check("[2b] 該当曜日でも予定時刻前なら1週間前",
          occ(_dt("2026-08-09T10:00:00"), 6, 23, 30) == _dt("2026-08-02T23:30:00"))
    check("[2c] 該当曜日で予定時刻後なら当日",
          occ(_dt("2026-08-09T23:59:00"), 6, 23, 30) == _dt("2026-08-09T23:30:00"))


def test_outage_scenario():
    """[3][4] 実障害の再現と、同日再起動での二重実行防止。"""
    occ = agent_scheduler._last_scheduled_occurrence
    # 08-12 12:05 に起動。Daily Note (07:00) の最終成功は前日 07:00。
    now = _dt("2026-08-12T12:05:00")
    occurrence = occ(now, None, 7, 0)
    last_run = _dt("2026-08-11T07:00:00")
    check("[3] 朝を落とした日は発火する (last_run < occurrence)",
          last_run < occurrence,
          f"occurrence={occurrence} last_run={last_run}")

    # 同じ日に 07:00 の分が走った後で 15:00 に再起動した場合は発火しない
    last_run_ok = _dt("2026-08-12T07:00:03")
    check("[4a] 当日実行済みなら発火しない",
          not (last_run_ok < occ(_dt("2026-08-12T15:00:00"), None, 7, 0)))
    # 予定時刻より前 (06:00) の再起動でも、前日分は済んでいるので発火しない
    check("[4b] 予定時刻前の再起動では発火しない",
          not (_dt("2026-08-11T07:00:03") < occ(_dt("2026-08-12T06:00:00"), None, 7, 0)))


def test_schedule_flags():
    """[5] 朝の生成系に catch_up が付き、相場系には付いていない。

    相場系に付けてはいけない理由: 遅れて実行すると古い値動きを「今の分析」として
    出す = 無いより有害。ここを緩めるときは必ずその判断を上書きする根拠を書くこと。
    """
    by_name = {t["name"]: t for t in agent_scheduler.SCHEDULE}

    must_have = [
        "Daily Note Generation", "X Trend Update", "Inbox Clip Auto-Tagging",
        "Morning Briefing", "Auto Deep-Dive (XTrend TOP1)", "Paper Radar",
        "Daily News",
    ]
    for name in must_have:
        task = by_name.get(name)
        if task is None:
            check(f"[5a] {name} が SCHEDULE に存在する", False, "見つからない")
            continue
        check(f"[5a] {name} に catch_up が付いている", task.get("catch_up") is True)

    must_not_have = ["BTC Analysis", "Crypto Market Scan", "Crypto Sweep"]
    for name in must_not_have:
        task = by_name.get(name)
        if task is None:
            continue  # 名前が変わった/消えた場合はここでは判定しない
        check(f"[5b] {name} には catch_up を付けない (古い相場を新しく見せない)",
              task.get("catch_up") is not True)

    # catch_up は HH:MM 発火が前提。every_minutes のジョブに付いていたら
    # _run_catch_up が warning でスキップするだけなので、宣言側で止める。
    for task in agent_scheduler.SCHEDULE:
        if task.get("catch_up"):
            check(f"[5c] {task['name']} は time を持つ", task.get("time") is not None)


def test_logger_handler_guard():
    """[6] 再 import してもハンドラが増えない (全ログ2重出力の再発防止)。"""
    before = len(agent_scheduler.logger.handlers)
    importlib.reload(agent_scheduler)
    after = len(agent_scheduler.logger.handlers)
    check("[6] reload でハンドラが増えない", before == after,
          f"before={before} after={after}")


def _drive_catch_up(schedule, state, execute):
    """SCHEDULE / 状態ファイル / _execute_task を差し替えて _run_catch_up を1回回す。

    状態ファイルは必ず一時ディレクトリへ逃がす。本番 logs/scheduler_job_state.json を
    書き換えると、次の実機起動で「実行済み」と誤認して朝の分を落とす
    (Known Bug Patterns C の資源汚染そのもの)。
    """
    tmp = tempfile.mkdtemp(prefix="catch_up_test_")
    orig = (agent_scheduler.SCHEDULE, agent_scheduler.JOB_STATE_FILE,
            agent_scheduler._execute_task)
    try:
        state_path = os.path.join(tmp, "scheduler_job_state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

        agent_scheduler.SCHEDULE = schedule
        agent_scheduler.JOB_STATE_FILE = state_path
        agent_scheduler._execute_task = execute

        before = set(threading.enumerate())
        agent_scheduler._run_catch_up()
        for t in threading.enumerate():
            if t not in before:
                t.join(timeout=10)
    finally:
        (agent_scheduler.SCHEDULE, agent_scheduler.JOB_STATE_FILE,
         agent_scheduler._execute_task) = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_skips_current_minute():
    """[7] 「今の分」は catch-up せず、メインループの定時発火に任せる。

    run_scheduler は last_run_minute="" で始まるためメインループの1周目が同じ分の
    ジョブを発火させる。catch-up は逐次実行で数分遅れて到達しうるので、ここで
    除外しないとロック解放後に2回目が走る (Gemini 二重課金 + 重複ノート)。
    """
    now = datetime.datetime.now()
    stale = (now - datetime.timedelta(days=3)).isoformat()
    schedule = [
        # 今この分に予定されている = 除外されるべき
        {"time": now.strftime("%H:%M"), "name": "NowJob",
         "func": lambda: "", "catch_up": True},
        # 1時間前に予定されていた = 取りこぼしとして発火すべき (対照)
        {"time": (now - datetime.timedelta(hours=1)).strftime("%H:%M"),
         "name": "PastJob", "func": lambda: "", "catch_up": True},
    ]
    ran = []
    _drive_catch_up(schedule, {"NowJob": stale, "PastJob": stale},
                    lambda task: ran.append(task["name"]))

    check("[7a] 今の分のジョブは catch-up しない", "NowJob" not in ran, f"ran={ran}")
    check("[7b] 過去の取りこぼしは発火する (検査が空振りでない証明)",
          "PastJob" in ran, f"ran={ran}")


def test_sequential_isolates_failures():
    """[8] 逐次実行は1ジョブの例外で残りを落とさない。

    並列(1ジョブ1スレッド)を逐次に変えた副作用。_execute_task は task["func"] の
    例外は自分で捕まえるが、その外側 (開始通知の送信・_lock 未初期化) には
    except が無く、素通しすると残りの catch-up ジョブが無言で消える。
    """
    now = datetime.datetime.now()
    stale = (now - datetime.timedelta(days=3)).isoformat()
    schedule = [
        {"time": (now - datetime.timedelta(hours=3)).strftime("%H:%M"),
         "name": "Boom", "func": lambda: "", "catch_up": True},
        {"time": (now - datetime.timedelta(hours=2)).strftime("%H:%M"),
         "name": "After", "func": lambda: "", "catch_up": True},
    ]
    ran = []

    def execute(task):
        if task["name"] == "Boom":
            raise RuntimeError("simulated failure outside task func")
        ran.append(task["name"])

    _drive_catch_up(schedule, {"Boom": stale, "After": stale}, execute)
    check("[8] 先行ジョブが例外でも後続は実行される", "After" in ran, f"ran={ran}")


def main():
    test_daily_occurrence()
    test_weekly_occurrence_unchanged()
    test_outage_scenario()
    test_schedule_flags()
    test_logger_handler_guard()
    test_skips_current_minute()
    test_sequential_isolates_failures()
    print("\n" + ("❌ 失敗あり" if _FAIL else "✅ 全て成功"))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
