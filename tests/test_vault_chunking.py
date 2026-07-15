# -*- coding: utf-8 -*-
"""
_chunk_markdown の純粋関数テスト (I/O・ネットワーク一切なし)

実行: py tests/test_vault_chunking.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_vault_index import _chunk_markdown, CHUNK_MIN_CHARS


def test_no_headers_single_chunk():
    text = "これは見出しのない短いノートです。日々の雑感を書いています。"
    chunks = _chunk_markdown(text)
    assert len(chunks) == 1, f"expected 1 chunk, got {len(chunks)}"
    assert chunks[0]["heading"] is None
    assert chunks[0]["text"] == text
    print("✅ no headers -> single chunk")


def test_h2_sections_split():
    text = (
        "イントロダクションの文章です。ここは見出しの前にあります。\n\n"
        "## セクション1\nセクション1の本文です。内容がここに続きます。\n\n"
        "## セクション2\nセクション2の本文です。別の話題について書いています。"
    )
    chunks = _chunk_markdown(text)
    assert len(chunks) == 3, f"expected 3 chunks, got {len(chunks)}"
    assert chunks[0]["heading"] is None
    assert chunks[1]["heading"] == "セクション1"
    assert chunks[2]["heading"] == "セクション2"
    assert chunks[1]["text"].startswith("## セクション1")
    print("✅ ## sections split with headings preserved")


def test_frontmatter_stripped():
    text = (
        "---\nup: \"[[Dashboard]]\"\ndate: 2026-07-15\ntype: note\n---\n"
        "本文はここからです。フロントマターは除去されるはずです。"
    )
    chunks = _chunk_markdown(text)
    assert len(chunks) == 1
    assert "date: 2026-07-15" not in chunks[0]["text"]
    assert chunks[0]["text"].startswith("本文はここから")
    print("✅ frontmatter stripped")


def test_oversized_section_subsplit_with_overlap():
    para = "この段落はテスト用のダミーテキストです。" * 5  # ~100 chars
    body = "\n\n".join([para] * 30)  # ~3000+ chars, paragraph boundaries
    text = f"## 長いセクション\n{body}"
    chunks = _chunk_markdown(text, max_chars=1200, overlap=150)
    assert len(chunks) >= 2, f"oversized section should sub-split, got {len(chunks)}"
    for c in chunks:
        assert len(c["text"]) <= 1200 + 200, f"chunk too long: {len(c['text'])}"
        assert c["heading"] == "長いセクション"
    print(f"✅ oversized section sub-split into {len(chunks)} chunks")


def test_hard_split_single_huge_paragraph():
    text = "あ" * 5000  # 段落境界なしの巨大テキスト
    chunks = _chunk_markdown(text, max_chars=1200, overlap=150)
    assert len(chunks) >= 3, f"expected hard split, got {len(chunks)}"
    print(f"✅ single huge paragraph hard-split into {len(chunks)} chunks")


def test_tiny_noise_dropped():
    text = "## x\nab\n\n## 実質的なセクション\nこちらは十分な長さのある本文で、チャンクとして保持されるべきものです。"
    chunks = _chunk_markdown(text)
    assert all(len(c["text"]) >= CHUNK_MIN_CHARS for c in chunks)
    assert any("実質的なセクション" in (c["heading"] or "") for c in chunks)
    print("✅ tiny noise chunks dropped")


def test_empty_and_frontmatter_only():
    assert _chunk_markdown("") == []
    assert _chunk_markdown("---\ndate: 2026-07-15\n---\n") == []
    print("✅ empty / frontmatter-only -> no chunks")


def test_crlf_normalized():
    """CRLF 改行のノートでもフロントマター除去と見出し分割が機能する (grill C1 回帰)。"""
    text = (
        "---\r\ndate: 2026-07-15\r\ntype: note\r\n---\r\n"
        "## セクション\r\nCRLF改行で保存されたノートの本文がここにあります。"
    )
    chunks = _chunk_markdown(text)
    assert len(chunks) == 1, f"expected 1 chunk, got {len(chunks)}"
    assert "date: 2026-07-15" not in chunks[0]["text"], "frontmatter must be stripped from CRLF file"
    assert chunks[0]["heading"] == "セクション"
    print("✅ CRLF notes: frontmatter stripped, headings split")


def test_overlap_clamp_no_infinite_loop():
    """overlap >= max_chars でも無限ループしない (防御クランプの回帰)。"""
    text = "あ" * 3000
    chunks = _chunk_markdown(text, max_chars=500, overlap=500)
    assert len(chunks) >= 2
    print("✅ overlap >= max_chars clamped (no infinite loop)")


if __name__ == "__main__":
    test_no_headers_single_chunk()
    test_h2_sections_split()
    test_frontmatter_stripped()
    test_oversized_section_subsplit_with_overlap()
    test_hard_split_single_huge_paragraph()
    test_tiny_noise_dropped()
    test_empty_and_frontmatter_only()
    test_crlf_normalized()
    test_overlap_clamp_no_infinite_loop()
    print("\n🎉 ALL CHUNKING TESTS PASSED")
