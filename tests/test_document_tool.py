# -*- coding: utf-8 -*-
"""
tools/tool_document.py (Phase 2: エージェントのツールにする) のユニットテスト

方針:
  - docling / pypdf は要らない。capture_document / summarize_document / pypdf を
    スタブに差し替え、実際の変換・Gemini呼び出し・pypdfパースが起きないことを
    前提にする (tests/test_docling_client.py と同じ作法)。
  - 本番 Vault (VAULT_ROOT) / logs/paper_cache には触らない。VAULT_ROOT は
    テスト内で一時ディレクトリへ差し替える (test_paper_radar.py の
    PAPERS_DIR 差し替えと同じ作法)。

実行: py tests/test_document_tool.py
"""
import os
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_document as doc
from lib.bot_errors import is_failure_result
from lib.docling_client import CaptureError


# ---------------------------------------------------------
# [1] check_document_caps: サイズ上限
# ---------------------------------------------------------

def test_check_document_caps_rejects_oversized_local_file():
    orig_max = doc.MAX_DOCUMENT_SIZE_BYTES
    doc.MAX_DOCUMENT_SIZE_BYTES = 10  # 10バイトに絞って確実に超過させる
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("x" * 100)
            result = doc.check_document_caps(path)
    finally:
        doc.MAX_DOCUMENT_SIZE_BYTES = orig_max
    assert is_failure_result(result), result
    assert "サイズ" in result, result
    print("✅ [1] check_document_caps: サイズ超過を拒否")


def test_check_document_caps_ignores_urls_and_missing_paths():
    assert doc.check_document_caps("https://example.com/x.pdf") == ""
    assert doc.check_document_caps(os.path.join(tempfile.gettempdir(), "nonexistent_arms_doc.pdf")) == ""
    print("✅ [1] check_document_caps: URL/不在パスは対象外 (空文字)")


# ---------------------------------------------------------
# [2] check_document_caps: ページ数上限 (pypdf スタブ)
# ---------------------------------------------------------

class _FakePdfReader:
    """テストごとに差し替え可能なページ数を返すスタブ。"""
    pages_count = 1

    def __init__(self, path):
        self.pages = [None] * type(self).pages_count


def test_check_document_caps_rejects_over_page_pdf_via_stubbed_pypdf():
    fake_pypdf = types.ModuleType("pypdf")
    fake_pypdf.PdfReader = _FakePdfReader
    orig_module = sys.modules.get("pypdf")
    orig_max_pages = doc.MAX_DOCUMENT_PAGES
    sys.modules["pypdf"] = fake_pypdf
    doc.MAX_DOCUMENT_PAGES = 5
    _FakePdfReader.pages_count = 6  # 上限超過
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "many_pages.pdf")
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4 dummy")
            result = doc.check_document_caps(path)
    finally:
        doc.MAX_DOCUMENT_PAGES = orig_max_pages
        if orig_module is None:
            sys.modules.pop("pypdf", None)
        else:
            sys.modules["pypdf"] = orig_module
    assert is_failure_result(result), result
    assert "ページ数" in result, result
    print("✅ [2] check_document_caps: ページ数超過を拒否 (スタブpypdf)")


def test_check_document_caps_accepts_pdf_within_page_cap_via_stubbed_pypdf():
    fake_pypdf = types.ModuleType("pypdf")
    fake_pypdf.PdfReader = _FakePdfReader
    orig_module = sys.modules.get("pypdf")
    orig_max_pages = doc.MAX_DOCUMENT_PAGES
    sys.modules["pypdf"] = fake_pypdf
    doc.MAX_DOCUMENT_PAGES = 5
    _FakePdfReader.pages_count = 3  # 上限内
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "few_pages.pdf")
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4 dummy")
            result = doc.check_document_caps(path)
    finally:
        doc.MAX_DOCUMENT_PAGES = orig_max_pages
        if orig_module is None:
            sys.modules.pop("pypdf", None)
        else:
            sys.modules["pypdf"] = orig_module
    assert result == "", result
    print("✅ [2] check_document_caps: ページ数が上限内なら通過")


