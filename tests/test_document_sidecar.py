# -*- coding: utf-8 -*-
"""
tools/tool_document_sidecar.py (Phase 3-0: サイドカー変換) のユニットテスト

方針:
  - docling / pypdf は要らない。is_available / capture_document / check_document_caps を
    スタブに差し替える (tests/test_document_tool.py, tests/test_docling_client.py と同じ作法)。
  - 本番 Vault (VAULT_ROOT) には触らない。sync_document_sidecars(vault_root=...) の
    引数注入を使い、一時ディレクトリだけを操作する。

実行: py tests/test_document_sidecar.py
"""
import os
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_document_sidecar as doc_sidecar
from lib.bot_errors import is_failure_result
from lib.docling_client import CaptureError


def _touch(path: str, mtime: float = None, content: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# ---------------------------------------------------------
# [1] _walk_source_documents
# ---------------------------------------------------------

def test_walk_source_documents_finds_pdf_docx_pptx_and_ignores_others():
    with tempfile.TemporaryDirectory() as tmp:
        _touch(os.path.join(tmp, "a.pdf"))
        _touch(os.path.join(tmp, "b.DOCX"))
        _touch(os.path.join(tmp, "c.pptx"))
        _touch(os.path.join(tmp, "d.txt"))
        _touch(os.path.join(tmp, "e.md"))
        found = {rel for _abs, rel in doc_sidecar._walk_source_documents(tmp)}
    assert found == {"a.pdf", "b.DOCX", "c.pptx"}, found
    print("✅ [1] pdf/docx/pptx (大小文字無視) を検出し、それ以外は無視する")


def test_walk_source_documents_excludes_generated_and_template_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        _touch(os.path.join(tmp, "Templates", "tpl.pdf"))
        _touch(os.path.join(tmp, ".git", "x.pdf"))
        _touch(os.path.join(tmp, "99_Assets", "Converted", "already.pdf"))
        _touch(os.path.join(tmp, "keep", "y.pdf"))
        found = {rel for _abs, rel in doc_sidecar._walk_source_documents(tmp)}
    assert found == {os.path.join("keep", "y.pdf").replace("\\", "/")}, found
    print("✅ [1] Templates/.git/Converted 配下を走査から除外する")


# ---------------------------------------------------------
# [2] _sidecar_path / _needs_conversion
# ---------------------------------------------------------

def test_sidecar_path_preserves_directory_structure():
    with tempfile.TemporaryDirectory() as tmp:
        path = doc_sidecar._sidecar_path(tmp, "30_Main/foo.pdf")
    expected = os.path.join(tmp, "99_Assets", "Converted", "30_Main", "foo.md")
    assert path == expected, (path, expected)
    print("✅ [2] サイドカーパスがディレクトリ構造を保って組み立てられる")


def test_needs_conversion_true_when_sidecar_missing():
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "a.pdf")
        _touch(source)
        sidecar = os.path.join(tmp, "out", "a.md")
        assert doc_sidecar._needs_conversion(source, sidecar) is True
    print("✅ [2] サイドカー無し -> 変換が要る")


def test_needs_conversion_false_when_sidecar_newer_than_source():
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "a.pdf")
        sidecar = os.path.join(tmp, "a.md")
        _touch(source, mtime=1000.0)
        _touch(sidecar, mtime=2000.0)
        assert doc_sidecar._needs_conversion(source, sidecar) is False
    print("✅ [2] サイドカーの方が新しい -> スキップしてよい")


def test_needs_conversion_true_when_source_newer_than_sidecar():
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "a.pdf")
        sidecar = os.path.join(tmp, "a.md")
        _touch(sidecar, mtime=1000.0)
        _touch(source, mtime=2000.0)
        assert doc_sidecar._needs_conversion(source, sidecar) is True
    print("✅ [2] 元ファイルの方が新しい -> 再変換が要る")


# ---------------------------------------------------------
# [3] _render_sidecar_note
# ---------------------------------------------------------

def test_render_sidecar_note_includes_frontmatter_and_wikilink():
    note = doc_sidecar._render_sidecar_note(
        "30_Main/foo.pdf", {"text": "本文です", "chars": 4, "warnings": []})
    assert "source: docling" in note, note
    assert "[[30_Main/foo.pdf]]" in note, note
    assert "本文です" in note, note
    print("✅ [3] サイドカーノートにフロントマターと元ファイルへのwikilinkが入る")


def test_render_sidecar_note_includes_warnings_when_present():
    note = doc_sidecar._render_sidecar_note(
        "30_Main/foo.pdf",
        {"text": "本文", "chars": 2, "warnings": ["docling 変換に失敗したため HTML から抽出: x"]})
    assert "HTML から抽出" in note, note
    print("✅ [3] warnings があればノートに反映される")


# ---------------------------------------------------------
# [4] sync_document_sidecars
# ---------------------------------------------------------

def _patch(monkeys: dict) -> dict:
    orig = {name: getattr(doc_sidecar, name) for name in monkeys}
    for name, value in monkeys.items():
        setattr(doc_sidecar, name, value)
    return orig


def _restore(orig: dict) -> None:
    for name, value in orig.items():
        setattr(doc_sidecar, name, value)


