# -*- coding: utf-8 -*-
"""tool_scan_crypto_market() の疎通テスト (手動デバッグスクリプト)。

**実 Binance API を全銘柄分叩き、BUY 系候補が出た銘柄には tool_analyze_crypto
(Gemini + Tavily) を起動する = 課金が発生する。** 既定では SKIP し、
明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_scan_crypto.py

2026-07-30 に直した欠陥:
  1. **ゲートが無く、スイープ1周ごとに実行されていた。** 日次カウンタが +1 される
     のをカウンタ差分で確認 (候補が出た日はさらに増える。cost_guard の
     SWEEP_COUNTER が別枠で止めるまで動く)。
     ネットワークにも毎回出ており、開発機では Binance が 451 を返して
     全銘柄 skip されるため「速いから無害」に見えていた。
  2. 例外を print するだけで **終了コードが常に 0** だった
     (失敗しても CI からは緑に見える)。
"""
import sys
import os
import datetime

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _live_allowed() -> bool:
    """実 API 呼び出しを行ってよいか (オプトイン + 実キーの両方が必要)。"""
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        return False
    key = os.getenv("GOOGLE_API_KEY", "")
    return bool(key) and not key.startswith("ci-")


def main() -> int:
    if not _live_allowed():
        print("⏭️ SKIP: ARMS_LIVE_TESTS=1 と実 GOOGLE_API_KEY のときだけ実行します")
        print("   (tool_scan_crypto_market は Binance を全銘柄分叩き、")
        print("    候補が出れば Gemini + Tavily 分析を起動します)")
        return 0

    from agent_hello import tool_scan_crypto_market
    from lib.bot_errors import is_failure_result

    print(f"[{datetime.datetime.now()}] Testing tool_scan_crypto_market...")
    try:
        result = tool_scan_crypto_market()
    except Exception as e:
        print(f"❌ Failed with Exception: {e}")
        import traceback
        traceback.print_exc()
        return 1

    # ツール層は例外を投げず "Error: ..." を返す規約なので、文字列でも判定する
    # (「例外が出なかった＝成功」は CLAUDE.md の既知バグパターン)
    if is_failure_result(result):
        print(f"❌ ツールが失敗を返した:\n{result[:300]}")
        return 1

    print(f"✅ Success! Result snippet:\n{result[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
