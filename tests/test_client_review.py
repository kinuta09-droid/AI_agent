# -*- coding: utf-8 -*-
import os
import sys

# プロジェクトルートのパスを追加
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
    from tools.tool_client_review import evaluate_deliverable
    
    bad_text = "できたよー。これでいいかな？"
    job = "プロフェッショナルな報告書"
    
    result = evaluate_deliverable(bad_text, job)
    
    if "error" in result:
        print(f"   -> Failed with error: {result['error']}")
        return
        
    avg = result.get("scores", {}).get("average", 10.0)
    print(f"   -> Score: {avg}")
    if avg < 8.0:
        print("   -> Success (Detected low quality)")
    else:
        print("   -> Failed: Expected < 8.0 for bad text")

def test_self_improve_empty():
    print("✅ Testing analyze_weak_tools without crash...")
    from engine_self_improve import analyze_weak_tools
    try:
        res = analyze_weak_tools()
        print(f"   -> Success: {res[:50]}...")
    except Exception as e:
        print(f"   -> Failed: {e}")

def test_patch_hints():
    print("✅ Testing get_approved_patch_hints...")
    from engine_self_improve import get_approved_patch_hints
    try:
        res = get_approved_patch_hints()
        print(f"   -> Success (Returned: {repr(res)})")
    except Exception as e:
        print(f"   -> Failed: {e}")

if __name__ == "__main__":
    test_imports()
    test_patch_hints()
    test_self_improve_empty()
    test_evaluation()
    print("🎉 All Client Review tests finished.")
