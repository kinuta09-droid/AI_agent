# -*- coding: utf-8 -*-
"""
lib/docling_client.py (Phase 1: 変換層の一般化) のユニットテスト

方針:
  - docling 本体は要らない。_get_converter / convert_with_docling / is_available を
    スタブに差し替え、実際に import が起きないことを前提にする
    (plans/docling-integration.md: 「docling が無い環境」が既定)。
  - logs/paper_cache (本番の変換キャッシュ) には触らない。CACHE_DIR は
    テスト内で一時ディレクトリへ差し替える (test_curate_note_save の
    「本番カウンタを見続けていた」事故の再発防止と同じ作法)。

実行: py tests/test_docling_client.py
"""
import os
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.docling_client as docling_client


# ---------------------------------------------------------
# フェイク docling オブジェクト (import せずに convert_with_docling を試す)
# ---------------------------------------------------------

class _FakeDataFrame:
    def __init__(self, csv_text):
        self._csv_text = csv_text

    def to_csv(self, index=False):
        return self._csv_text


class _FakeTable:
    def __init__(self, markdown, caption, csv_text):
        self._markdown = markdown
        self._caption = caption
        self._csv_text = csv_text

    def export_to_markdown(self, doc):
        return self._markdown

    def caption_text(self, doc):
        return self._caption

    def export_to_dataframe(self, doc):
        return _FakeDataFrame(self._csv_text)


class _FakeDocument:
    def __init__(self, markdown, tables):
        self._markdown = markdown
        self.tables = tables

    def export_to_markdown(self):
        return self._markdown


class _FakeConvertResult:
    def __init__(self, document):
        self.document = document


class _FakeConverter:
    def __init__(self, document):
        self._document = document
        self.convert_calls = []

    def convert(self, source):
        self.convert_calls.append(source)
        return _FakeConvertResult(self._document)


# ---------------------------------------------------------
# [0] is_available のプロセス内キャッシュ
# ---------------------------------------------------------

def test_is_available_result_is_cached_in_process():
    orig_cache = docling_client._AVAILABLE_CACHE
    docling_client._AVAILABLE_CACHE = None
    try:
        first = docling_client.is_available()
        assert docling_client._AVAILABLE_CACHE is first, "判定結果がキャッシュへ書き戻されていない"
        second = docling_client.is_available()
        assert second == first, "2回目の呼び出しで結果がぶれた"
    finally:
        docling_client._AVAILABLE_CACHE = orig_cache
    print("✅ [0] is_available: プロセス内でキャッシュされる")


# ---------------------------------------------------------
# [1] 表の抽出と text 互換
# ---------------------------------------------------------

def test_convert_with_docling_extracts_tables_and_keeps_text_compat():
    """export_to_markdown() だけでなく tables も同梱し、text 側の互換は保つこと。"""
    markdown = "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    document = _FakeDocument(markdown, [_FakeTable(
        "| a | b |\n|---|---|\n| 1 | 2 |\n", "Table 1: sample", "a,b\n1,2\n")])
    fake_converter = _FakeConverter(document)

    orig_get_converter = docling_client._get_converter
    docling_client._get_converter = lambda **kwargs: fake_converter
    try:
        result = docling_client.convert_with_docling("dummy.pdf")
    finally:
        docling_client._get_converter = orig_get_converter

    assert result["text"] == markdown, result
    assert len(result["tables"]) == 1, result
    table = result["tables"][0]
    assert table["caption"] == "Table 1: sample", table
    assert "a,b" in table["csv"] and "1,2" in table["csv"], table
    print("✅ [1] convert_with_docling: tables を取り出しつつ text 互換を保つ")


