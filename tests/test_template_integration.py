# -*- coding: utf-8 -*-
"""tool_create_note のテンプレート統合 (参照元/関連ノートの抽出) の回帰テスト

本文中の `## References` / `## Sources` / `> Source: [[...]]` をテンプレートの
「### 参照元」「### 関連ノート」へ移し、かつ**本文を巻き込んで消さない**ことを見る。
最後の点はかつて正規表現が本文を丸ごと飲み込んだ実障害の回帰。

**実 Vault にノートを作り、tool_create_note の副作用で Obsidian.exe が起動する**
統合テスト。テストを一周回すだけで実副作用が起きる状態にしないため、既定では
SKIP し、明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_template_integration.py

Vault が無い環境 (CI 等) でも SKIP して正常終了する
(tests/test_vault_semantic_smoke.py と同じ作法)。

2026-07-30 に3点直した (test_moc_auto_link.py と同じ欠陥):
  1. `from agent_hello import find_file_in_vault` が ImportError。
     この関数は lib/lib_shared.py:256 へ移動済み。
  2. ルートを `r'C:\\AI_agent'` と直書きしていたのを __file__ 相対に。
  3. **errors を集めておきながら終了コードに反映していなかった**ため、
     全チェックが FAIL でも CI からは緑に見えていた。

実行: py tests/test_template_integration.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.lib_shared import VAULT_ROOT, find_file_in_vault
from tools.tool_obsidian import tool_create_note

FILENAME = "Test_Integration.md"
CONTENT = """This is the main content of the note.
It has some important information.
> Source: [[Lineage_Parent_Note]]

## References
- https://example.com/ref1
- https://example.com/ref2

## Sources
- [[Source_Note_A]]
- [[Source_Note_B]]

This part should remain in the content.
"""


def test_template_integration():
    print("=== TEST START: Template Integration ===")
    errors = []

    print(f"\nCreating note: {FILENAME}")
    result = tool_create_note(FILENAME, CONTENT, "Testing template integration")
    print(f"Result: {result}")

    path = find_file_in_vault(FILENAME)
    if not (path and os.path.exists(path)):
        return [f"作成したはずのファイルが見つからない ({result})"]

    try:
        with open(path, 'r', encoding='utf-8') as f:
            file_content = f.read()

        print("\n--- Generated File Content ---")
        print(file_content)
        print("------------------------------\n")

        def expect(label, cond, detail):
            if cond:
                print(f"  ✅ PASS: {label}")
            else:
                errors.append(f"{label}: {detail}")
                print(f"  ❌ FAIL: {label} — {detail}")

        expect(
            "参照元がテンプレート節へ移動",
            "### 参照元\n- https://example.com/ref1" in file_content,
            "### 参照元 に URL が入っていない",
        )
        expect(
            "本文が保持されている",
            "This is the main content of the note." in file_content,
            "正規表現が本文を飲み込んだ",
        )
        expect(
            "関連ノート (Lineage 含む) がテンプレート節へ移動",
            "- [[Lineage_Parent_Note]]" in file_content,
            "Lineage リンクがテンプレート節に無い",
        )
        expect(
            "Lineage リンクが本文から除去されている",
            "> Source: [[Lineage_Parent_Note]]" not in file_content,
            "本文に '> Source: ...' が残っている",
        )
        expect(
            "'## References' が本文から除去されている",
            "## References" not in file_content,
            "見出しが本文に残っている",
        )
        expect(
            "'## Sources' が本文から除去されている",
            "## Sources" not in file_content,
            "見出しが本文に残っている",
        )
        expect(
            "末尾の本文が保持されている",
            "This part should remain in the content." in file_content,
            "末尾が巻き込まれて消えた",
        )
    finally:
        # 判定の成否によらず実 Vault から消す (旧版は成功時しか消していなかった)
        try:
            os.remove(path)
            print("  (deleted test file)")
        except OSError as e:
            print(f"  ⚠️ テストファイルを削除できなかった: {e}")

    return errors


if __name__ == "__main__":
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        print("⏭️ SKIP: 実 Vault への書き込みは ARMS_LIVE_TESTS=1 のときだけ行います")
        print("   (ノートを作成・削除し、tool_create_note の副作用で Obsidian が起動します)")
        sys.exit(0)
    if not os.path.isdir(VAULT_ROOT):
        print(f"⏭️ SKIP: Obsidian Vault が見つかりません ({VAULT_ROOT})")
        print("   (実 Vault にノートを作る統合テストのため、CI 等では実行できません)")
        sys.exit(0)

    failed = test_template_integration()
    print("\n" + "=" * 50)
    if failed:
        print(f"❌ FAILED: {len(failed)} 件")
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)
    print("🎉 ALL TESTS PASSED")
    sys.exit(0)
