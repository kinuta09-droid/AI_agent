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

from tools.tool_vault_index import (
    _chunk_markdown, CHUNK_MIN_CHARS,
    _is_table_delimiter, _split_by_paragraphs, _split_table_block, _table_start_index,
)


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


# =========================================================
# Phase 3-1: 表を壊さないこと (2026-07-30 追加)
#
# 背景: Markdown の表は空行を含まない1ブロックなので、_split_by_paragraphs から
# 見ると「巨大な単一段落」に見える。従来は para[:max_chars] のハードスプリットに
# 落ちていたため、**行の途中で切断され2片目以降がヘッダ行を失っていた**。
# 検索で当たっても `| 3 | 0.82 | 0.71 |` のような列名の無い数字の羅列になり、
# LLM には解釈できない。壊れたことはログに一行も出ない (無言の死)。
# =========================================================

MAX = 1200
OVERLAP = 150


def _legacy_split_by_paragraphs(text: str, max_chars: int, overlap: int) -> list:
    """表対応**前**の _split_by_paragraphs をそのまま写したもの。

    「表以外は挙動不変」は宣言では確かめられない。両方を走らせて突き合わせる。
    """
    paragraphs = text.split("\n\n")
    pieces = []
    current = ""
    for para in paragraphs:
        while len(para) > max_chars:
            head, para = para[:max_chars], para[max_chars - overlap:]
            if current:
                pieces.append(current)
                current = ""
            pieces.append(head)

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > max_chars and current:
            pieces.append(current)
            current = current[-overlap:] + "\n\n" + para if overlap > 0 else para
        else:
            current = candidate
    if current.strip():
        pieces.append(current)
    return pieces


def _make_table(rows: int, cols: int = 4) -> str:
    header = "| " + " | ".join(f"列{c}" for c in range(1, cols + 1)) + " |"
    delim = "|" + "|".join(["---"] * cols) + "|"
    body = [
        "| " + " | ".join(f"値{r}-{c}" for c in range(1, cols + 1)) + " |"
        for r in range(1, rows + 1)
    ]
    return "\n".join([header, delim] + body)


def _pipe_rows(text: str) -> list:
    return [ln for ln in text.split("\n") if ln.strip().startswith("|")]


def test_table_delimiter_detection():
    assert _is_table_delimiter("|---|---|")
    assert _is_table_delimiter("| :--- | ---: | :---: |")
    assert _is_table_delimiter("---|---")
    # 水平線を表と誤認すると、表でない本文まで行単位分割に落ちる
    assert not _is_table_delimiter("---")
    assert not _is_table_delimiter("- - -")
    assert not _is_table_delimiter("")
    assert not _is_table_delimiter("| データ | 値 |")
    print("✅ table: 区切り行の判定 (水平線 --- を誤認しない)")


def test_table_start_index():
    lines = ["前置きの文章です。", "| A | B |", "|---|---|", "| 1 | 2 |"]
    assert _table_start_index(lines) == 1, _table_start_index(lines)
    assert _table_start_index(["a | b", "---", "本文"]) is None
    assert _table_start_index(["本文だけ"]) is None
    print("✅ table: 開始位置の検出 (前置きがあっても見つける)")


def test_split_table_repeats_header_and_keeps_rows_intact():
    table = _make_table(rows=120)
    assert len(table) > MAX, f"検体が上限を超えていない ({len(table)}字)"
    pieces = _split_table_block(table, MAX)
    assert pieces and len(pieces) > 1, f"分割されていない: {len(pieces or [])}片"

    header, delim = table.split("\n")[0], table.split("\n")[1]
    for i, piece in enumerate(pieces, 1):
        lines = piece.split("\n")
        assert lines[0] == header, f"{i}片目にヘッダ行が無い: {lines[0]!r}"
        assert lines[1] == delim, f"{i}片目に区切り行が無い: {lines[1]!r}"
        for ln in lines[2:]:
            assert ln.startswith("|") and ln.rstrip().endswith("|"), \
                f"{i}片目で行が途中で切れている: {ln!r}"
    print(f"✅ table: {len(pieces)}片すべてにヘッダ再掲・行の切断なし")


def test_split_table_loses_no_row():
    table = _make_table(rows=200)
    pieces = _split_table_block(table, MAX)
    original = _pipe_rows(table)[2:]
    restored = []
    for piece in pieces:
        restored.extend(_pipe_rows(piece)[2:])
    assert restored == original, \
        f"行が欠落/重複している: 元 {len(original)}行 → 復元 {len(restored)}行"
    print(f"✅ table: {len(original)}行が欠落も重複もせず保存される")