def test_convert_with_docling_survives_single_table_extraction_failure():
    """1個の表の抽出に失敗しても、本文と他の表は無駄にしないこと。"""
    class _BoomTable(_FakeTable):
        def export_to_markdown(self, doc):
            raise RuntimeError("boom")

    markdown = "本文だけは取れている"
    document = _FakeDocument(markdown, [
        _BoomTable("x", "x", "x"),
        _FakeTable("| a |\n|---|\n| 1 |\n", "Table 2", "a\n1\n"),
    ])
    fake_converter = _FakeConverter(document)

    orig_get_converter = docling_client._get_converter
    docling_client._get_converter = lambda **kwargs: fake_converter
    try:
        result = docling_client.convert_with_docling("dummy.pdf")
    finally:
        docling_client._get_converter = orig_get_converter

    assert result["text"] == markdown, result
    assert len(result["tables"]) == 1 and result["tables"][0]["caption"] == "Table 2", result
    print("✅ [1] convert_with_docling: 1表の抽出失敗が全体を巻き込まない")


# ---------------------------------------------------------
# [2] ocr / table_structure オプションの伝播
# ---------------------------------------------------------

def test_ocr_and_table_structure_options_propagate_to_converter():
    document = _FakeDocument("text", [])
    fake_converter = _FakeConverter(document)
    calls = []

    def _stub_get_converter(**kwargs):
        calls.append(kwargs)
        return fake_converter

    orig_get_converter = docling_client._get_converter
    docling_client._get_converter = _stub_get_converter
    try:
        docling_client.convert_with_docling("dummy.pdf", ocr=True, table_structure=False)
    finally:
        docling_client._get_converter = orig_get_converter

    assert calls == [{"ocr": True, "table_structure": False}], calls
    print("✅ [2] convert_with_docling: ocr/table_structure が変換器生成に伝わる")


# ---------------------------------------------------------
# [3] ローカルファイルの mtime キャッシュ
# ---------------------------------------------------------

