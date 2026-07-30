# -*- coding: utf-8 -*-
"""agent_scheduler の Discord 通知経路の手動確認スクリプト

**実際に Discord へ投稿する副作用がある**ため、既定では配線だけ確認して
送信しない。実送信は明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_scheduler_setup.py

2026-07-30 の変更理由:
  このファイルは sys.path の bootstrap を持たず import で落ちており、
  実機では「失敗」として数えられていた。しかし単に sys.path を足すと
  今度は **CI が回るたびに本物の Discord へ通知を送る**ようになる
  (CI の PYTHONPATH=workspace 設定下では実際にそうなりかけていた)。
  CLAUDE.md Architecture が言う "tests/: Unit tests and debugging stubs" の
  後者に当たるので、副作用はオプトインの後ろに置く。

既定モードでも「通知関数が import でき、呼び出し可能である」ことは検査するので、
agent_scheduler 側の配線が壊れれば気付ける。

実行: py tests/test_scheduler_setup.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agent_scheduler import send_discord_notification


def main():
    if not callable(send_discord_notification):
        print("❌ FAILED: agent_scheduler.send_discord_notification が呼び出し可能でない")
        return 1
    print("✅ agent_scheduler.send_discord_notification を import できた")

    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("⏭️ SKIP: 実送信は ARMS_LIVE_TESTS=1 のときだけ行います")
        print("   (このテストは本物の Discord チャンネルへ投稿します)")
        return 0

    print("Sending test notification...")
    send_discord_notification("✅ **Scheduler Test**: Notification system is working!")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
