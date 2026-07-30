# -*- coding: utf-8 -*-
"""tool_sync_audio_transcriptions() (Google Drive Inbox 同期) の疎通テスト

外部前提 (gws CLI + Drive 認証) が要り、**Inbox の音声を実際に文字起こし・
要約する = 課金と副作用がある**統合テスト。既定では SKIP し、
明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_audio_automation.py

前提が無い環境でも SKIP して正常終了する
(tests/test_vault_semantic_smoke.py と同じ作法)。

2026-07-30 に2点直した (test_gmail_summary.py と同じ欠陥):
  1. `sys.path.append(os.path.dirname(os.path.abspath(__file__)))` が
     tests/ 自身を追加しており、プロジェクトルートが載っていなかった
     (dirname が1段足りない)。CI では PYTHONPATH=workspace が隠していた。
  2. 終了コードを返していなかったため、失敗しても CI からは緑に見えた。
     判定は lib.bot_errors.is_failure_result() に一元化する。

実行: py tests/test_audio_automation.py
"""
import logging
import os
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.bot_errors import is_failure_result
from tools.tool_audio_automation import tool_sync_audio_transcriptions

logging.basicConfig(level=logging.INFO)


def main():
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("⏭️ SKIP: 実同期は ARMS_LIVE_TESTS=1 のときだけ行います")
        print("   (Drive Inbox の音声を実際に文字起こし・要約するため Gemini を消費し、")
        print("    Inbox のファイル状態も変えます)")
        return 0
    if shutil.which("gws") is None:
        print("⏭️ SKIP: gws CLI が見つかりません (Drive Inbox は gws 経由で読みます)")
        return 0

    print("Starting Audio Automation Sync Test...")
    result = tool_sync_audio_transcriptions()
    print("\n--- TEST RESULT ---")
    print(result)

    if is_failure_result(result):
        print("\n❌ FAILED: ツールが失敗文字列を返した")
        return 1
    print("\n✅ PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
