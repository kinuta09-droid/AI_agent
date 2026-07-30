# -*- coding: utf-8 -*-
"""tool_client_review / engine_self_improve の疎通テスト

2026-07-30 に2点直した:
  1. `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` が
     tests/ 自身を追加しており、プロジェクトルートが載っていなかった
     (dirname が1段足りない)。実機では ImportError で落ちていたが、
     CI では PYTHONPATH=workspace がこの欠陥を隠していた。
  2. test_imports 以外は失敗しても終了コードに反映されず、
     「Failed:」と印字しつつ exit 0 = CI から緑に見えていた。

evaluate_deliverable() は Gemini を実際に呼ぶ = 課金が発生するので、
ARMS_LIVE_TESTS=1 かつ実キーがあるときだけ実行する。
テストを一周させるだけで API 予算が減る状態にはしない
(このリポジトリは DAILY_GEMINI_CALL_LIMIT でコストを管理している)。

実行: py tests/test_client_review.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_FAIL = []


def _live_gemini_allowed() -> bool:
    """実 Gemini 呼び出しを行ってよいか (オプトイン + 実キーの両方が必要)。"""
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        return False
    key = os.getenv("GOOGLE_API_KEY", "")
    return bool(key) and not key.startswith("ci-")


def test_imports():
    print("✅ Testing imports...")
    try:
        from tools import tool_client_review
        from engine_self_improve import analyze_weak_tools, get_approved_patch_hints
        from lib import lib_persona
        print("   -> Success")
    except Exception as e:
        print(f"   -> Failed: {e}")
        sys.exit(1)

def test_evaluation():
    print("✅ Testing evaluation engine (low quality string)...")
    if not _live_gemini_allowed():
        print("   -> ⏭️ SKIP: ARMS_LIVE_TESTS=1 と実 GOOGLE_API_KEY のときだけ実行します")
        print("      (evaluate_deliverable は Gemini を実呼び出しするため課金が発生します)")
        return

    from tools.tool_client_review import evaluate_deliverable

    bad_text = "できたよー。これでいいかな？"
    job = "プロフェッショナルな報告書"

    result = evaluate_deliverable(bad_text, job)

    if "error" in result:
        _FAIL.append(f"evaluate_deliverable がエラーを返した: {result['error']}")
        print(f"   -> Failed with error: {result['error']}")
        return

    avg = result.get("scores", {}).get("average", 10.0)
    print(f"   -> Score: {avg}")
    if avg < 8.0:
        print("   -> Success (Detected low quality)")
    else:
        _FAIL.append(f"低品質テキストのスコアが {avg} (期待: < 8.0)")
        print("   -> Failed: Expected < 8.0 for bad text")

def test_self_improve_empty():
    print("✅ Testing analyze_weak_tools without crash...")
    from engine_self_improve import analyze_weak_tools
    try:
        res = analyze_weak_tools()
        print(f"   -> Success: {res[:50]}...")
    except Exception as e:
        _FAIL.append(f"analyze_weak_tools が例外: {type(e).__name__}: {e}")
        print(f"   -> Failed: {e}")

def test_patch_hints():
    print("✅ Testing get_approved_patch_hints...")
    from engine_self_improve import get_approved_patch_hints
    try:
        res = get_approved_patch_hints()
        print(f"   -> Success (Returned: {repr(res)})")
    except Exception as e:
        _FAIL.append(f"get_approved_patch_hints が例外: {type(e).__name__}: {e}")
        print(f"   -> Failed: {e}")

if __name__ == "__main__":
    test_imports()
    test_patch_hints()
    test_self_improve_empty()
    test_evaluation()

    print("\n" + "=" * 50)
    if _FAIL:
        print(f"❌ FAILED: {len(_FAIL)} 件")
        for f in _FAIL:
            print(f"  - {f}")
        sys.exit(1)
    print("🎉 All Client Review tests passed.")
    sys.exit(0)
