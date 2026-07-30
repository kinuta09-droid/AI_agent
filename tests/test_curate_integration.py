# -*- coding: utf-8 -*-
"""tool_curator.search_and_save_topic() の統合テスト (手動デバッグスクリプト)。

**実 Tavily 検索 + 実 Gemini 分類 + 実 Vault への書き込み/削除を伴う = 課金と
副作用がある**。既定では SKIP し、明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_curate_integration.py

2026-07-30 に直した欠陥:
  1. `sys.path.insert(0, r'C:\\AI_agent')` とルートを直書きしており、
     リポジトリが別の場所にある環境 (CI) では `No module named 'tools'` で落ちていた。
  2. 実 API + 実 Vault を触るのにゲートが無く、スイープ1回で必ず課金と
     Vault 書き込みが起きていた (Session 2 の仕分けから漏れていた1件)。
  3. 判定結果が終了コードに反映されず、FAIL を印字しつつ exit 0 だった。
  4. Vault ルート `D:\\マイドライブ\\Obsidian` の直書き -> lib_shared の設定を使う。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

_FAIL = []


def _check(ok: bool, label: str, fail_detail: str = "") -> None:
    if ok:
        print(f"  ✅ {label}")
    else:
        _FAIL.append(label)
        print(f"  ❌ {label}" + (f" — {fail_detail}" if fail_detail else ""))


def _live_allowed() -> bool:
    """実 Tavily/Gemini 呼び出しと実 Vault 書き込みを行ってよいか。"""
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        return False
    key = os.getenv("GOOGLE_API_KEY", "")
    return bool(key) and not key.startswith("ci-")


def test_curate_integration():
    print("=== TEST START: Curate Integration ===")

    if not _live_allowed():
        print("⏭️ SKIP: ARMS_LIVE_TESTS=1 と実 GOOGLE_API_KEY のときだけ実行します")
        print("   (search_and_save_topic は Tavily と Gemini を実呼び出しし、")
        print("    実 Vault にノートを作成してから削除します)")
        return

    from tools.tool_curator import search_and_save_topic
    from lib.lib_shared import VAULT_ROOT

    topic = "Test_Agent_Integration"
    print(f"Running search_and_save_topic for '{topic}'...")
    try:
        result = search_and_save_topic(topic, search_query="Agentic workflow design patterns")
    except Exception as e:
        _check(False, "search_and_save_topic が例外を投げない", str(e))
        return
    print(f"Result: {result}")

    # 戻り値は
    #   "Success: Created [[30_Main/Knowledge_Base/yyyy-mm-dd_Test_Agent_Integration.md]] (Path: ...)"
    # の形。2026-07-26 以前は [[Knowledge_Base/...]] と Vault ルート基準になっておらず、
    # Obsidian 上で未解決リンクになっていた。リンクは Vault ルート相対に統一済み。
    if "[[" not in result or "]]" not in result:
        _check(False, "戻り値が [[wikilink]] を含む", f"result={result!r}")
        return
    match = result.split("[[")[1].split("]]")[0]

    filepath = os.path.join(VAULT_ROOT, *match.split("/"))
    if not os.path.exists(filepath):
        _check(False, "ノートが実際に作成されている", f"not found: {filepath}")
        return
    _check(True, "ノートが実際に作成されている")

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    print("\n--- Content Header ---")
    print(content[:500])
    print("----------------------\n")

    # MOC リンク: 00_Inbox に落ちていたら分類が効いていない
    _check(
        'up: "[[MOC_AI]]"' in content or 'up: "[[MOC_PC]]"' in content,
        "MOC にリンクされている (00_Inbox に落ちていない)",
        f"up の値が MOC_AI/MOC_PC でない (content 先頭: {content[:120]!r})",
    )

    # テンプレート統合: 生成された "## References" が "### 参照元" へ移されている
    _check(
        "### 参照元" in content and "## References" not in content,
        "References が ### 参照元 に統合されている",
        "### 参照元 が無い、または ## References が本文に残っている",
    )

    try:
        os.remove(filepath)
        print("Deleted test file.")
    except OSError as e:
        print(f"⚠️ テストファイルを削除できませんでした: {e}")


if __name__ == "__main__":
    test_curate_integration()
    print(f"\n=== 結果: FAIL {len(_FAIL)} ===")
    sys.exit(1 if _FAIL else 0)
