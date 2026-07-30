# -*- coding: utf-8 -*-
"""get_gmail_summary() (gws CLI 経由) の疎通テスト

外部前提 (gws CLI + 認証済みアカウント) が要り、**本物の受信箱を読んで
Gemini で要約する = 課金が発生する**統合テスト。既定では SKIP し、
明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_gmail_summary.py

前提が無い環境でも SKIP して正常終了する
(tests/test_vault_semantic_smoke.py と同じ作法)。

2026-07-30 に2点直した:
  1. `sys.path.append(os.path.dirname(os.path.abspath(__file__)))` が
     tests/ ディレクトリ自身を追加していた (dirname が1段足りない)。
     プロジェクトルートが載らないので `from tools...` が ImportError で落ちる。
     CI では PYTHONPATH=workspace がこの欠陥を隠していて気付けなかった。
  2. 終了コードを一切返していなかった。ツール層は例外を投げずに
     "[ERROR] ..." を返す規約なので、失敗しても常に exit 0 = CI から緑に見える。
     判定は lib.bot_errors.is_failure_result() に一元化する (CLAUDE.md の規約)。

実行: py tests/test_gmail_summary.py
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
from tools.tool_gmail_manager import get_gmail_summary

logging.basicConfig(level=logging.INFO)


def main():
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("⏭️ SKIP: 実取得は ARMS_LIVE_TESTS=1 のときだけ行います")
        print("   (本物の受信箱を読み、未読メールを Gemini で要約するため課金が発生します)")
        return 0
    if shutil.which("gws") is None:
        print("⏭️ SKIP: gws CLI が見つかりません (Gmail は gws 経由で取得します)")
        return 0

    print("Testing Updated Gmail Summary (via gws)...")
    summary = get_gmail_summary()
    print("\n--- GMAIL SUMMARY ---")
    print(summary)

    if is_failure_result(summary):
        print("\n❌ FAILED: ツールが失敗文字列を返した")
        return 1
    print("\n✅ PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
