# -*- coding: utf-8 -*-
"""
NotebookLM ツール — ユニットテスト

方針:
  - ネットワークに一切出ない。tools.tool_notebooklm._get_client を差し替えて
    NotebookLMClient への実アクセスを遮断する (CLAUDE.md: テストを一周回すだけで
    課金や実副作用が起きてはいけない)。
  - notebooklm-mcp-cli 自体の内部実装(RPC形式等)には依存しない。
    _get_client() が返すオブジェクトの公開メソッド呼び出し・戻り値だけをテストする。

実行: py tests/test_notebooklm.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_notebooklm as nlm
from lib.bot_errors import is_failure_result


class _FakeNotebook:
    def __init__(self, id, title):
        self.id = id
        self.title = title


class _FakeClient:
    """notebooklm_tools.core.client.NotebookLMClient のテスト用差し替え。

    既存ソース一覧 (get_notebook_sources_with_types) も notebook_id ごとに
    保持し、add_source の重複排除ロジックが実際に参照する経路を再現する。
    """

    def __init__(self):
        self.notebooks = []
        self.create_calls = 0
        self.url_source_calls = []
        self.text_source_calls = []
        self._sources_by_notebook = {}

    def list_notebooks(self):
        return self.notebooks

    def create_notebook(self, title=""):
        self.create_calls += 1
        nb = _FakeNotebook(f"nb-{len(self.notebooks) + 1}", title)
        self.notebooks.append(nb)
        self._sources_by_notebook.setdefault(nb.id, [])
        return nb

    def get_notebook_sources_with_types(self, notebook_id):
        return self._sources_by_notebook.get(notebook_id, [])

    def add_url_source(self, notebook_id, url, wait=True, wait_timeout=180.0):
        self.url_source_calls.append(url)
        src = {"id": f"src-{len(self.url_source_calls) + len(self.text_source_calls)}",
               "title": url, "url": url}
        self._sources_by_notebook.setdefault(notebook_id, []).append(src)
        return src

    def add_text_source(self, notebook_id, text, title="", wait=True, wait_timeout=180.0):
        self.text_source_calls.append(text)
        src = {"id": f"src-{len(self.url_source_calls) + len(self.text_source_calls)}",
               "title": title, "url": None}
        self._sources_by_notebook.setdefault(notebook_id, []).append(src)
        return src

    def query(self, notebook_id, query_text, source_ids=None, timeout=120.0):
        return {
            "answer": f"answer to: {query_text}",
            "references": [{"cited_text": "根拠テキスト"}],
        }


def _patch_client(fake_client):
    """tools.tool_notebooklm._get_client を差し替え、finally で元に戻す関数を返す。"""
    original = nlm._get_client
    nlm._get_client = lambda: fake_client
    return original


def test_find_or_create_notebook_creates_when_missing():
    fake = _FakeClient()
    original = _patch_client(fake)
    try:
        nb_id = nlm.find_or_create_notebook("新トピック")
        assert fake.create_calls == 1, f"create_notebook が呼ばれるべき: calls={fake.create_calls}"
        assert nb_id == fake.notebooks[0].id
    finally:
        nlm._get_client = original


def test_find_or_create_notebook_uses_prefixed_title():
    fake = _FakeClient()
    original = _patch_client(fake)
    try:
        nlm.find_or_create_notebook("トピックX")
        assert fake.notebooks[0].title == f"{nlm.NOTEBOOK_TITLE_PREFIX}トピックX", fake.notebooks[0].title
    finally:
        nlm._get_client = original


def test_find_or_create_notebook_reuses_existing_prefixed():
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-existing", f"{nlm.NOTEBOOK_TITLE_PREFIX}既存トピック"))
    original = _patch_client(fake)
    try:
        nb_id = nlm.find_or_create_notebook("既存トピック")
        assert fake.create_calls == 0, "既存ノートブックがあるのに新規作成された"
        assert nb_id == "nb-existing"
    finally:
        nlm._get_client = original


def test_find_or_create_notebook_does_not_match_unprefixed_existing_title():
    """ユーザーの既存ノートブック(このツールと無関係・プレフィックス無し)と衝突しないこと。

    /grillで検出: topic がLLM由来の自由文字列のため、プレフィックス無しだと
    ユーザーの実データを持つ既存ノートブックへ誤って書き込みうる事故だった。
    """
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-real-unrelated", "トピックX"))
    original = _patch_client(fake)
    try:
        nb_id = nlm.find_or_create_notebook("トピックX")
        assert nb_id != "nb-real-unrelated", "既存の無関係なノートブックへ誤って書き込もうとした"
        assert fake.create_calls == 1
    finally:
        nlm._get_client = original


def test_find_or_create_notebook_raises_when_create_fails():
    class _NoCreateClient(_FakeClient):
        def create_notebook(self, title=""):
            return None

    fake = _NoCreateClient()
    original = _patch_client(fake)
    try:
        raised = False
        try:
            nlm.find_or_create_notebook("t")
        except RuntimeError:
            raised = True
        assert raised, "create_notebook が None を返したら RuntimeError になるべき"
    finally:
        nlm._get_client = original


def test_add_source_routes_url_and_text_separately():
    fake = _FakeClient()
    nb_id = fake.create_notebook(title="t").id
    original = _patch_client(fake)
    try:
        nlm.add_source(nb_id, "https://example.com/a")
        nlm.add_source(nb_id, "生テキストソース")
        assert fake.url_source_calls == ["https://example.com/a"]
        assert fake.text_source_calls == ["生テキストソース"]
    finally:
        nlm._get_client = original


def test_add_source_dedupes_same_url():
    fake = _FakeClient()
    nb_id = fake.create_notebook(title="t").id
    original = _patch_client(fake)
    try:
        id1 = nlm.add_source(nb_id, "https://example.com/a")
        id2 = nlm.add_source(nb_id, "https://example.com/a")
        assert len(fake.url_source_calls) == 1, f"同じURLが2回追加された: {fake.url_source_calls}"
        assert id1 == id2
    finally:
        nlm._get_client = original


def test_add_source_dedupes_url_with_trailing_slash_difference():
    fake = _FakeClient()
    nb_id = fake.create_notebook(title="t").id
    original = _patch_client(fake)
    try:
        nlm.add_source(nb_id, "https://example.com/a")
        nlm.add_source(nb_id, "https://example.com/a/")
        assert len(fake.url_source_calls) == 1, "末尾スラッシュ違いが別ソース扱いされた"
    finally:
        nlm._get_client = original


def test_add_source_dedupes_same_text():
    fake = _FakeClient()
    nb_id = fake.create_notebook(title="t").id
    original = _patch_client(fake)
    try:
        nlm.add_source(nb_id, "同じテキスト")
        nlm.add_source(nb_id, "同じテキスト")
        assert len(fake.text_source_calls) == 1, "同じテキストが2回追加された"
    finally:
        nlm._get_client = original


def test_add_source_does_not_dedupe_different_text():
    fake = _FakeClient()
    nb_id = fake.create_notebook(title="t").id
    original = _patch_client(fake)
    try:
        nlm.add_source(nb_id, "テキストA")
        nlm.add_source(nb_id, "テキストB")
        assert len(fake.text_source_calls) == 2, "異なるテキストが誤って重複除去された"
    finally:
        nlm._get_client = original


def test_add_source_failure_raises():
    class _EmptyClient(_FakeClient):
        def add_url_source(self, notebook_id, url, wait=True, wait_timeout=180.0):
            return None

    fake = _EmptyClient()
    original = _patch_client(fake)
    try:
        nb_id = nlm.find_or_create_notebook("t")
        raised = False
        try:
            nlm.add_source(nb_id, "https://example.com")
        except RuntimeError:
            raised = True
        assert raised, "ソース追加失敗時に RuntimeError が発生するべき"
    finally:
        nlm._get_client = original


def test_run_notebooklm_research_returns_answer_with_references():
    fake = _FakeClient()
    original = _patch_client(fake)
    try:
        result = nlm.run_notebooklm_research(
            "テストトピック", ["https://example.com/a", "生テキストソース"], "何が書かれている?"
        )
        assert "answer to: 何が書かれている?" in result, result
        assert "出典:" in result, result
        assert "根拠テキスト" in result, result
        assert not is_failure_result(result), f"成功ケースなのに失敗判定された: {result}"
        assert fake.url_source_calls == ["https://example.com/a"]
        assert fake.text_source_calls == ["生テキストソース"]
    finally:
        nlm._get_client = original


def test_run_notebooklm_research_rejects_too_many_sources():
    fake = _FakeClient()
    original = _patch_client(fake)
    try:
        sources = [f"https://example.com/{i}" for i in range(nlm.MAX_SOURCES + 1)]
        result = nlm.run_notebooklm_research("t", sources, "q")
        assert is_failure_result(result), result
        assert fake.create_calls == 0, "上限超過なのにノートブックが作られた(外部への書き込みが発生した)"
    finally:
        nlm._get_client = original


def test_run_notebooklm_research_reports_progress_on_partial_failure():
    class _FailOnSecondUrl(_FakeClient):
        def add_url_source(self, notebook_id, url, wait=True, wait_timeout=180.0):
            if len(self.url_source_calls) == 1:
                return None
            return super().add_url_source(notebook_id, url, wait, wait_timeout)

    fake = _FailOnSecondUrl()
    original = _patch_client(fake)
    try:
        result = nlm.run_notebooklm_research("t", ["https://a.example", "https://b.example"], "q")
        assert is_failure_result(result), result
        assert "1/2" in result, f"何件成功したかが分かる進捗表示が無い: {result}"
    finally:
        nlm._get_client = original


def test_run_notebooklm_research_error_result_is_detected_as_failure():
    class _BrokenClient(_FakeClient):
        def query(self, notebook_id, query_text, source_ids=None, timeout=120.0):
            return None

    fake = _BrokenClient()
    original = _patch_client(fake)
    try:
        result = nlm.run_notebooklm_research("t", [], "質問")
        assert is_failure_result(result), f"失敗ケースなのに成功扱いされた: {result}"
        assert result.startswith("Error:"), result
    finally:
        nlm._get_client = original


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
