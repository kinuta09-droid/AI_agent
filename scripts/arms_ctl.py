# -*- coding: utf-8 -*-
"""
scripts/arms_ctl.py — ARMS スタックのワンクリック制御 (start / stop / restart / status / logs)

なぜ .bat ではなく Python なのか:
  判定ロジック (PID の生死・heartbeat の鮮度) は lib/service_control.py に既にあり、
  Discord の /status と同じ表示を再利用したい。cmd.exe で書き直すと表示が二重管理に
  なるうえ、日本語のクォート/文字化けで壊れやすい。.bat は薄いランチャーに徹する。

なぜ start が schtasks 経由なのか:
  自動起動 (タスクスケジューラ) と経路を一本化するため。同じ start_arms.bat が走り、
  ログも logs\\launcher.log に揃うので「手で起動したときだけ挙動が違う」を作らない。
  タスク未登録なら警告して直接起動にフォールバックする (2026-07-25 に実際に
  タスクが未登録で Bot が上がらなかったので、その状態を検知できるようにしてある)。

使い方:
    py scripts/arms_ctl.py status
    py scripts/arms_ctl.py start | stop | restart | logs
"""
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib import service_control as sc

TASK_NAME = "ARMS Launcher"
START_BAT = os.path.join(_PROJECT_ROOT, "scripts", "start_arms.bat")
LAUNCHER_LOG = os.path.join(_PROJECT_ROOT, "logs", "launcher.log")

# stop の完了待ち (kill は非同期なので、消えるまで見てから次へ進む)
_STOP_TIMEOUT = 15
_POLL_INTERVAL = 0.5


def _run(cmd):
    """外部コマンドを実行して (returncode, 出力) を返す。

    encoding/errors を明示するのは Windows 日本語環境で schtasks の出力が
    cp932 になり、UnicodeDecodeError で制御スクリプトごと落ちるのを防ぐため。
    """
    proc = subprocess.run(cmd, capture_output=True, encoding='cp932',
                          errors='replace')
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def cmd_status():
    for line in sc.format_status_lines():
        print("  " + line)
    return 0


def _task_registered():
    code, _out = _run(["schtasks", "/query", "/tn", TASK_NAME])
    return code == 0


def cmd_start():
    if sc.launcher_alive():
        print(f"  ℹ️ 既に起動しています (Launcher PID {sc.launcher_status()['pid']})")
        return cmd_status()

    if _task_registered():
        code, out = _run(["schtasks", "/run", "/tn", TASK_NAME])
        if code == 0:
            print(f'  🚀 タスク "{TASK_NAME}" を実行しました')
        else:
            print(f"  ❌ タスクの実行に失敗しました: {out.strip()}")
            return 1
    else:
        # タスクが無い = PC 起動時に自動で立ち上がらない状態。起動はさせるが、
        # 黙って代替経路を使うと「自動起動が壊れている」ことに気づけない。
        print(f'  ⚠️ タスクスケジューラに "{TASK_NAME}" が登録されていません。')
        print("     → PC 起動時の自動起動が効きません。scripts\\AUTOSTART.md の")
        print("        schtasks /create ... を管理者 PowerShell で1回実行してください。")
        print("  🚀 今回は start_arms.bat を直接起動します。")
        # DETACHED_PROCESS: この端末を閉じてもスタックが道連れにならないようにする
        subprocess.Popen(["cmd", "/c", START_BAT], cwd=_PROJECT_ROOT,
                         creationflags=0x00000008 | 0x00000200)

    # 起動確認 (ログイン直後などネットワーク待ちで数秒かかることがある)
    for _ in range(int(_STOP_TIMEOUT / _POLL_INTERVAL)):
        if sc.launcher_alive():
            print("  ✅ Launcher が起動しました")
            return cmd_status()
        time.sleep(_POLL_INTERVAL)
    print("  ⚠️ Launcher の起動を確認できませんでした。logs\\launcher.log を確認してください。")
    return 1


def cmd_stop():
    for msg in sc.stop_all():
        print("  " + msg)
    # 実際に消えるまで待つ。ここで待たないと restart の start が
    # 「まだ生きている」と判定して何もせずに終わる。
    deadline = time.time() + _STOP_TIMEOUT
    while time.time() < deadline:
        if not sc.launcher_alive() and not any(s["alive"] for s in sc.all_status()):
            print("  ✅ 全プロセスが停止しました")
            return 0
        time.sleep(_POLL_INTERVAL)
    # status の戻り値 (0) をそのまま返すと「停止成功」と誤って伝わり、
    # cmd_restart が残存プロセスの上から起動を重ねてしまう。
    print("  ⚠️ 一部のプロセスが残っています:")
    cmd_status()
    return 1


def cmd_restart():
    rc = cmd_stop()
    if rc != 0:
        print("  ❌ 停止しきれなかったため起動を中止しました (二重起動を避けるため)")
        return rc
    return cmd_start()


def cmd_logs(lines=50):
    if not os.path.exists(LAUNCHER_LOG):
        print(f"  ⚠️ {LAUNCHER_LOG} がありません (launcher が一度も走っていない可能性)")
        return 1
    with open(LAUNCHER_LOG, encoding='utf-8', errors='replace') as f:
        tail = f.readlines()[-lines:]
    print(f"  --- {LAUNCHER_LOG} (末尾 {len(tail)} 行) ---")
    for line in tail:
        print("  " + line.rstrip())
    return 0


COMMANDS = {
    "status": cmd_status,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "logs": cmd_logs,
}

_MENU = [("1", "start", "起動"), ("2", "restart", "再起動"),
         ("3", "status", "状態確認"), ("4", "stop", "停止"), ("5", "logs", "ログ表示")]


def interactive():
    """ダブルクリック起動 (引数なし) 用の番号メニュー。"""
    print("\n" + "=" * 46)
    print("  🛰️ ARMS Control")
    print("=" * 46)
    cmd_status()
    print("-" * 46)
    for num, _key, label in _MENU:
        print(f"  {num}: {label}")
    print("  q: 終了")
    choice = input("\n  番号を選んでください: ").strip().lower()
    for num, key, _label in _MENU:
        if choice == num or choice == key:
            print()
            return COMMANDS[key]()
    return 0


def main(argv):
    if not argv:
        return interactive()
    cmd = argv[0].lower()
    if cmd not in COMMANDS:
        print(f"usage: py scripts/arms_ctl.py [{' | '.join(COMMANDS)}]")
        return 2
    return COMMANDS[cmd]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
