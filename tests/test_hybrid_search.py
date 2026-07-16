# -*- coding: utf-8 -*-
"""
Phase④ ハイブリッド検索 (RRF統合) のユニットテスト
(keyword_fn / semantic_fn 注入 — ネットワーク・Vault実体なし)

実行: py tests/test_hybrid_search.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_vault_hybrid import tool_vault_hybrid_search, NOT_FOUND


def _hit(rel_path, score, snippet="snippet-body"):
    return {"rel_path": rel_path, "filename": os.path.basename(rel_path),
            "score": score, "snippet": snippet}


def test_rrf_both_sources_win():
    """両リストに載るファイルが片方のみのファイルよりRRFで上位に来る"""
    kw = [_hit("a.md", 3), _hit("b.md", 1)]
    sem = [_hit("b.md", 0.9), _hit("c.md", 0.8)]
    result = tool_vault_hybrid_search("q", keyword_fn=lambda q: kw,
                                      semantic_fn=lambda q, k: sem)
    # b: 1/62 + 1/61 > a: 1/61 > c: 1/62
    assert result.startswith("- File: [[b.md]]"), f"b.md must rank first:\n{result}"
    assert result.index("[[a.md]]") < result.index("[[c.md]]"), "keyword rank1 must beat semantic rank2"
    assert "keyword+semantic" in result.split("\n")[0], "b.md must be tagged as both sources"
    print("✅ hybrid: RRF merge — both-source file wins")


def test_fallback_on_semantic_error_string():
    """semantic側が soft-error 文字列を返したら keyword のみ + 注記"""
    kw = [_hit("a.md", 2)]
    result = tool_vault_hybrid_search("q", keyword_fn=lambda q: kw,
                                      semantic_fn=lambda q, k: "[ERROR] Ollama not running.")
    assert "⚠️" in result and "[ERROR] Ollama not running." in result, "fallback note required"
    assert "[[a.md]]" in result, "keyword hits must still be returned"
    print("✅ hybrid: soft-error string → keyword-only fallback with note")


def test_fallback_on_import_error():
    """semantic側の ImportError (numpy欠落相当) でも keyword のみで継続"""
    def broken(q, k):
        raise ImportError("No module named 'numpy'")
    kw = [_hit("a.md", 2)]
    result = tool_vault_hybrid_search("q", keyword_fn=lambda q: kw, semantic_fn=broken)
    assert "⚠️" in result and "[[a.md]]" in result
    print("✅ hybrid: ImportError → keyword-only fallback")


def test_both_empty():
    """両ソース0件 → 見つかりませんでした"""
    result = tool_vault_hybrid_search("q", keyword_fn=lambda q: [],
                                      semantic_fn=lambda q, k: [])
    assert result == NOT_FOUND, f"unexpected: {result}"
    assert tool_vault_hybrid_search("") == "No query."
    print("✅ hybrid: empty results / empty query")


def test_top_k_and_output_format():
    """top_k制限 + 出力が既存ツールと同じ形式 (top_kは文字列でも強制変換)"""
    kw = [_hit(f"f{i}.md", 10 - i) for i in range(6)]
    result = tool_vault_hybrid_search("q", top_k="2", keyword_fn=lambda q: kw,
                                      semantic_fn=lambda q, k: [])
    assert result.count("- File: [[") == 2, f"top_k=2 must yield 2 hits:\n{result}"
    assert "Snippet:" in result and result.rstrip().endswith("..."), "format must match existing tools"
    print("✅ hybrid: top_k coercion + output format")


def test_semantic_chunks_dedup():
    """semantic側の同一ファイル複数チャンクは最良ランク1件に集約"""
    sem = [_hit("x.md", 0.9, "chunk1"), _hit("x.md", 0.8, "chunk2"), _hit("y.md", 0.7)]
    result = tool_vault_hybrid_search("q", keyword_fn=lambda q: [],
                                      semantic_fn=lambda q, k: sem)
    assert result.count("[[x.md]]") == 1, f"x.md must appear once:\n{result}"
    assert result.index("[[x.md]]") < result.index("[[y.md]]"), "x.md keeps its best rank"
    assert "chunk1" in result, "snippet must come from the best-ranked chunk"
    print("✅ hybrid: per-file dedup of semantic chunks")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
