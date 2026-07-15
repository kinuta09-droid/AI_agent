# -*- coding: utf-8 -*-
"""
tool_vault_semantic_search のランキングロジックテスト (スタブ埋め込み、ネットワークなし)

既知の類似度順になるようベクトルを手動構築して直接DBに挿入し、
top_k の並び順・件数・出力形式を検証する。

実行: py tests/test_vault_cosine_search.py
"""
import os
import sys
import tempfile
import shutil

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_vault_index import tool_vault_semantic_search, _connect, _normalize

DIM = 4


def _insert_chunk(conn, filepath, heading, text, vec):
    arr = _normalize(np.asarray(vec, dtype=np.float32))
    conn.execute(
        "INSERT INTO chunks(filepath, chunk_index, heading, text, embedding, dim) "
        "VALUES(?, 0, ?, ?, ?, ?)",
        (filepath, heading, text, arr.tobytes(), DIM),
    )


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "index.db")

    try:
        # --- DB未構築時のメッセージ ---
        result = tool_vault_semantic_search("なにか", db_path=db)
        assert "未構築" in result, result
        print("✅ missing index -> friendly Japanese message")

        # クエリベクトルは x軸方向 [1,0,0,0]。類似度: best > mid > worst
        conn = _connect(db)
        _insert_chunk(conn, "30_Main/best.md", "最重要", "クエリに最も近いチャンクの本文です。", [1.0, 0.05, 0.0, 0.0])
        _insert_chunk(conn, "01_Daily/mid.md", None, "そこそこ近いチャンクの本文です。", [0.7, 0.7, 0.0, 0.0])
        _insert_chunk(conn, "50_Archive/worst.md", "無関係", "全く関係ないチャンクの本文です。", [0.0, 0.0, 1.0, 0.0])
        conn.commit()
        conn.close()

        def stub_embed(texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        # --- ランキング順の検証 ---
        result = tool_vault_semantic_search("テストクエリ", top_k=3, embed_fn=stub_embed, db_path=db)
        pos_best = result.find("best.md")
        pos_mid = result.find("mid.md")
        pos_worst = result.find("worst.md")
        assert -1 not in (pos_best, pos_mid, pos_worst), result
        assert pos_best < pos_mid < pos_worst, f"ranking order wrong:\n{result}"
        print("✅ cosine ranking order: best > mid > worst")

        # --- top_k 制限 ---
        result = tool_vault_semantic_search("テストクエリ", top_k=1, embed_fn=stub_embed, db_path=db)
        assert "best.md" in result and "mid.md" not in result, result
        print("✅ top_k=1 returns only the best hit")

        # --- 出力形式が tool_vault_search と互換 ---
        assert result.startswith("- File: [[best.md]]"), result
        assert "Snippet:" in result
        print("✅ output format matches tool_vault_search style")

        # --- top_k が文字列で来ても動く (LLM解決引数対策) ---
        result = tool_vault_semantic_search("テストクエリ", top_k="2", embed_fn=stub_embed, db_path=db)
        assert "best.md" in result and "mid.md" in result and "worst.md" not in result
        print("✅ string top_k coerced to int")

        # --- 埋め込み失敗はソフトエラー文字列 ---
        result = tool_vault_semantic_search(
            "テストクエリ", embed_fn=lambda t: "[ERROR] Ollama not running.", db_path=db)
        assert result.startswith("[ERROR]")
        print("✅ embed failure -> soft [ERROR] string (no crash)")

        # --- 空クエリ ---
        assert tool_vault_semantic_search("", db_path=db) == "No query."
        print("✅ empty query rejected")

        print("\n🎉 ALL COSINE SEARCH TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