def test_mtime_change_triggers_reconversion_cache_hit_when_unchanged():
    """同じパスでも mtime が変われば再変換、変わらなければキャッシュを使うこと。

    これが崩れると「ローカルファイルを更新したのに古い変換結果が返る」
    無言の嘘になる (plans/docling-integration.md Phase 1-1)。
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "doc.pdf")
        with open(src, "w", encoding="utf-8") as f:
            f.write("dummy pdf bytes")

        calls = []

        def _fake_convert(source, ocr=False, table_structure=True):
            calls.append(source)
            return {"text": f"converted #{len(calls)}", "tables": []}

        orig_cache_dir = docling_client.CACHE_DIR
        orig_available = docling_client.is_available
        orig_convert = docling_client.convert_with_docling
        docling_client.CACHE_DIR = os.path.join(tmp, "cache")
        docling_client.is_available = lambda: True
        docling_client.convert_with_docling = _fake_convert
        try:
            r1 = docling_client.capture_document(src, [], use_cache=True)
            assert r1["backend"] == "docling" and len(calls) == 1, (r1, calls)

            r2 = docling_client.capture_document(src, [], use_cache=True)
            assert r2["backend"] == "cache" and len(calls) == 1, "mtime不変なのに再変換された"
            assert r2["text"] == r1["text"], "キャッシュから返した内容が変換結果と食い違う"

            future = os.path.getmtime(src) + 5  # FSの時刻解像度対策で明示的に進める
            os.utime(src, (future, future))

            r3 = docling_client.capture_document(src, [], use_cache=True)
            assert r3["backend"] == "docling" and len(calls) == 2, "mtimeが変わったのに再変換されなかった"
        finally:
            docling_client.CACHE_DIR = orig_cache_dir
            docling_client.is_available = orig_available
            docling_client.convert_with_docling = orig_convert
    print("✅ [3] capture_document: mtime変化で再変換・不変ならキャッシュ")


def test_tables_survive_cache_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "doc.pdf")
        with open(src, "w", encoding="utf-8") as f:
            f.write("dummy pdf bytes")

        tables = [{"markdown": "| a |\n|---|\n| 1 |\n", "caption": "Table 1", "csv": "a\n1\n"}]

        def _fake_convert(source, ocr=False, table_structure=True):
            return {"text": "本文", "tables": tables}

        orig_cache_dir = docling_client.CACHE_DIR
        orig_available = docling_client.is_available
        orig_convert = docling_client.convert_with_docling
        docling_client.CACHE_DIR = os.path.join(tmp, "cache")
        docling_client.is_available = lambda: True
        docling_client.convert_with_docling = _fake_convert
        try:
            r1 = docling_client.capture_document(src, [], use_cache=True)
            assert r1["tables"] == tables, r1

            r2 = docling_client.capture_document(src, [], use_cache=True)
            assert r2["backend"] == "cache", "2回目はキャッシュ経由のはず"
            assert r2["tables"] == tables, "キャッシュ経由で tables が失われた"
        finally:
            docling_client.CACHE_DIR = orig_cache_dir
            docling_client.is_available = orig_available
            docling_client.convert_with_docling = orig_convert
    print("✅ [4] capture_document: tables がキャッシュを跨いで保持される")


# ---------------------------------------------------------
# [6] _resolve_cache_key の stat 一発判定 (grill 指摘の TOCTOU 修正)
# ---------------------------------------------------------

def test_resolve_cache_key_treats_directories_as_non_local():
    """ディレクトリは os.stat() が成功するが S_ISREG ではないので、URL 扱い
    (mtime を含めない) に落ちること。os.path.isfile() 相当の区別を保つ。"""
    with tempfile.TemporaryDirectory() as tmp:
        key = docling_client._resolve_cache_key(tmp, False, True)
    assert key == f"docling::{tmp}::ocr=False::ts=True", key
    print("✅ [6] _resolve_cache_key: ディレクトリはローカルファイル扱いしない")


def test_resolve_cache_key_survives_stat_race():
    """isfile→stat の間にファイルが消える (TOCTOU) を模しても例外を伝播しないこと。

    Phase 2 で Discord 添付を一時ファイルへ落として即変換する経路ができると、
    ウイルススキャン等でこの隙間が現実的なタイミングになる。
    """
    orig_stat = docling_client.os.stat

    def _boom(path, *a, **kw):
        raise FileNotFoundError(f"gone: {path}")

    docling_client.os.stat = _boom
    try:
        key = docling_client._resolve_cache_key("some/vanished/file.pdf", False, True)
    finally:
        docling_client.os.stat = orig_stat
    assert key == "docling::some/vanished/file.pdf::ocr=False::ts=True", key
    print("✅ [6] _resolve_cache_key: stat 中の例外を握りつぶしURL扱いへ落ちる")


# ---------------------------------------------------------
# [7] _get_converter の実体 (PdfPipelineOptions への配線)
# ---------------------------------------------------------

def test_get_converter_wires_pipeline_options_via_stubbed_docling_modules():
    """_get_converter 自体を検証する。

    他のテストは _get_converter をまるごとスタブへ差し替えているため、
    do_ocr/do_table_structure/artifacts_path を実際に PdfPipelineOptions へ
    渡す配線そのものは一度も実行されていなかった (grill 指摘)。ここでは
    docling の該当モジュールを sys.modules に差し込み、_get_converter の
    中身 (import 文を含む) を実際に走らせて検証する。
    """
    created_options = []
    created_format_options = []

    class _FakePdfPipelineOptions:
        def __init__(self, **kwargs):
            created_options.append(kwargs)

    class _FakePdfFormatOption:
        def __init__(self, pipeline_options=None):
            self.pipeline_options = pipeline_options

    class _FakeDocumentConverter:
        def __init__(self, format_options=None):
            created_format_options.append(format_options)

    class _FakeInputFormat:
        PDF = "PDF"

    fake_base_models = types.ModuleType("docling.datamodel.base_models")
    fake_base_models.InputFormat = _FakeInputFormat
    fake_pipeline_options_mod = types.ModuleType("docling.datamodel.pipeline_options")
    fake_pipeline_options_mod.PdfPipelineOptions = _FakePdfPipelineOptions
    fake_document_converter_mod = types.ModuleType("docling.document_converter")
    fake_document_converter_mod.DocumentConverter = _FakeDocumentConverter
    fake_document_converter_mod.PdfFormatOption = _FakePdfFormatOption

    injected = {
        "docling": types.ModuleType("docling"),
        "docling.datamodel": types.ModuleType("docling.datamodel"),
        "docling.datamodel.base_models": fake_base_models,
        "docling.datamodel.pipeline_options": fake_pipeline_options_mod,
        "docling.document_converter": fake_document_converter_mod,
    }
    orig_modules = {name: sys.modules.get(name) for name in injected}
    orig_converters = dict(docling_client._CONVERTERS)
    sys.modules.update(injected)
    docling_client._CONVERTERS.clear()
    try:
        docling_client._get_converter(ocr=True, table_structure=False)
    finally:
        for name, mod in orig_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        docling_client._CONVERTERS.clear()
        docling_client._CONVERTERS.update(orig_converters)

    assert created_options == [{
        "do_ocr": True,
        "do_table_structure": False,
        "artifacts_path": docling_client.DOCLING_ARTIFACTS_PATH,
    }], created_options
    assert len(created_format_options) == 1, created_format_options
    print("✅ [7] _get_converter: PdfPipelineOptions/DocumentConverter への配線を検証")


# ---------------------------------------------------------
# [5] docling 不在時のフォールバック順序 (旧 test_paper_radar.py [9] から移設)
# ---------------------------------------------------------

def test_capture_document_falls_back_to_html_then_raises():
    """docling が無ければ HTML、HTML も駄目なら CaptureError。

    「docling が無い環境」が既定なので、この経路が黙って壊れると
    論文機能そのものが無言で死ぬ。
    """
    calls = []

    class _Resp:
        status_code = 200
        encoding = "utf-8"
        text = "<html><article>" + ("本文です。" * 200) + "</article></html>"

        def raise_for_status(self):
            return None

    def _fake_get(url, **kwargs):
        calls.append(url)
        return _Resp()

    orig_available, orig_get = docling_client.is_available, docling_client.requests.get
    docling_client.is_available = lambda: False
    docling_client.requests.get = _fake_get
    try:
        result = docling_client.capture_document(
            "https://arxiv.org/pdf/2607.00060",
            ["https://arxiv.org/html/2607.00060v1"],
            use_cache=False,
        )
        assert result["backend"] == "html", result
        assert calls == ["https://arxiv.org/html/2607.00060v1"], calls
        assert any("docling が未インストール" in w for w in result["warnings"]), result["warnings"]
        assert result["tables"] == [], "HTML経路では tables は空のはず"

        # HTML も無い → 例外 (静かに空文字を返さない)
        raised = False
        try:
            docling_client.capture_document("https://arxiv.org/pdf/x", [], use_cache=False)
        except docling_client.CaptureError:
            raised = True
        assert raised, "全経路失敗なのに CaptureError が飛んでいない"
    finally:
        docling_client.is_available = orig_available
        docling_client.requests.get = orig_get
    print("✅ [5] capture_document: docling→HTML→例外 の順に劣化する")


def test_capture_document_truncates_and_reports():
    """max_chars で切ったことを truncated と warnings で申告すること。"""
    orig_available = docling_client.is_available
    orig_convert = docling_client.convert_with_docling
    docling_client.is_available = lambda: True
    docling_client.convert_with_docling = lambda source, ocr=False, table_structure=True: {
        "text": "x" * 5000, "tables": []}
    try:
        result = docling_client.capture_document("dummy.pdf", [], max_chars=1000, use_cache=False)
    finally:
        docling_client.is_available = orig_available
        docling_client.convert_with_docling = orig_convert
    assert result["truncated"] is True and len(result["text"]) == 1000, result
    assert result["chars"] == 5000, "chars は切る前の全長であること"
    assert any("先頭 1000 文字" in w for w in result["warnings"]), result["warnings"]
    print("✅ [5] capture_document: 切り詰めを申告する")


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
