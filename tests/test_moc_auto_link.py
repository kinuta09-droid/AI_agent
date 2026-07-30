# -*- coding: utf-8 -*-
"""tool_create_note の MOC 自動リンク (up プロパティ) の回帰テスト

**実 Vault にノートを作り、tool_create_note の副作用で Obsidian.exe が起動する**
統合テスト。テストを一周回すだけで実副作用が起きる状態にしないため、既定では
SKIP し、明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_moc_auto_link.py

Vault が無い環境 (CI 等) でも SKIP して正常終了する
(tests/test_vault_semantic_smoke.py と同じ作法)。

2026-07-30 に3点直した:
  1. `from agent_hello import find_file_in_vault` が ImportError で落ちていた。
     この関数は lib/lib_shared.py:256 へ移動済みで、agent_hello の
     再エクスポート一覧にも入っていない (モジュール分割の取り残し)。
  2. `sys.path.insert(0, r'C:\\AI_agent')` とルートを直書きしていた。
     チェックアウト先が違えば動かない。__file__ からの相対に変えた。
  3. **判定結果を終了コードに反映していなかった**。全ケースが FAIL でも
     print するだけで exit 0 を返すため、CI (終了コードで合否を見る) からは
     常に緑に見える。このリポジトリが繰り返し踏んでいる
     「表示と実態の食い違い」がテスト層で起きていた。

実行: py tests/test_moc_auto_link.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.lib_shared import VAULT_ROOT, find_file_in_vault
from tools.tool_obsidian import tool_create_note

TEST_CASES = [
    {
        "filename": "Test_AI_Report.md",
        "content": (
            "Recent advancements in Large Language Models (LLMs) such as Gemini and GPT-4 "
            "have revolutionized natural language processing. Neural networks are becoming "
            "more efficient."
        ),
        "summary": "AI progress report",
        "expected_moc": "MOC_AI",
    },
    {
        "filename": "Test_Election_Analysis.md",
        "content": (
            "The recent election results show a shift in voter demographics. "
            "The ruling party lost significant seats in the diet."
        ),
        "summary": "Election analysis",
        "expected_moc": "MOC_Politics",
    },
]


def test_moc_linking():
    print("=== TEST START: MOC Auto-Linking ===")
    failures = []

    for case in TEST_CASES:
        print(f"\nRunning case: {case['filename']}")
        result = tool_create_note(case["filename"], case["content"], case["summary"])
        print(f"Creation Result: {result}")

        path = find_file_in_vault(case["filename"])
        if not (path and os.path.exists(path)):
            failures.append(f"{case['filename']}: 作成したはずのファイルが見つからない ({result})")
            print("  ❌ FAIL: File not found.")
            continue

        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()

            expected = f'up: "[[{case["expected_moc"]}]]"'
            if expected in content:
                print(f"  ✅ PASS: Linked to [[{case['expected_moc']}]]")
            elif 'up: "[[00_Inbox]]"' in content:
                failures.append(
                    f"{case['filename']}: [[00_Inbox]] にフォールバックした "
                    f"(期待: [[{case['expected_moc']}]])"
                )
                print(f"  ❌ FAIL: Defaulted to [[00_Inbox]] (Expected {case['expected_moc']})")
            else:
                failures.append(f"{case['filename']}: up プロパティが想定外 — {content[:100]!r}")
                print(f"  ❌ FAIL: Unexpected property value.\n{content[:100]}")
        finally:
            # 実 Vault を汚さないよう、判定の成否によらず必ず消す
            try:
                os.remove(path)
                print("  (deleted test file)")
            except OSError as e:
                print(f"  ⚠️ テストファイルを削除できなかった: {e}")

    return failures


if __name__ == "__main__":
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("⏭️ SKIP: 実 Vault への書き込みは ARMS_LIVE_TESTS=1 のときだけ行います")
        print("   (ノートを作成・削除し、tool_create_note の副作用で Obsidian が起動します)")
        sys.exit(0)
    if not os.path.isdir(VAULT_ROOT):
        print(f"⏭️ SKIP: Obsidian Vault が見つかりません ({VAULT_ROOT})")
        print("   (実 Vault にノートを作る統合テストのため、CI 等では実行できません)")
        sys.exit(0)

    failed = test_moc_linking()
    print("\n" + "=" * 50)
    if failed:
        print(f"❌ FAILED: {len(failed)} 件")
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)
    print("🎉 ALL TESTS PASSED")
    sys.exit(0)