def test_oversized_single_row_is_not_cut():
    """1行が単体で上限超でも切らない (行の完全性 > 長さ制約という設計判断)。"""
    long_row = "| " + "あ" * 2000 + " |"
    pieces = _split_table_block("| A |\n|---|\n" + long_row, MAX)
    assert len(pieces) == 1, pieces
    assert long_row in pieces[0], "長大な行が切断されている"
    print("✅ table: 長すぎる1行は上限を超えてでも切らない")


def test_preamble_is_separated_from_table():
    text = "この表は手法Aと既存3手法の精度比較です。\n" + _make_table(rows=120)
    pieces = _split_table_block(text, MAX)
    assert pieces[0].strip() == "この表は手法Aと既存3手法の精度比較です。", pieces[0]
    assert "|" not in pieces[0], "前置きに表が混ざっている"
    assert all("この表は手法A" not in p for p in pieces[1:]), "前置きが各片に重複している"
    print("✅ table: 表の前の地の文は分離される")


def test_non_table_returns_none():
    """表として解釈できない段落は None (従来のハードスプリットへ委ねる)。"""
    assert _split_table_block("ただの長い文章。" * 300, MAX) is None
    print("✅ table: 表でない段落は None を返す")


def test_table_free_input_is_identical_to_legacy():
    """表を含まない入力では修正前と**完全に同じ**結果になること。

    これが崩れると、表対応のついでに Vault の全ノートが再チャンクされ、
    意味検索の挙動が黙って変わる (原因を追えなくなる)。
    """
    samples = [
        "短い段落。",
        "段落A。" * 50 + "\n\n" + "段落B。" * 50,
        "あ" * 3000,
        ("見出しの無い長文。" * 80 + "\n\n") * 5,
        "箇条書き:\n- 項目1\n- 項目2\n\n" + "本文。" * 400,
    ]
    for i, text in enumerate(samples, 1):
        assert "|" not in text, "この検体は表を含まない前提"
        new = _split_by_paragraphs(text, MAX, OVERLAP)
        old = _legacy_split_by_paragraphs(text, MAX, OVERLAP)
        assert new == old, \
            f"検体{i}: 表を含まない入力で分割結果が変わった (新{len(new)}片/旧{len(old)}片)"
    print(f"✅ table: 表を含まない{len(samples)}検体で修正前と完全一致")


def test_small_table_is_never_split():
    table = _make_table(rows=5)
    assert len(table) < MAX
    chunks = _chunk_markdown("前書き。\n\n" + table + "\n\n後書き。",
                             max_chars=MAX, overlap=OVERLAP)
    assert table in "\n".join(c["text"] for c in chunks), "上限未満の表が分割されている"
    print("✅ table: 上限未満の表は1片のまま")


def test_chunk_markdown_keeps_table_headers():
    """見出し配下の巨大な表を _chunk_markdown 経由で通しても不変条件が保たれること。"""
    table = _make_table(rows=150)
    header = table.split("\n")[0]
    doc = (
        "---\ntitle: テスト\n---\n"
        "## 実験結果\n以下の表は3手法の比較です。\n" + table + "\n\n"
        "## 考察\n" + "考察の本文。" * 30
    )
    chunks = _chunk_markdown(doc, max_chars=MAX, overlap=OVERLAP)
    table_chunks = [c for c in chunks if "|" in c["text"]]
    assert len(table_chunks) > 1, f"表が分割されていない: {len(table_chunks)}"
    for c in table_chunks:
        assert header in c["text"], f"表チャンクにヘッダが無い:\n{c['text'][:120]}"
        assert c["heading"] == "実験結果", f"見出しの帰属が壊れている: {c['heading']}"
    assert any(c["heading"] == "考察" for c in chunks), "他セクションが消えている"
    print(f"✅ table: _chunk_markdown 経由でも {len(table_chunks)}件すべてにヘッダ")


def test_no_header_only_chunks():
    """データ行の無い「ヘッダだけのチャンク」を作らない (埋め込みの無駄)。"""
    chunks = _chunk_markdown("## 表\n" + _make_table(rows=150),
                             max_chars=MAX, overlap=OVERLAP)
    for c in chunks:
        if "|" in c["text"]:
            rows = [ln for ln in c["text"].split("\n") if ln.strip().startswith("| 値")]
            assert rows, f"データ行の無い表チャンクがある:\n{c['text']}"
    print("✅ table: ヘッダだけのチャンクを作らない")


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

    # Phase 3-1: 表
    test_table_delimiter_detection()
    test_table_start_index()
    test_split_table_repeats_header_and_keeps_rows_intact()
    test_split_table_loses_no_row()
    test_oversized_single_row_is_not_cut()
    test_preamble_is_separated_from_table()
    test_non_table_returns_none()
    test_table_free_input_is_identical_to_legacy()
    test_small_table_is_never_split()
    test_chunk_markdown_keeps_table_headers()
    test_no_header_only_chunks()

    print("\n🎉 ALL CHUNKING TESTS PASSED")