def test_check_document_caps_skips_page_check_when_pypdf_missing():
    """pypdf が import できない環境でも例外を伝播せず、サイズ判定のみで続行すること。"""
    orig_module = sys.modules.get("pypdf")
    sys.modules["pypdf"] = None  # `from pypdf import ...` が ImportError を投げる標準トリック
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "whatever.pdf")
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4 dummy")
            result = doc.check_document_caps(path)
    finally:
        if orig_module is None:
            sys.modules.pop("pypdf", None)
        else:
            sys.modules["pypdf"] = orig_module
    assert result == "", result
    print("✅ [2] check_document_caps: pypdf 未導入でも例外を伝播せず続行")


def test_check_document_caps_skips_page_check_when_pdf_unparseable():
    """pypdf はあるが壊れたPDFで解析に失敗するケースも、握りつぶして続行すること。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "garbage.pdf")
        with open(path, "wb") as f:
            f.write(b"not a real pdf at all")
        result = doc.check_document_caps(path)
    assert result == "", result
    print("✅ [2] check_document_caps: 解析失敗のPDFでも例外を伝播せず続行")


# ---------------------------------------------------------
# [3] tool_read_document
# ---------------------------------------------------------

def test_read_document_short_circuits_on_cap_violation():
    calls = []
    orig_capture = doc.capture_document
    orig_check = doc.check_document_caps
    doc.capture_document = lambda *a, **kw: calls.append(1) or {"text": "x"}
    doc.check_document_caps = lambda p: "Error: 上限超過テスト"
    try:
        result = doc.tool_read_document("dummy.pdf")
    finally:
        doc.capture_document = orig_capture
        doc.check_document_caps = orig_check
    assert result == "Error: 上限超過テスト", result
    assert not calls, "上限違反時に capture_document を呼んではいけない"
    print("✅ [3] tool_read_document: 上限違反時は capture_document を呼ばず即返す")


def test_read_document_returns_header_and_text():
    orig_capture = doc.capture_document
    orig_check = doc.check_document_caps
    doc.check_document_caps = lambda p: ""
    doc.capture_document = lambda *a, **kw: {
        "text": "本文です", "backend": "docling", "chars": 1234,
        "truncated": True, "warnings": ["注意事項A"],
    }
    try:
        result = doc.tool_read_document("dummy.pdf")
    finally:
        doc.capture_document = orig_capture
        doc.check_document_caps = orig_check
    assert "docling で全文を構造化" in result, result
    assert "先頭のみ" in result, result
    assert "注意事項A" in result, result
    assert result.rstrip().endswith("本文です"), result
    print("✅ [3] tool_read_document: ヘッダ(backend/truncated/warnings)+本文を返す")


def test_read_document_capture_error_is_a_failure_string():
    orig_capture = doc.capture_document
    orig_check = doc.check_document_caps
    doc.check_document_caps = lambda p: ""

    def _boom(*a, **kw):
        raise CaptureError("全経路失敗")
    doc.capture_document = _boom
    try:
        result = doc.tool_read_document("dummy.pdf")
    finally:
        doc.capture_document = orig_capture
        doc.check_document_caps = orig_check
    assert is_failure_result(result), result
    print("✅ [3] tool_read_document: CaptureError は失敗文字列になる")


# ---------------------------------------------------------
# [4] _render_source_link
# ---------------------------------------------------------

def test_render_source_link_variants():
    assert doc._render_source_link("https://example.com/paper.pdf") == \
        "[元ファイル](https://example.com/paper.pdf)"

    orig_vault = doc.VAULT_ROOT
    with tempfile.TemporaryDirectory() as vault_tmp, tempfile.TemporaryDirectory() as outside_tmp:
        doc.VAULT_ROOT = vault_tmp
        try:
            inside_path = os.path.join(vault_tmp, "30_Main", "note_source.pdf")
            os.makedirs(os.path.dirname(inside_path), exist_ok=True)
            with open(inside_path, "w", encoding="utf-8") as f:
                f.write("x")
            inside_result = doc._render_source_link(inside_path)
            assert inside_result.startswith("[[") and inside_result.endswith("]]"), inside_result
            assert "30_Main" in inside_result, inside_result

            outside_path = os.path.join(outside_tmp, "temp_upload.pdf")
            with open(outside_path, "w", encoding="utf-8") as f:
                f.write("x")
            outside_result = doc._render_source_link(outside_path)
            assert "直接リンクなし" in outside_result, outside_result
            assert "temp_upload.pdf" in outside_result, outside_result
        finally:
            doc.VAULT_ROOT = orig_vault
    print("✅ [4] _render_source_link: URL/Vault内/Vault外の3分岐")


# ---------------------------------------------------------
# [5] tool_import_document
# ---------------------------------------------------------

_FAKE_CAPTURE = {"text": "本文全文", "backend": "docling", "chars": 100,
                 "truncated": False, "warnings": []}
_FAKE_DIGEST = {
    "title": "テスト文書のタイトル", "doc_type": "レポート",
    "one_liner": "これはテスト文書です", "summary": "要約本文。",
    "key_points": ["要点1", "要点2"], "action_items": ["アクション1"],
    "keywords": ["テスト", "文書"],
}


def _patch_import_document(tmp_vault, capture=None, digest=None, capture_raises=None):
    orig = (doc.VAULT_ROOT, doc.capture_document, doc.summarize_document)
    doc.VAULT_ROOT = tmp_vault
    if capture_raises is not None:
        def _boom(*a, **kw):
            raise capture_raises
        doc.capture_document = _boom
    else:
        doc.capture_document = lambda *a, **kw: dict(capture or _FAKE_CAPTURE)
    doc.summarize_document = lambda *a, **kw: dict(digest) if digest is not None else dict(_FAKE_DIGEST)
    return orig


def _restore_import_document(orig):
    doc.VAULT_ROOT, doc.capture_document, doc.summarize_document = orig


def test_import_document_saves_note_with_backend_and_wikilink():
    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_import_document(tmp)
        try:
            result = doc.tool_import_document("dummy.pdf", folder="30_Main")
        finally:
            _restore_import_document(orig)
        assert not is_failure_result(result), result
        assert "[[" in result, result

        saved_dir = os.path.join(tmp, "30_Main")
        files = [f for f in os.listdir(saved_dir) if f.endswith(".md")]
        assert len(files) == 1, files
        with open(os.path.join(saved_dir, files[0]), encoding="utf-8") as f:
            content = f.read()
        assert "docling で全文を構造化" in content, content
        assert _FAKE_DIGEST["one_liner"] in content, content
        assert "要点1" in content, content
    print("✅ [5] tool_import_document: backend+要約付きノートを保存しwikilinkを返す")


def test_import_document_duplicate_filename_gets_timestamp_suffix():
    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_import_document(tmp)
        try:
            doc.tool_import_document("dummy.pdf", folder="30_Main")
            saved_dir = os.path.join(tmp, "30_Main")
            first_files = sorted(os.listdir(saved_dir))
            assert len(first_files) == 1, first_files
            with open(os.path.join(saved_dir, first_files[0]), encoding="utf-8") as f:
                first_content = f.read()

            doc.tool_import_document("dummy.pdf", folder="30_Main")
            second_files = sorted(os.listdir(saved_dir))
        finally:
            _restore_import_document(orig)
        assert len(second_files) == 2, second_files
        assert first_files[0] in second_files, second_files
        with open(os.path.join(saved_dir, first_files[0]), encoding="utf-8") as f:
            assert f.read() == first_content, "1件目のファイルが上書きされている"
    print("✅ [5] tool_import_document: 同名衝突はタイムスタンプ接尾辞で退避 (1件目は無傷)")


def test_import_document_malformed_summary_is_error_and_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_import_document(tmp, digest={})
        try:
            result = doc.tool_import_document("dummy.pdf", folder="30_Main")
        finally:
            _restore_import_document(orig)
        assert is_failure_result(result), result
        saved_dir = os.path.join(tmp, "30_Main")
        assert not os.path.exists(saved_dir) or not os.listdir(saved_dir), \
            "要約が空なのにファイルが書かれている"
    print("✅ [5] tool_import_document: 要約が空形式なら失敗を返しファイルを書かない")


def test_import_document_capture_error_is_a_failure_string():
    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_import_document(tmp, capture_raises=CaptureError("全経路失敗"))
        try:
            result = doc.tool_import_document("dummy.pdf", folder="30_Main")
        finally:
            _restore_import_document(orig)
        assert is_failure_result(result), result
    print("✅ [5] tool_import_document: CaptureError は失敗文字列になる")


# ---------------------------------------------------------
# [7] render_document_draft (純関数) / mode="sns"|"note"|"both"|"none"
# ---------------------------------------------------------

def test_render_document_draft_variants():
    """sns本文のみ・記事のみ・両方・両方空、の4パターンで見出しの有無が変わること。"""
    only_sns = doc.render_document_draft("タイトルA", "SNS本文", "", "")
    assert "## X (Twitter) 投稿" in only_sns, only_sns
    assert "SNS本文" in only_sns, only_sns
    assert "note.com 記事下書き" not in only_sns, only_sns

    only_article = doc.render_document_draft("タイトルB", "", "記事タイトル", "記事本文")
    assert "## X (Twitter) 投稿" not in only_article, only_article
    assert "## note.com 記事下書き" in only_article, only_article
    assert "## X Articles 下書き" in only_article, only_article
    assert only_article.count("記事本文") == 2, only_article  # note.com + X Articles の両方に同一本文
    assert "記事タイトル" in only_article, only_article

    both = doc.render_document_draft("タイトルC", "SNS本文C", "記事タイトルC", "記事本文C")
    assert "## X (Twitter) 投稿" in both, both
    assert "## note.com 記事下書き" in both, both
    assert "## X Articles 下書き" in both, both

    empty = doc.render_document_draft("タイトルD", "", "", "")
    assert "## X (Twitter) 投稿" not in empty, empty
    assert "## note.com 記事下書き" not in empty, empty
    assert empty.startswith("# ✍️ 投稿下書き — タイトルD"), empty
    print("✅ [7] render_document_draft: sns本文のみ/記事のみ/両方/両方空の4パターン")


def test_draft_subject_meta_uses_url_as_abs_url_only_for_http_sources():
    """path_or_url が http(s) の場合だけ abs_url へ伝播すること (回帰テスト)。

    以前は abs_url を一律 "" にしていたため、ReAct 経由で URL を直接指定した
    ケース (本編ノートには _render_source_link が [元ファイル](URL) を載せる)
    でも下書きの末尾リンクだけが握りつぶされていた。
    """
    assert doc._draft_subject_meta("T", "https://example.com/a.pdf")["abs_url"] == "https://example.com/a.pdf"
    assert doc._draft_subject_meta("T", "HTTPS://example.com/a.pdf")["abs_url"] == "HTTPS://example.com/a.pdf"
    assert doc._draft_subject_meta("T", "C:\\tmp\\arms_doc_xyz\\a.pdf")["abs_url"] == ""
    assert doc._draft_subject_meta("T", "")["abs_url"] == ""
    print("✅ [7] _draft_subject_meta: http(s)ソースのみ abs_url へ伝播する")


def test_import_document_mode_sns_generates_draft_with_stubbed_build_sns_post():
    """mode='sns' で doc.build_sns_post をスタブし、下書きファイルが本編と別に
    保存されること。Gemini は一切呼ばない。"""
    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_import_document(tmp)
        orig_build_sns = doc.build_sns_post
        doc.build_sns_post = lambda dd, adapter: "スタブSNS投稿"
        try:
            result = doc.tool_import_document("dummy.pdf", folder="30_Main", mode="sns")
        finally:
            doc.build_sns_post = orig_build_sns
            _restore_import_document(orig)
        assert not is_failure_result(result), result
        assert result.count("[[") == 2, f"本編+下書きの2件分のwikilinkが無い: {result!r}"

        saved_dir = os.path.join(tmp, "30_Main")
        files = sorted(os.listdir(saved_dir))
        assert len(files) == 2, files
        draft_files = [f for f in files if f.endswith("_draft.md")]
        main_files = [f for f in files if not f.endswith("_draft.md")]
        assert len(draft_files) == 1, files
        assert len(main_files) == 1, files

        with open(os.path.join(saved_dir, draft_files[0]), encoding="utf-8") as f:
            draft_content = f.read()
        assert "スタブSNS投稿" in draft_content, draft_content

        with open(os.path.join(saved_dir, main_files[0]), encoding="utf-8") as f:
            main_content = f.read()
        assert _FAKE_DIGEST["one_liner"] in main_content, main_content
    print("✅ [7] tool_import_document mode=sns: 下書きファイルを本編と別に保存する")


def test_import_document_mode_note_generates_draft_with_stubbed_build_note_article():
    """mode='note' で doc.build_note_article をスタブし、下書きに記事タイトル/本文が
    反映されること。"""
    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_import_document(tmp)
        orig_build_note = doc.build_note_article
        doc.build_note_article = lambda dd, adapter, capture: {
            "title": "記事タイトル", "body": "記事本文",
        }
        try:
            result = doc.tool_import_document("dummy.pdf", folder="30_Main", mode="note")
        finally:
            doc.build_note_article = orig_build_note
            _restore_import_document(orig)
        assert not is_failure_result(result), result

        saved_dir = os.path.join(tmp, "30_Main")
        draft_files = [f for f in os.listdir(saved_dir) if f.endswith("_draft.md")]
        assert len(draft_files) == 1, os.listdir(saved_dir)
        with open(os.path.join(saved_dir, draft_files[0]), encoding="utf-8") as f:
            draft_content = f.read()
        assert "記事タイトル" in draft_content, draft_content
        assert "記事本文" in draft_content, draft_content
    print("✅ [7] tool_import_document mode=note: 下書きに記事タイトル/本文が反映される")


def test_import_document_mode_none_writes_no_draft_file():
    """mode='none' (既定) では下書きファイルを一切生成しないこと。

    既存テストは "[[" in result しか見ておらず、下書きファイルの有無という
    観点が抜けていたための明示的な回帰テスト。
    """
    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_import_document(tmp)
        try:
            result = doc.tool_import_document("dummy.pdf", folder="30_Main", mode="none")
        finally:
            _restore_import_document(orig)
        assert not is_failure_result(result), result
        saved_dir = os.path.join(tmp, "30_Main")
        files = os.listdir(saved_dir)
        assert not any(f.endswith("_draft.md") for f in files), \
            f"mode=none なのに下書きファイルが生成されている: {files}"
        assert len(files) == 1, files
    print("✅ [7] tool_import_document mode=none (既定): 下書きファイルは一切生成しない")


def test_import_document_mode_sns_build_sns_post_failure_is_error_string():
    """build_sns_post が例外を投げたら握りつぶさず Error: 文字列を返すこと。"""
    with tempfile.TemporaryDirectory() as tmp:
        orig = _patch_import_document(tmp)
        orig_build_sns = doc.build_sns_post

        def _boom(dd, adapter):
            raise RuntimeError("gemini down")

        doc.build_sns_post = _boom
        try:
            result = doc.tool_import_document("dummy.pdf", folder="30_Main", mode="sns")
        finally:
            doc.build_sns_post = orig_build_sns
            _restore_import_document(orig)
        assert is_failure_result(result), result
    print("✅ [7] tool_import_document mode=sns: build_sns_post 例外は Error: で返す (握りつぶさない)")


# ---------------------------------------------------------
# [6] registry
# ---------------------------------------------------------

def test_registry_wrapper_failure_prefix_is_detected():
    assert is_failure_result("[read_document error] boom")
    assert is_failure_result("[import_document error] boom")
    print("✅ [6] registry wrapper: 失敗文字列を is_failure_result が拾う")


def test_registry_entries_registered():
    import lib.tool_registry as registry
    names = {e["name"] for e in registry.TOOL_ENTRIES}
    assert {"tool_read_document", "tool_import_document"} <= names, names
    print("✅ [6] registry: tool_read_document / tool_import_document が登録されている")


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
