# -*- coding: utf-8 -*-
"""
実 Ollama + bge-m3 を使う唯一のスモークテスト。

Ollama 未起動 / bge-m3 未 pull の環境では SKIP して正常終了する
(CI や未セットアップ環境でも失敗しない)。

実行: py tests/test_vault_semantic_smoke.py
"""
import os
import sys
import tempfile
import shutil

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_vault_index import (
    _call_ollama_embed, update_vault_index, tool_vault_semantic_search,
)


def main():
    # 疎通確認 — ダメなら SKIP (失敗ではない)
    probe = _call_ollama_embed(["ping"])
    if isinstance(probe, str):
        print(f"⏭️ SKIP: {probe}")
        print("   (Ollama を起動し `ollama pull bge-m3` を実行すると本テストが有効になります)")
        return

    print(f"✅ Ollama reachable, embedding dim = {len(probe[0])}")

    tmp = tempfile.mkdtemp()
    vault = os.path.join(tmp, "vault")
    db = os.path.join(tmp, "index.db")
    os.makedirs(vault)
    try:
        with open(os.path.join(vault, "ai_note.md"), "w", encoding="utf-8") as f:
            f.write("## AIエージェント\nAIエージェントの収益化とマネタイズ戦略についてのメモ。note.comでの有料販売を検討中。")
        with open(os.path.join(vault, "cooking.md"), "w", encoding="utf-8") as f:
            f.write("## 料理\n今日の夕食はカレーを作った。スパイスの配合を工夫した。")

        result = update_vault_index(db_path=db, vault_root=vault)
        print(f"✅ index built: {result}")
        assert "2 files updated" in result, result

        # 意味検索: 「お金を稼ぐ方法」-> マネタイズのノートが上位に来るはず
        result = tool_vault_semantic_search("お金を稼ぐ方法", top_k=1, db_path=db)
        print(f"--- search result ---\n{result}\n---------------------")
        assert "ai_note.md" in result, f"semantic ranking unexpected:\n{result}"
        print("✅ semantic search ranked the monetization note above cooking")

        print("\n🎉 SMOKE TEST PASSED (real Ollama + bge-m3)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
