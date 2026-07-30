# -*- coding: utf-8 -*-
"""tool_get_youtube_transcript() の疎通テスト

**実際に YouTube へアクセスする**ため、既定ではモジュール配線だけ確認して
取得は行わない。実アクセスは明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_yt.py

2026-07-30 に2点直した:
  1. 1行目でいきなり `sys.path.append(...)` しており `import sys, os` が無く、
     NameError で必ず落ちていた (テストとして一度も機能していない)。
  2. 直しただけだと今度は CI が回るたびに YouTube を叩き、
     レート制限や地域制限で不安定に赤くなる。実アクセスはオプトインの後ろへ。

実行: py tests/test_yt.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.bot_errors import is_failure_result
from tools.tool_web import tool_get_youtube_transcript

# 字幕が確実に存在する安定した動画
TEST_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def main():
    if not callable(tool_get_youtube_transcript):
        print("❌ FAILED: tool_get_youtube_transcript が呼び出し可能でない")
        return 1
    print("✅ tools.tool_web.tool_get_youtube_transcript を import できた")

    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("⏭️ SKIP: 実取得は ARMS_LIVE_TESTS=1 のときだけ行います")
        print("   (このテストは YouTube へ実アクセスします)")
        return 0

    print(f"Testing transcript fetch for: {TEST_URL}")
    result = tool_get_youtube_transcript(TEST_URL)

    print("\n--- Result Sample ---")
    print(result[:500])
    print("...")
    print("---------------------")

    if is_failure_result(result) or "Error" in result:
        print("❌ Test Failed")
        return 1
    if "【YouTube動画内容（文字起こし）】" in result:
        print("✅ Test Passed")
        return 0
    print("❓ Unexpected Result")
    return 1


if __name__ == "__main__":
    sys.exit(main())
