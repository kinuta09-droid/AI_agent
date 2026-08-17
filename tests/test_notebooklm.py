# -*- coding: utf-8 -*-
"""
NotebookLM ツール — ユニットテスト

方針:
  - ネットワークに一切出ない。tools.tool_notebooklm._get_client / _classify_genre を
    差し替えて NotebookLMClient・Gemini への実アクセスを遮断する (CLAUDE.md: テストを
    一周回すだけで課金や実副作用が起きてはいけない)。
  - notebooklm-mcp-cli 自体の内部実装(RPC形式等)には依存しない。
    _get_client() が返すオブジェクトの公開メソッド呼び出し・戻り値だけをテストする。
  - notebooklm_tools.services.auth.credentials_are_usable もモジュール読み込み時点で
    常に「認証OK」を返すスタブに差し替える (下記参照)。_check_auth() が
    run_notebooklm_research 等の入口で呼ばれるようになったため、差し替えないと
    このテストファイルを実行するだけで実際のGoogleへネットワークアクセスが発生し、
    かつこのマシンの実際の認証状態(切れている場合がある)次第でテストの合否が
    ぶれてしまう。認証切れそのものを検証するテストは、この既定値を個別に
    上書きしてから実行する。

実行: py tests/test_notebooklm.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.lib_shared as lib_shared
import tools.tool_notebooklm as nlm
from lib.bot_errors import is_failure_result

import notebooklm_tools.services.auth as _nlm_auth
_nlm_auth.credentials_are_usable = lambda force=False: (True, "configured", None)


class _FakeGeminiResp:
    def __init__(self, text):
        self.text = text


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
        self.file_source_calls = []
        self.deleted_source_ids = []
        self._sources_by_notebook = {}
        self.research_start_calls = []
        self.studio_create_calls = []
        self.studio_artifacts = {}

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

    def add_file(self, notebook_id, file_path, wait=True, wait_timeout=180.0):
        self.file_source_calls.append(file_path)
        src = {"id": f"src-file-{len(self.file_source_calls)}",
               "title": os.path.basename(file_path), "url": None}
        self._sources_by_notebook.setdefault(notebook_id, []).append(src)
        return src

    def delete_source(self, source_id):
        self.deleted_source_ids.append(source_id)
        for nb_id, srcs in self._sources_by_notebook.items():
            self._sources_by_notebook[nb_id] = [s for s in srcs if s.get("id") != source_id]
        return True

    # --- research_and_import が使う notebooklm_tools.services.research 経由の呼び出し ---

    def start_research(self, notebook_id, query, source, mode):
        self.research_start_calls.append((notebook_id, query, source, mode))
        return {"task_id": "task-1", "notebook_id": notebook_id}

    def poll_research(self, notebook_id, target_task_id=None, target_query=None):
        return {
            "status": "completed",
            "task_id": target_task_id or "task-1",
            "sources": [{"url": "https://found.example/a", "title": "Found A", "result_type": 1}],
            "report": "見つかった内容の要約レポート",
        }

    def import_research_sources(self, notebook_id, task_id, sources, timeout=300.0):
        imported = [{"id": f"src-research-{i}", "title": s.get("title"), "url": s.get("url")}
                    for i, s in enumerate(sources, start=1)]
        self._sources_by_notebook.setdefault(notebook_id, []).extend(imported)
        return imported

    # --- create_studio_artifact/check_studio_status が使う
    # notebooklm_tools.services.studio 経由の呼び出し ---

    def create_audio_overview(self, notebook_id, source_ids, format_code, length_code, language, focus_prompt):
        self.studio_create_calls.append(("audio", notebook_id, source_ids))
        return {"artifact_id": "artifact-1", "status": "in_progress"}

    def poll_studio_status(self, notebook_id):
        return self.studio_artifacts.get(notebook_id, [])

    def list_mind_maps(self, notebook_id):
        return []


def _patch_client(fake_client):
    """tools.tool_notebooklm._get_client を差し替え、finally で元に戻す関数を返す。"""
    original = nlm._get_client
    nlm._get_client = lambda: fake_client
    return original


def _patch_classify(fn):
    """tools.tool_notebooklm._classify_genre を差し替え、finally で元に戻す関数を返す。"""
    original = nlm._classify_genre
    nlm._classify_genre = fn
    return original


def _always_existing(genre):
    """常に『既存ジャンルに強く一致』を返す _classify_genre の差し替え関数を作る。"""
    return lambda content_hint, existing_genres: {
        "existing_match": genre, "match_is_strong": True, "new_genre_suggestion": genre,
    }


def _always_new(genre):
    """常に『既存との一致が弱く、新規ジャンルを提案』を返す _classify_genre の差し替え関数を作る。"""
    return lambda content_hint, existing_genres: {
        "existing_match": "", "match_is_strong": False, "new_genre_suggestion": genre,
    }


def _patch_export_vault(fn):
    """tools.tool_notebooklm._export_vault を差し替え、finally で元に戻す関数を返す。"""
    original = nlm._export_vault
    nlm._export_vault = fn
    return original


def _patch_credentials_are_usable(fn):
    """notebooklm_tools.services.auth.credentials_are_usable を差し替え、finally で元に戻す関数を返す。"""
    import notebooklm_tools.services.auth as nlm_auth
    original = nlm_auth.credentials_are_usable
    nlm_auth.credentials_are_usable = fn
    return original


def _write_files(tmpdir, contents: dict) -> list:
    """{同期キー: 内容} から export_vault() 相当の戻り値 [(path, group, count, chars), ...] を作る。"""
    results = []
    for name, content in contents.items():
        path = os.path.join(tmpdir, f"{name}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        results.append((path, name, 1, len(content)))
    return results


# ---------------------------------------------------------
# _check_auth — 認証切れを早期に・正確に検出する (2026-08-17)
#
# 実測: Cookieの鮮度確認トークンが数時間の未使用で失効し、それを引数の問題と
# 誤解したCriticが tool_web_search へのフォールバックや存在しないファイルへの
# 追記を繰り返した。genre分類やリサーチを試みる前に認証を確認し、失敗時は
# 即座に「nlm loginしてください」と分かりやすく返すことで、無駄な再試行の
# 連鎖を防ぐ。
# ---------------------------------------------------------

def test_check_auth_returns_empty_when_usable():
    import notebooklm_tools.services.auth as nlm_auth
    original = _patch_credentials_are_usable(lambda: (True, "configured", None))
    try:
        assert nlm._check_auth() == ""
    finally:
        nlm_auth.credentials_are_usable = original


def test_check_auth_returns_login_instruction_when_expired():
    import notebooklm_tools.services.auth as nlm_auth
    original = _patch_credentials_are_usable(lambda: (False, "expired", "cookies expired"))
    try:
        result = nlm._check_auth()
        assert result.startswith("Error:"), result
        assert "nlm login" in result, result
        assert "cookies expired" in result, result
    finally:
        nlm_auth.credentials_are_usable = original


def test_check_auth_fails_open_on_exception():
    import notebooklm_tools.services.auth as nlm_auth

    def _boom():
        raise RuntimeError("network down")

    original = _patch_credentials_are_usable(_boom)
    try:
        assert nlm._check_auth() == "", "認証チェック自体の失敗で実処理まで止めてはいけない"
    finally:
        nlm_auth.credentials_are_usable = original


def test_run_notebooklm_research_fails_fast_when_auth_expired():
    """認証切れのとき、ジャンル分類やソース投入を一切試みず即座にエラーを返すこと。"""
    import notebooklm_tools.services.auth as nlm_auth
    fake = _FakeClient()

    def _boom_classify(content_hint, existing_genres):
        raise AssertionError("認証切れのはずなのに分類が呼ばれた")

    oa = _patch_credentials_are_usable(lambda: (False, "expired", "cookies expired"))
    oc, ocl = _patch_classify(_boom_classify), _patch_client(fake)
    try:
        result = nlm.run_notebooklm_research("t", ["https://example.com"], "q")
        assert is_failure_result(result), result
        assert "nlm login" in result, result
        assert fake.create_calls == 0, "認証切れなのにノートブックが作成された"
    finally:
        nlm_auth.credentials_are_usable = oa
        nlm._classify_genre, nlm._get_client = oc, ocl


def test_research_and_import_fails_fast_when_auth_expired():
    import notebooklm_tools.services.auth as nlm_auth
    fake = _FakeClient()
    oa = _patch_credentials_are_usable(lambda: (False, "expired", "cookies expired"))
    ocl = _patch_client(fake)
    try:
        result = nlm.research_and_import("t", "q", genre_override="ジャンルA")
        assert is_failure_result(result), result
        assert "nlm login" in result, result
        assert fake.research_start_calls == [], "認証切れなのにリサーチを開始した"
    finally:
        nlm_auth.credentials_are_usable = oa
        nlm._get_client = ocl


def test_sync_vault_export_fails_fast_when_auth_expired():
    """Vaultのエクスポート処理(ローカルファイルI/O)自体も無駄に走らせないこと。"""
    import notebooklm_tools.services.auth as nlm_auth
    calls = []

    def _track_export():
        calls.append(1)
        return []

    oa = _patch_credentials_are_usable(lambda: (False, "expired", "cookies expired"))
    oe = _patch_export_vault(_track_export)
    try:
        result = nlm.sync_vault_export("ジャンルA")
        assert is_failure_result(result), result
        assert "nlm login" in result, result
        assert calls == [], "認証切れなのにVaultエクスポートが実行された"
    finally:
        nlm_auth.credentials_are_usable = oa
        nlm._export_vault = oe


# ---------------------------------------------------------
# find_or_create_notebook(genre) — 分類は行わない、指定ジャンル名での作成/再利用のみ
# ---------------------------------------------------------

def test_find_or_create_notebook_creates_when_missing():
    fake = _FakeClient()
    original = _patch_client(fake)
    try:
        nb_id = nlm.find_or_create_notebook("新ジャンル")
        assert fake.create_calls == 1, f"create_notebook が呼ばれるべき: calls={fake.create_calls}"
        assert nb_id == fake.notebooks[0].id
    finally:
        nlm._get_client = original


def test_find_or_create_notebook_uses_prefixed_title():
    fake = _FakeClient()
    original = _patch_client(fake)
    try:
        nlm.find_or_create_notebook("ジャンルX")
        assert fake.notebooks[0].title == f"{nlm.NOTEBOOK_TITLE_PREFIX}ジャンルX", fake.notebooks[0].title
    finally:
        nlm._get_client = original


def test_find_or_create_notebook_reuses_existing_prefixed():
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-existing", f"{nlm.NOTEBOOK_TITLE_PREFIX}既存ジャンル"))
    original = _patch_client(fake)
    try:
        nb_id = nlm.find_or_create_notebook("既存ジャンル")
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
    fake.notebooks.append(_FakeNotebook("nb-real-unrelated", "ジャンルX"))
    original = _patch_client(fake)
    try:
        nb_id = nlm.find_or_create_notebook("ジャンルX")
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


# ---------------------------------------------------------
# Vault同期専用ジャンルの分類候補からの除外・直接指定の拒否
#
# 2026-08-17実測: agent_scheduler.py の日次Vault同期ジョブが使う "[ARMS] Vault" が、
# 一般的なジャンル("料理"等)と同じ名前空間を共有しているため、無関係なリサーチ結果
# (「Project AIRIについてリサーチして」等)が繰り返しVaultへ紛れ込んだ。
# ---------------------------------------------------------

def test_existing_genres_excludes_vault_sync_genre():
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-vault", f"{nlm.NOTEBOOK_TITLE_PREFIX}{nlm.VAULT_SYNC_GENRE}"))
    fake.notebooks.append(_FakeNotebook("nb-cooking", f"{nlm.NOTEBOOK_TITLE_PREFIX}料理"))
    original = _patch_client(fake)
    try:
        genres = nlm._existing_genres()
        assert nlm.VAULT_SYNC_GENRE not in genres, f"Vaultが分類候補に混ざっている: {genres}"
        assert "料理" in genres
    finally:
        nlm._get_client = original


def test_resolve_genre_notebook_rejects_vault_via_genre_override():
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-vault", f"{nlm.NOTEBOOK_TITLE_PREFIX}{nlm.VAULT_SYNC_GENRE}"))
    original = _patch_client(fake)
    try:
        genre, notebook_id, confirmation = nlm._resolve_genre_notebook(
            "何かのリサーチ", nlm.VAULT_SYNC_GENRE
        )
        assert genre is None and notebook_id is None, "Vault指定なのに書き込み先が確定してしまった"
        assert confirmation and "Vault" in confirmation and "同期専用" in confirmation, confirmation
    finally:
        nlm._get_client = original


def test_resolve_genre_notebook_rejects_vault_when_classifier_proposes_it():
    """分類器がVaultの存在を知らなくても、偶然同名の新規ジャンルを提案した場合の保険。"""
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-vault", f"{nlm.NOTEBOOK_TITLE_PREFIX}{nlm.VAULT_SYNC_GENRE}"))
    oc, ocl = _patch_classify(_always_new(nlm.VAULT_SYNC_GENRE)), _patch_client(fake)
    try:
        genre, notebook_id, confirmation = nlm._resolve_genre_notebook("何かのリサーチ", "")
        assert genre is None and notebook_id is None
        assert confirmation and "同期専用" in confirmation, confirmation
    finally:
        nlm._classify_genre, nlm._get_client = oc, ocl


def test_resolve_genre_notebook_asks_when_match_is_weak_even_with_sparse_candidates():
    """既存ジャンルが1つしか無くても、一致が弱ければ黙って押し込まず確認を挟むこと。

    2026-08-17実測: 既存ジャンルが「ビジネス」しか無い状態で「最新のローカルLLM」が
    「ビジネス」に分類され、無関係な話題が唯一の既存ジャンルへこじつけで押し込まれた。
    """
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-biz", f"{nlm.NOTEBOOK_TITLE_PREFIX}ビジネス"))

    def _weak_match(content_hint, existing_genres):
        return {"existing_match": "ビジネス", "match_is_strong": False, "new_genre_suggestion": "AI技術"}

    oc, ocl = _patch_classify(_weak_match), _patch_client(fake)
    try:
        genre, notebook_id, confirmation = nlm._resolve_genre_notebook("最新のローカルLLMについて", "")
        assert genre is None and notebook_id is None, "一致が弱いのに書き込み先が確定してしまった"
        assert confirmation and "[要確認]" in confirmation, confirmation
        assert "AI技術" in confirmation and "ビジネス" in confirmation, (
            f"新規案と既存候補の両方が選択肢として出るべき: {confirmation}")
        assert fake.create_calls == 0, "確認前にノートブックが作成された"
    finally:
        nlm._classify_genre, nlm._get_client = oc, ocl


# ---------------------------------------------------------
# add_source
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# run_notebooklm_research — genre_override / 新規ジャンル確認ゲート
# ---------------------------------------------------------

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


def test_run_notebooklm_research_with_genre_override_skips_classification():
    fake = _FakeClient()

    def _boom(content_hint, existing_genres):
        raise AssertionError("genre_override 指定時は _classify_genre を呼んではいけない")

    oc, ocl = _patch_classify(_boom), _patch_client(fake)
    try:
        result = nlm.run_notebooklm_research(
            "t", ["https://example.com/a"], "何が書かれている?", genre_override="料理"
        )
        assert not is_failure_result(result), result
        assert any(nb.title == f"{nlm.NOTEBOOK_TITLE_PREFIX}料理" for nb in fake.notebooks)
    finally:
        nlm._classify_genre, nlm._get_client = oc, ocl


def test_run_notebooklm_research_proceeds_when_classify_confirms_existing_genre():
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-cooking", f"{nlm.NOTEBOOK_TITLE_PREFIX}料理"))
    oc, ocl = _patch_classify(_always_existing("料理")), _patch_client(fake)
    try:
        result = nlm.run_notebooklm_research(
            "パスタのレシピ動画", ["https://example.com/pasta"], "パスタの作り方を教えて"
        )
        assert "answer to: パスタの作り方を教えて" in result, result
        assert not is_failure_result(result), result
        assert fake.create_calls == 0, "既存ジャンルへの分類なのに新規作成された"
    finally:
        nlm._classify_genre, nlm._get_client = oc, ocl


def test_run_notebooklm_research_asks_for_confirmation_when_new_genre_needed():
    """新規ジャンルが必要と判定されたら、何も作成/追加せず確認メッセージだけ返すこと。"""
    fake = _FakeClient()
    fake.notebooks.append(_FakeNotebook("nb-cooking", f"{nlm.NOTEBOOK_TITLE_PREFIX}料理"))
    oc, ocl = _patch_classify(_always_new("宇宙")), _patch_client(fake)
    try:
        result = nlm.run_notebooklm_research(
            "火星探査について", ["https://example.com/mars"], "火星の特徴は?"
        )
        assert not is_failure_result(result), f"確認待ちは失敗扱いにしない: {result}"
        assert "[要確認]" in result, result
        assert "宇宙" in result, result
        assert "料理" in result, "既存ジャンルの選択肢も提示すべき"
        assert fake.create_calls == 0, "確認前にノートブックが作成された"
        assert fake.url_source_calls == [], "確認前にソースが追加された"
    finally:
        nlm._classify_genre, nlm._get_client = oc, ocl


def test_run_notebooklm_research_reports_progress_on_partial_failure():
    class _FailOnSecondUrl(_FakeClient):
        def add_url_source(self, notebook_id, url, wait=True, wait_timeout=180.0):
            if len(self.url_source_calls) == 1:
                return None
            return super().add_url_source(notebook_id, url, wait, wait_timeout)

    fake = _FailOnSecondUrl()
    original = _patch_client(fake)
    try:
        result = nlm.run_notebooklm_research(
            "t", ["https://a.example", "https://b.example"], "q", genre_override="ジャンルA"
        )
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
        result = nlm.run_notebooklm_research("t", [], "質問", genre_override="ジャンルA")
        assert is_failure_result(result), f"失敗ケースなのに成功扱いされた: {result}"
        assert result.startswith("Error:"), result
    finally:
        nlm._get_client = original


# ---------------------------------------------------------
# research_and_import — DeepSearch相当 (自動ソース発見→インポート)
# ---------------------------------------------------------

def test_research_and_import_completes_and_imports():
    fake = _FakeClient()
    oc, ocl = _patch_classify(_always_existing("宇宙")), _patch_client(fake)
    fake.notebooks.append(_FakeNotebook("nb-space", f"{nlm.NOTEBOOK_TITLE_PREFIX}宇宙"))
    try:
        result = nlm.research_and_import("火星の最新探査", "火星探査 最新")
        assert not is_failure_result(result), result
        assert fake.research_start_calls == [("nb-space", "火星探査 最新", "web", "fast")]
        assert "見つかったソース: 1件" in result, result
        assert "新規インポート: 1件" in result, result
        assert "既存と重複でスキップ: 0件" in result, result
        assert "見つかった内容の要約レポート" in result, result
        assert "Found A" in result and "https://found.example/a" in result, (
            f"ソース一覧(タイトル/出典)が出力に含まれていない: {result}")
    finally:
        nlm._classify_genre, nlm._get_client = oc, ocl


def test_research_and_import_lists_sources_when_report_is_empty():
    """mode="fast"はNotebookLM側がreportを生成しない(空文字)ため、報告文がsources一覧だけに
    頼る実際のケース。sources自体は使わずに捨てると、後続のノート作成が中身の無い報告に
    なってしまう(2026-08-17実測: 「Project AIRIについてリサーチして」でreportが空のまま
    件数しか報告されず、後続のtool_create_noteが実在するソースを一切参照できなかった)。"""
    class _FastModeClient(_FakeClient):
        def poll_research(self, notebook_id, target_task_id=None, target_query=None):
            return {
                "status": "completed",
                "task_id": "task-fast",
                "sources": [
                    {"url": "https://a.example/airi", "title": "AIRIの紹介記事",
                     "description": "オープンソースのAI VTuberプラットフォーム"},
                ],
                "report": "",
            }

    fake = _FastModeClient()
    original = _patch_client(fake)
    try:
        result = nlm.research_and_import("AIRI", "AIRI について", genre_override="ジャンルA")
        assert not is_failure_result(result), result
        assert "AIRIの紹介記事" in result, f"report空でもソースのタイトルが出るべき: {result}"
        assert "https://a.example/airi" in result, result
        assert "オープンソースのAI VTuberプラットフォーム" in result, result
    finally:
        nlm._get_client = original


def test_research_and_import_skips_sources_already_in_notebook():
    """既存ノートブックに同じURLのソースが既にある場合、重複インポートしないこと。

    2026-08-17実測: SDKのauto_importは重複排除を行わず、同じ話題で「リサーチして」を
    繰り返すたびに同一記事がノートブックへ何件も溜まっていった(実際に[ARMS] Vaultで
    Project AIRI関連記事が2〜3重に重複していた)。add_source (手動ソース投入) が既に持つ
    URL重複防止と同じ発想を、自動リサーチ側にも適用する。
    """
    class _MultiSourceClient(_FakeClient):
        def poll_research(self, notebook_id, target_task_id=None, target_query=None):
            return {
                "status": "completed",
                "task_id": "task-multi",
                "sources": [
                    {"url": "https://a.example/airi", "title": "既にあるソース"},
                    {"url": "https://b.example/airi", "title": "新しいソース"},
                ],
                "report": "",
            }

    fake = _MultiSourceClient()
    nb = fake.create_notebook(title=f"{nlm.NOTEBOOK_TITLE_PREFIX}ジャンルA")
    fake._sources_by_notebook[nb.id] = [
        {"id": "src-existing", "title": "既にあるソース", "url": "https://a.example/airi"},
    ]
    original = _patch_client(fake)
    try:
        result = nlm.research_and_import("AIRI", "AIRI について", genre_override="ジャンルA")
        assert not is_failure_result(result), result
        assert "見つかったソース: 2件" in result, result
        assert "新規インポート: 1件" in result, result
        assert "既存と重複でスキップ: 1件" in result, result
        assert "新しいソース" in result, result
        # 既存のURLと同じソースは再度importされていないこと
        titles_after = [s.get("title") for s in fake._sources_by_notebook[nb.id]]
        assert titles_after.count("既にあるソース") == 1, (
            f"既存ソースが重複してインポートされた: {titles_after}")
        assert "新しいソース" in titles_after
    finally:
        nlm._get_client = original


def test_research_and_import_reports_found_sources_when_import_fails():
    """リサーチ自体は成功したのに、インポート呼び出しだけ失敗した場合、
    見つかったソース件数を握りつぶさず報告に残すこと (grillで検出)。

    以前は import_research の例外がそのまま関数全体の except に落ち、
    実際には資料が見つかっていたという情報が「Error: ...」の一言に
    上書きされて消えていた。run_notebooklm_research の部分失敗時の
    進捗表示と同じ発想をここにも適用する。
    """
    class _ImportFailsClient(_FakeClient):
        def poll_research(self, notebook_id, target_task_id=None, target_query=None):
            return {
                "status": "completed",
                "task_id": "task-fail",
                "sources": [{"url": "https://a.example/x", "title": "見つかったソース"}],
                "report": "",
            }

        def import_research_sources(self, notebook_id, task_id, sources, timeout=300.0):
            raise RuntimeError("import RPC timeout")

    fake = _ImportFailsClient()
    original = _patch_client(fake)
    try:
        result = nlm.research_and_import("AIRI", "AIRI について", genre_override="ジャンルA")
        assert "見つかったソース: 1件" in result, (
            f"インポート失敗で発見件数まで失われた: {result}")
        assert "インポート失敗" in result and "import RPC timeout" in result, result
        assert "見つかったソース" in result and "新規インポート: 0件" in result, result
    finally:
        nlm._get_client = original


def test_research_and_import_asks_for_confirmation_when_new_genre_needed():
    fake = _FakeClient()
    oc, ocl = _patch_classify(_always_new("宇宙")), _patch_client(fake)
    try:
        result = nlm.research_and_import("火星の最新探査", "火星探査 最新")
        assert "[要確認]" in result, result
        assert fake.research_start_calls == [], "確認前にリサーチを開始してしまった"
    finally:
        nlm._classify_genre, nlm._get_client = oc, ocl


def test_research_and_import_reports_incomplete_status_with_task_id():
    """タイムアウト時にtask_idを捨てないこと(捨てると、再試行が新規タスクを重複起動するだけになる)。"""
    class _StillRunningClient(_FakeClient):
        def poll_research(self, notebook_id, target_task_id=None, target_query=None):
            return {"status": "in_progress", "task_id": target_task_id, "sources": [], "report": ""}

    fake = _StillRunningClient()
    original = _patch_client(fake)
    try:
        result = nlm.research_and_import("t", "q", genre_override="ジャンルA")
        assert not is_failure_result(result), result
        assert "まだ処理中です" in result, result
        assert "task-1" in result, f"再開に必要なtask_idが失われている: {result}"
    finally:
        nlm._get_client = original


# ---------------------------------------------------------
# create_studio_artifact / check_studio_status — Studio成果物生成
# ---------------------------------------------------------

def test_create_studio_artifact_starts_generation():
    fake = _FakeClient()
    nb = fake.create_notebook(title=f"{nlm.NOTEBOOK_TITLE_PREFIX}宇宙")
    fake._sources_by_notebook[nb.id] = [{"id": "src-1", "title": "既存ソース", "url": "https://x.example"}]
    original = _patch_client(fake)
    try:
        result = nlm.create_studio_artifact("宇宙の話", "audio", genre_override="宇宙")
        assert not is_failure_result(result), result
        assert "audio" in result and "artifact-1" in result and "in_progress" in result, result
        assert fake.studio_create_calls == [("audio", nb.id, ["src-1"])]
    finally:
        nlm._get_client = original


def test_create_studio_artifact_requires_sources():
    fake = _FakeClient()
    original = _patch_client(fake)
    try:
        result = nlm.create_studio_artifact("t", "audio", genre_override="ジャンルA")
        assert is_failure_result(result), "ソースが無いノートブックで成功してしまった"
    finally:
        nlm._get_client = original


def test_create_studio_artifact_rejects_invalid_type():
    fake = _FakeClient()
    nb = fake.create_notebook(title=f"{nlm.NOTEBOOK_TITLE_PREFIX}ジャンルA")
    fake._sources_by_notebook[nb.id] = [{"id": "src-1", "title": "s", "url": "https://x.example"}]
    original = _patch_client(fake)
    try:
        result = nlm.create_studio_artifact("t", "not_a_real_type", genre_override="ジャンルA")
        assert is_failure_result(result), result
        assert result.startswith("Error:"), result
    finally:
        nlm._get_client = original


def test_check_studio_status_reports_completed_artifact_with_url():
    fake = _FakeClient()
    nb = fake.create_notebook(title=f"{nlm.NOTEBOOK_TITLE_PREFIX}宇宙")
    fake.studio_artifacts[nb.id] = [
        {"artifact_id": "artifact-1", "type": "audio", "title": "音声概要",
         "status": "completed", "audio_url": "https://audio.example/x"},
    ]
    original = _patch_client(fake)
    try:
        result = nlm.check_studio_status("宇宙の話", genre_override="宇宙")
        assert "音声概要" in result, result
        assert "status=completed" in result, result
        assert "https://audio.example/x" in result, result
    finally:
        nlm._get_client = original


def test_check_studio_status_does_not_create_notebook_when_missing():
    """『確認するだけ』の呼び出しが、未作成のジャンルに対して空のノートブックを新規作成しないこと。"""
    fake = _FakeClient()
    original = _patch_client(fake)
    try:
        result = nlm.check_studio_status("t", genre_override="存在しないジャンル")
        assert is_failure_result(result), result
        assert "見つかりません" in result, result
        assert fake.create_calls == 0, "確認するだけのはずが新規ノートブックを作成した"
    finally:
        nlm._get_client = original


def test_check_studio_status_reports_no_artifacts():
    fake = _FakeClient()
    fake.create_notebook(title=f"{nlm.NOTEBOOK_TITLE_PREFIX}宇宙")
    original = _patch_client(fake)
    try:
        result = nlm.check_studio_status("宇宙の話", genre_override="宇宙")
        assert not is_failure_result(result), result
        assert "生成物がありません" in result, result
    finally:
        nlm._get_client = original


# ---------------------------------------------------------
# sync_vault_export — Vault再エクスポート→差分アップロード
# ---------------------------------------------------------

def test_sync_vault_export_reports_export_failure():
    def _boom():
        raise RuntimeError("disk full")

    original = _patch_export_vault(_boom)
    try:
        result = nlm.sync_vault_export("お試し")
        assert is_failure_result(result), result
    finally:
        nlm._export_vault = original


def test_sync_vault_export_first_run_adds_all_files():
    fake = _FakeClient()
    ocl = _patch_client(fake)
    try:
        oc = _patch_export_vault(lambda: _write_files(tempfile.mkdtemp(), {"01_Daily": "content A", "30_Main": "content B"}))
        try:
            result = nlm.sync_vault_export("お試し")
            assert len(fake.file_source_calls) == 2, fake.file_source_calls
            assert "追加: 2件" in result, result
            assert "更新: 0件" in result, result
            assert "変更無しでスキップ: 0件" in result, result
        finally:
            nlm._export_vault = oc
    finally:
        nlm._get_client = ocl


def test_sync_vault_export_second_run_skips_unchanged_files():
    fake = _FakeClient()
    ocl = _patch_client(fake)
    try:
        contents = {"01_Daily": "content A", "30_Main": "content B"}
        oc = _patch_export_vault(lambda: _write_files(tempfile.mkdtemp(), contents))
        try:
            nlm.sync_vault_export("お試し")
            r2 = nlm.sync_vault_export("お試し")
            assert "変更無しでスキップ: 2件" in r2, r2
            assert len(fake.file_source_calls) == 2, "2回目でアップロードが再実行された"
            assert fake.deleted_source_ids == [], "変更が無いのに削除された"
        finally:
            nlm._export_vault = oc
    finally:
        nlm._get_client = ocl


def test_sync_vault_export_replaces_only_changed_files():
    fake = _FakeClient()
    ocl = _patch_client(fake)
    try:
        state = {"01_Daily": "content A", "30_Main": "content B"}
        oc = _patch_export_vault(lambda: _write_files(tempfile.mkdtemp(), state))
        try:
            nlm.sync_vault_export("お試し")
            nb_id = fake.notebooks[0].id
            before = {s["title"].split("__")[0]: s["id"] for s in fake._sources_by_notebook[nb_id]}

            state["01_Daily"] = "content A CHANGED"
            r2 = nlm.sync_vault_export("お試し")

            assert "更新: 1件" in r2, r2
            assert "変更無しでスキップ: 1件" in r2, r2
            assert fake.deleted_source_ids == [before["01_Daily"]], fake.deleted_source_ids

            after = {s["title"].split("__")[0]: s["id"] for s in fake._sources_by_notebook[nb_id]}
            assert after["30_Main"] == before["30_Main"], "変更していないファイルのソースIDが変わった"
            assert after["01_Daily"] != before["01_Daily"], "変更したファイルのソースIDが変わっていない"
        finally:
            nlm._export_vault = oc
    finally:
        nlm._get_client = ocl


def test_sync_vault_export_upload_failure_keeps_existing_source():
    class _FailingFileClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.fail_next = False

        def add_file(self, notebook_id, file_path, wait=True, wait_timeout=180.0):
            if self.fail_next:
                return None
            return super().add_file(notebook_id, file_path, wait, wait_timeout)

    fake = _FailingFileClient()
    ocl = _patch_client(fake)
    try:
        state = {"01_Daily": "content A"}
        oc = _patch_export_vault(lambda: _write_files(tempfile.mkdtemp(), state))
        try:
            nlm.sync_vault_export("お試し")
            nb_id = fake.notebooks[0].id
            prior_id = fake._sources_by_notebook[nb_id][0]["id"]

            state["01_Daily"] = "content A CHANGED"
            fake.fail_next = True
            r2 = nlm.sync_vault_export("お試し")

            assert "失敗" in r2, r2
            assert fake.deleted_source_ids == [], "アップロード失敗時に既存ソースを削除してしまった"
            assert fake._sources_by_notebook[nb_id][0]["id"] == prior_id, "既存ソースが無傷で残っていない"
        finally:
            nlm._export_vault = oc
    finally:
        nlm._get_client = ocl


def test_sync_vault_export_removes_temp_export_dir():
    fake = _FakeClient()
    ocl = _patch_client(fake)
    try:
        tmp_holder = {}

        def _make_export():
            tmp = tempfile.mkdtemp()
            tmp_holder["dir"] = tmp
            return _write_files(tmp, {"01_Daily": "content A"})

        oc = _patch_export_vault(_make_export)
        try:
            nlm.sync_vault_export("お試し")
            assert not os.path.isdir(tmp_holder["dir"]), "一時エクスポートディレクトリが残っている"
        finally:
            nlm._export_vault = oc
    finally:
        nlm._get_client = ocl


# ---------------------------------------------------------
# _classify_genre / _sanitize_genre_name そのもののテスト (Geminiをモック)
# ---------------------------------------------------------

def _patch_generate_content(fn):
    original = lib_shared.client.models.generate_content
    lib_shared.client.models.generate_content = fn
    return original


def test_classify_genre_reuses_existing_when_match_is_strong():
    original = _patch_generate_content(
        lambda model, contents: _FakeGeminiResp(
            '{"existing_match": "料理", "match_is_strong": true, "new_genre_suggestion": "料理"}'
        )
    )
    try:
        decision = nlm._classify_genre("パスタのレシピ動画", ["料理", "旅行"])
        assert decision == {
            "existing_match": "料理", "match_is_strong": True, "new_genre_suggestion": "料理",
        }, decision
    finally:
        lib_shared.client.models.generate_content = original


def test_classify_genre_uses_new_genre_when_match_is_weak():
    original = _patch_generate_content(
        lambda model, contents: _FakeGeminiResp(
            '{"existing_match": "", "match_is_strong": false, "new_genre_suggestion": "ガジェット"}'
        )
    )
    try:
        decision = nlm._classify_genre("新しいスマホの比較", ["料理"])
        assert decision == {
            "existing_match": "", "match_is_strong": False, "new_genre_suggestion": "ガジェット",
        }, decision
    finally:
        lib_shared.client.models.generate_content = original


def test_classify_genre_does_not_force_weak_match_into_sparse_existing_genres():
    """既存ジャンルが1つしかない状況で、無関係な話題までこじつけで押し込まれないこと。

    2026-08-17実測: 既存ジャンルが「ビジネス」1つしかない状態で「最新のローカルLLM」が
    ビジネスに分類された。分類プロンプトの「広く括るバイアス」が、候補が薄い時ほど
    無関係な話題まで唯一の既存ジャンルへ押し込む方向に働いていたための回帰テスト。
    """
    original = _patch_generate_content(
        lambda model, contents: _FakeGeminiResp(
            '{"existing_match": "ビジネス", "match_is_strong": false, '
            '"new_genre_suggestion": "AI技術"}'
        )
    )
    try:
        decision = nlm._classify_genre("最新のローカルLLMについて", ["ビジネス"])
        assert decision["match_is_strong"] is False, decision
        assert decision["existing_match"] == "ビジネス", (
            "参考情報としては既存候補を残してよいが、match_is_strong=Falseのまま届くこと")
        assert decision["new_genre_suggestion"] == "AI技術"
    finally:
        lib_shared.client.models.generate_content = original


def test_classify_genre_hallucination_guard_resets_match_is_strong():
    """既存一覧に無いジャンル名を existing_match として返してきた場合(幻覚)の安全策。"""
    original = _patch_generate_content(
        lambda model, contents: _FakeGeminiResp(
            '{"existing_match": "存在しないジャンル", "match_is_strong": true, '
            '"new_genre_suggestion": "存在しないジャンル"}'
        )
    )
    try:
        decision = nlm._classify_genre("何かの内容", ["料理", "旅行"])
        assert decision["existing_match"] == "", decision
        assert decision["match_is_strong"] is False, decision
    finally:
        lib_shared.client.models.generate_content = original


def test_classify_genre_falls_back_to_sanitized_hint_on_exception():
    def _boom(model, contents):
        raise RuntimeError("api down")

    original = _patch_generate_content(_boom)
    try:
        decision = nlm._classify_genre("  パスタの  \n レシピ  ", [])
        assert decision == {
            "existing_match": "", "match_is_strong": False,
            "new_genre_suggestion": "パスタの レシピ",
        }, decision
    finally:
        lib_shared.client.models.generate_content = original


def test_classify_genre_falls_back_to_sanitized_hint_on_malformed_json():
    original = _patch_generate_content(
        lambda model, contents: _FakeGeminiResp("これはJSONではない")
    )
    try:
        decision = nlm._classify_genre("何かの内容", [])
        assert decision == {
            "existing_match": "", "match_is_strong": False, "new_genre_suggestion": "何かの内容",
        }, decision
    finally:
        lib_shared.client.models.generate_content = original


def test_sanitize_genre_name_collapses_whitespace_and_truncates():
    assert nlm._sanitize_genre_name("あ" * 50) == "あ" * 30
    assert nlm._sanitize_genre_name("  a \n b   c ") == "a b c"
    assert nlm._sanitize_genre_name("") == "未分類"
    assert nlm._sanitize_genre_name(None) == "未分類"


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