def test_sync_document_sidecars_skips_when_docling_unavailable():
    orig = _patch({"is_available": lambda: False})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "a.pdf"))
            result = doc_sidecar.sync_document_sidecars(vault_root=tmp)
    finally:
        _restore(orig)
    assert not is_failure_result(result), result
    assert "スキップ" in result, result
    print("✅ [4] docling 未導入なら例外を投げず0件処理で正常終了する")


def test_sync_document_sidecars_converts_and_writes_sidecar():
    orig = _patch({
        "is_available": lambda: True,
        "capture_document": lambda path: {"text": "変換結果テキスト", "chars": 8, "warnings": []},
        "check_document_caps": lambda path: "",
    })
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "30_Main", "foo.pdf"))
            result = doc_sidecar.sync_document_sidecars(vault_root=tmp)
            sidecar = os.path.join(tmp, "99_Assets", "Converted", "30_Main", "foo.md")
            assert os.path.exists(sidecar), sidecar
            with open(sidecar, encoding="utf-8") as f:
                content = f.read()
    finally:
        _restore(orig)
    assert not is_failure_result(result), result
    assert "1 変換" in result, result
    assert "変換結果テキスト" in content, content
    print("✅ [4] 新規PDFを変換しサイドカーMarkdownを書き出す")


def test_sync_document_sidecars_skips_unchanged_on_second_run():
    calls = []
    orig = _patch({
        "is_available": lambda: True,
        "capture_document": lambda path: (calls.append(path), {"text": "本文", "chars": 2, "warnings": []})[1],
        "check_document_caps": lambda path: "",
    })
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "30_Main", "foo.pdf"))
            first = doc_sidecar.sync_document_sidecars(vault_root=tmp)
            second = doc_sidecar.sync_document_sidecars(vault_root=tmp)
    finally:
        _restore(orig)
    assert "1 変換" in first, first
    assert "0 変換" in second and "1 未変更" in second, second
    assert len(calls) == 1, calls
    print("✅ [4] 未変更のファイルは2回目で再変換されない (capture_documentが1回しか呼ばれない)")


def test_sync_document_sidecars_reconverts_when_source_updated():
    orig = _patch({
        "is_available": lambda: True,
        "capture_document": lambda path: {"text": "本文", "chars": 2, "warnings": []},
        "check_document_caps": lambda path: "",
    })
    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "30_Main", "foo.pdf")
            _touch(source)
            doc_sidecar.sync_document_sidecars(vault_root=tmp)
            time.sleep(0.05)
            os.utime(source, (time.time() + 10, time.time() + 10))
            second = doc_sidecar.sync_document_sidecars(vault_root=tmp)
    finally:
        _restore(orig)
    assert "1 変換" in second, second
    print("✅ [4] 元ファイルが更新されたら再変換される")


def test_sync_document_sidecars_capped_file_is_not_converted():
    orig = _patch({
        "is_available": lambda: True,
        "capture_document": lambda path: {"text": "本文", "chars": 2, "warnings": []},
        "check_document_caps": lambda path: "Error: ファイルサイズが上限を超えています",
    })
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "30_Main", "huge.pdf"))
            result = doc_sidecar.sync_document_sidecars(vault_root=tmp)
            sidecar = os.path.join(tmp, "99_Assets", "Converted", "30_Main", "huge.md")
            assert not os.path.exists(sidecar), "上限超過ファイルが変換されてしまった"
    finally:
        _restore(orig)
    assert "1 上限超過" in result, result
    print("✅ [4] サイズ/ページ数上限を超えるファイルは変換せずスキップする")


def test_sync_document_sidecars_one_failure_does_not_block_others():
    def _capture(path):
        if "bad" in path:
            raise CaptureError("変換失敗")
        return {"text": "本文", "chars": 2, "warnings": []}

    orig = _patch({
        "is_available": lambda: True,
        "capture_document": _capture,
        "check_document_caps": lambda path: "",
    })
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "bad.pdf"))
            _touch(os.path.join(tmp, "good.pdf"))
            result = doc_sidecar.sync_document_sidecars(vault_root=tmp)
            good_sidecar = os.path.join(tmp, "99_Assets", "Converted", "good.md")
            assert os.path.exists(good_sidecar), "失敗した1件に巻き込まれて残りが処理されていない"
    finally:
        _restore(orig)
    assert "1 変換" in result and "1 失敗" in result, result
    print("✅ [4] 1件の変換失敗が他ファイルの処理を止めない")


def test_sync_document_sidecars_empty_result_counts_as_failure():
    orig = _patch({
        "is_available": lambda: True,
        "capture_document": lambda path: {"text": "   ", "chars": 0, "warnings": []},
        "check_document_caps": lambda path: "",
    })
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _touch(os.path.join(tmp, "empty.pdf"))
            result = doc_sidecar.sync_document_sidecars(vault_root=tmp)
            sidecar = os.path.join(tmp, "99_Assets", "Converted", "empty.md")
            assert not os.path.exists(sidecar)
    finally:
        _restore(orig)
    assert "1 失敗" in result, result
    print("✅ [4] 変換結果が空文字なら失敗としてカウントしファイルを書かない")


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
