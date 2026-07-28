# -*- coding: utf-8 -*-
"""
リランクのバックエンド切替 (Gemini / ローカル Ollama) のユニットテスト
(requests.post を差し替え — ネットワーク・Ollama 実体なし)

このファイルが守るもの:
  ローカルリランクは「壊れても検索は返る」設計なので、壊れたことに気付けない。
  特に危ないのは以下で、それぞれ専用のケースを置いてある。
    - Ollama が落ちている / モデル名が違うときに、理由の分かる形で劣化するか (4・5)
    - Qwen3 の <think> ブロックが JSON 抽出を汚さないか (2)
      — <think> の中には「[1] が良さそう」のような角括弧が普通に出るので、
        除去し損ねると思考の断片を本命の配列として拾う
    - 検索そのものを道連れにしないか (6)
    - 既定バックエンドが実測で決めた値から黙って動いていないか (8)

実行: py tests/test_rerank_backend.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests

from tools import tool_vault_hybrid as hybrid
from tools.tool_vault_hybrid import (
    _call_ollama_llm, _resolve_rerank_llm, _llm_rerank, _hybrid_hits,
    _call_light_llm, RERANK_BACKEND_DEFAULT,
)

_ENV_KEYS = ("VAULT_SEARCH_RERANK", "VAULT_SEARCH_RERANK_BACKEND",
             "VAULT_SEARCH_RERANK_OLLAMA_MODEL", "VAULT_SEARCH_RERANK_CANDIDATES",
             "VAULT_SEARCH_RERANK_SNIPPET_CHARS")


def _save_env():
    return {k: os.environ.get(k) for k in _ENV_KEYS}


def _restore_env(saved):
    """os.environ に None は代入できない。未設定だったものは削除して戻す。"""
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _cand(rel_path, rrf):
    return {"rel_path": rel_path, "filename": os.path.basename(rel_path),
            "rrf": rrf, "sources": ["semantic"], "query_ids": [1],
            "heading": None, "chunk_id": f"{rel_path}#0",
            "snippet": f"body of {rel_path}", "rerank_pos": None}


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Server Error")

    def json(self):
        return self._payload


class _PostSpy:
    """requests.post を差し替えて、送られたリクエストを記録する。"""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.response


def _patched_post(spy):
    """requests.post を差し替えるコンテキスト。

    tool_vault_hybrid は関数内で `import requests` するため、
    requests モジュールの属性を差し替えれば届く。
    """
    class _Ctx:
        def __enter__(self_inner):
            self_inner.saved = requests.post
            requests.post = spy
            return spy

        def __exit__(self_inner, *exc):
            requests.post = self_inner.saved
            return False
    return _Ctx()


# ---------------------------------------------------------
# 1. バックエンド選択の優先順位
# ---------------------------------------------------------

def test_backend_resolution_order():
    saved = _save_env()
    try:
        os.environ.pop("VAULT_SEARCH_RERANK_BACKEND", None)
        fn, name = _resolve_rerank_llm()
        assert name == RERANK_BACKEND_DEFAULT, name
        assert fn is (_call_light_llm if name == "gemini" else _call_ollama_llm)

        os.environ["VAULT_SEARCH_RERANK_BACKEND"] = "ollama"
        fn, name = _resolve_rerank_llm()
        assert (fn, name) == (_call_ollama_llm, "ollama")

        # 明示引数が env より強い (eval の A/B が環境に左右されない)
        fn, name = _resolve_rerank_llm("gemini")
        assert (fn, name) == (_call_light_llm, "gemini")

        # 未知の名前は既定へ倒す (打ち間違いで黙って別モデルを使わない)
        os.environ["VAULT_SEARCH_RERANK_BACKEND"] = "gpt5-turbo-max"
        fn, name = _resolve_rerank_llm()
        assert (fn, name) == (_call_light_llm, "gemini"), name

        # 大文字・空白は正規化する
        fn, name = _resolve_rerank_llm("  OLLAMA  ")
        assert name == "ollama"
    finally:
        _restore_env(saved)
    print("✅ backend: 明示引数 > env > 既定、未知名は gemini へフォールバック")


# ---------------------------------------------------------
# 2〜3. Ollama 呼び出しの整形
# ---------------------------------------------------------

def test_think_block_is_stripped():
    """Qwen3 の思考ブロックを落とす。

    <think> の中には角括弧が普通に出るので、除去し損ねると
    _parse_json_array が思考の断片を本命の配列として拾う。
    """
    spy = _PostSpy(_FakeResponse({
        "response": "<think>候補[5]も良さそうだが…[9] は無関係</think>\n[3, 1]"}))
    with _patched_post(spy):
        out = _call_ollama_llm("q", model="qwen3:1.7b")
    assert out == "[3, 1]", repr(out)
    assert hybrid._parse_json_array(out) == [3, 1]

    # 特殊トークン・endoftext 以降のゴミも落とす
    spy2 = _PostSpy(_FakeResponse({"response": "[2]<|endoftext|>ゴミ"}))
    with _patched_post(spy2):
        assert _call_ollama_llm("q") == "[2]"
    print("✅ ollama: <think> / 特殊トークン / endoftext 以降を除去")


def test_no_think_prefix_and_model_selection():
    spy = _PostSpy(_FakeResponse({"response": "[1]"}))
    saved = _save_env()
    try:
        os.environ.pop("VAULT_SEARCH_RERANK_OLLAMA_MODEL", None)
        with _patched_post(spy):
            _call_ollama_llm("本文プロンプト")
        body = spy.calls[0]["json"]
        # /no_think が無いと Qwen3 が数千トークン思考してタイムアウトする
        assert body["prompt"].startswith("/no_think"), body["prompt"][:40]
        assert "本文プロンプト" in body["prompt"]
        assert body["model"] == hybrid.RERANK_OLLAMA_MODEL
        assert body["stream"] is False
        # 順位付けは決定的であってほしい (計測の再現性)
        assert body["options"]["temperature"] == 0

        # env でモデルを差し替えられる
        os.environ["VAULT_SEARCH_RERANK_OLLAMA_MODEL"] = "qwen2.5-coder:7b"
        with _patched_post(spy):
            _call_ollama_llm("x")
        assert spy.calls[-1]["json"]["model"] == "qwen2.5-coder:7b"

        # 明示引数が env より強い
        with _patched_post(spy):
            _call_ollama_llm("x", model="qwen3:0.6b")
        assert spy.calls[-1]["json"]["model"] == "qwen3:0.6b"
    finally:
        _restore_env(saved)
    print("✅ ollama: /no_think 前置・temperature=0・モデル選択の優先順位")


# ---------------------------------------------------------
# 4〜5. 失敗の伝え方
# ---------------------------------------------------------

def test_failures_raise_readable_runtime_error():
    """失敗は "[ERROR] ..." 文字列ではなく例外にする。

    文字列で返すと _parse_json_array が黙って [] を返し、
    「有効な番号が0件」という**理由を失った**フォールバックになる。
    """
    cases = [
        (requests.exceptions.ConnectionError("refused"), "Ollama not running"),
        (requests.exceptions.Timeout("too slow"), "timeout"),
        (requests.exceptions.HTTPError("500"), "failed"),
    ]
    for exc, expected in cases:
        spy = _PostSpy(raises=exc)
        with _patched_post(spy):
            try:
                _call_ollama_llm("q", model="qwen3:1.7b")
            except RuntimeError as e:
                assert expected.lower() in str(e).lower(), f"{exc}: {e}"
            except Exception as e:
                raise AssertionError(f"RuntimeError であるべき: {type(e).__name__}: {e}")
            else:
                raise AssertionError(f"例外が出ていない: {exc}")

    # HTTP 500 (raise_for_status 経由) も RuntimeError になる
    spy = _PostSpy(_FakeResponse({}, status=500))
    with _patched_post(spy):
        try:
            _call_ollama_llm("q")
        except RuntimeError as e:
            assert "500" in str(e), e
        else:
            raise AssertionError("HTTP 500 で例外が出ていない")
    print("✅ ollama: 接続不可/タイムアウト/HTTPエラーが読める RuntimeError になる")


def test_failure_reason_reaches_the_note():
    """Ollama の失敗理由が戻り値の note に残る (無言で劣化しない)"""
    saved = _save_env()
    try:
        os.environ["VAULT_SEARCH_RERANK_BACKEND"] = "ollama"
        cands = [_cand("a.md", 0.03), _cand("b.md", 0.02)]
        spy = _PostSpy(raises=requests.exceptions.ConnectionError("refused"))
        with _patched_post(spy):
            ordered, note = _llm_rerank("質問", cands, enabled=True)

        assert [h["rel_path"] for h in ordered] == ["a.md", "b.md"], ordered
        assert "ollama" in note, note
        assert "RuntimeError" in note, note
        assert "Ollama not running" in note, note
    finally:
        _restore_env(saved)
    print("✅ note: Ollama の失敗理由とバックエンド名が戻り値に残る")


# ---------------------------------------------------------
# 6. 検索を道連れにしない
# ---------------------------------------------------------

def test_search_survives_dead_ollama():
    saved = _save_env()
    try:
        os.environ["VAULT_SEARCH_RERANK_BACKEND"] = "ollama"
        os.environ["VAULT_SEARCH_RERANK"] = "1"
        spy = _PostSpy(raises=requests.exceptions.ConnectionError("refused"))
        with _patched_post(spy):
            hits, notes, queries = _hybrid_hits(
                "質問", top_k=3,
                keyword_fn=lambda q: [
                    {"rel_path": "a.md", "filename": "a.md", "score": 3, "snippet": "s"},
                    {"rel_path": "b.md", "filename": "b.md", "score": 1, "snippet": "s"},
                ],
                semantic_fn=lambda q, k: [])
        # Ollama が死んでいても RRF 順の結果は返る
        assert [h["rel_path"] for h in hits] == ["a.md", "b.md"], hits
        assert any("ollama" in n for n in notes), notes
    finally:
        _restore_env(saved)
    print("✅ resilience: Ollama が落ちていても検索結果は RRF 順で返る")


# ---------------------------------------------------------
# 7. OFF ならバックエンドに関係なく呼ばない
# ---------------------------------------------------------

def test_rerank_off_never_calls_any_backend():
    saved = _save_env()
    try:
        os.environ["VAULT_SEARCH_RERANK"] = "0"
        for backend in ("ollama", "gemini"):
            os.environ["VAULT_SEARCH_RERANK_BACKEND"] = backend
            spy = _PostSpy(_FakeResponse({"response": "[1]"}))
            with _patched_post(spy):
                cands = [_cand("a.md", 0.03)]
                ordered, note = _llm_rerank("質問", cands)
            assert spy.calls == [], f"{backend}: OFF なのに呼ばれた"
            assert note == "", note
    finally:
        _restore_env(saved)
    print("✅ flags: リランク OFF ならどのバックエンドも呼ばない")


# ---------------------------------------------------------
# 8. 既定バックエンドの固定
# ---------------------------------------------------------

def test_default_backend_matches_the_measurement():
    """既定バックエンドは実測で決めた値であること。

    ローカル (ollama) を既定にしてよいのは
    hit@5 >= 1.0 かつ mrr@10 >= 0.8167 (Gemini リランクの実測値) かつ
    平均 <= 10秒 を満たしたときだけ、と先に決めてある。
    採用したのは qwen2.5-coder:7b + CPU埋め込み (1.0 / 0.8722 / 8.30秒)。
    数値は logs/eval/retrieval_20260728_1029_ollama_coder7b_cpuembed.json。
    """
    assert RERANK_BACKEND_DEFAULT in ("gemini", "ollama"), RERANK_BACKEND_DEFAULT
    saved = _save_env()
    try:
        os.environ.pop("VAULT_SEARCH_RERANK_BACKEND", None)
        _, name = _resolve_rerank_llm()
        assert name == RERANK_BACKEND_DEFAULT
        # 基準を満たしたのは coder7b だけ。qwen3:1.7b は MRR 0.6244 で
        # リランク無し (0.7317) すら下回ったので、既定に戻ってはいけない。
        if RERANK_BACKEND_DEFAULT == "ollama":
            assert hybrid.RERANK_OLLAMA_MODEL == "qwen2.5-coder:7b", \
                hybrid.RERANK_OLLAMA_MODEL
    finally:
        _restore_env(saved)
    print(f"✅ defaults: 既定バックエンドは '{RERANK_BACKEND_DEFAULT}' (実測で決定)")


def test_embed_device_follows_the_backend():
    """クエリ埋め込みの CPU/GPU は、リランクのバックエンドと必ず連動する。

    VRAM 6GB に bge-m3 と 7B リランカは同居できない。ローカルリランクなのに
    埋め込みを GPU に置くと、検索のたびにモデルが入れ替わって
    8.3秒 → 15.3秒 になる (実測)。**動きはするので誰も気付けない**類の劣化。
    逆に gemini バックエンドで CPU 埋め込みにすると、譲る相手がいないのに
    ただ遅くなる。だから既定 (auto) は自動で揃える。
    """
    from tools.tool_vault_index import query_embed_on_cpu

    saved = dict(_save_env())
    saved["VAULT_QUERY_EMBED_DEVICE"] = os.environ.get("VAULT_QUERY_EMBED_DEVICE")
    try:
        os.environ.pop("VAULT_QUERY_EMBED_DEVICE", None)

        os.environ["VAULT_SEARCH_RERANK_BACKEND"] = "ollama"
        assert query_embed_on_cpu() is True, "ローカルリランクなら VRAM を譲る"

        os.environ["VAULT_SEARCH_RERANK_BACKEND"] = "gemini"
        assert query_embed_on_cpu() is False, "gemini なら GPU 埋め込みのままが速い"

        # 明示指定は auto より強い (どちらの向きも)
        os.environ["VAULT_QUERY_EMBED_DEVICE"] = "cpu"
        assert query_embed_on_cpu() is True
        os.environ["VAULT_SEARCH_RERANK_BACKEND"] = "ollama"
        os.environ["VAULT_QUERY_EMBED_DEVICE"] = "gpu"
        assert query_embed_on_cpu() is False

        # env 未設定時はコード既定のバックエンドに追従する
        os.environ.pop("VAULT_SEARCH_RERANK_BACKEND", None)
        os.environ.pop("VAULT_QUERY_EMBED_DEVICE", None)
        assert query_embed_on_cpu() is (RERANK_BACKEND_DEFAULT == "ollama")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("✅ 連動: 埋め込みデバイスがリランクのバックエンドに追従する")


def test_reasoning_model_options_are_opt_in():
    """format スキーマ / think:false は既定 OFF で、env で入れられること。

    2026-07-28 の実測で、この2つはモデルによって効果が正反対だった:
      qwen3:4b  — 無しだと /no_think を無視して1万文字思考し137秒。
                  スキーマ強制で 5.5秒に落ちる (ただし順位は素通しで無意味)
      coder7b   — スキーマを付けても品質は同じ (0.8722) で 5.73→8.38秒と遅くなるだけ
    採用モデル (coder7b) の実測条件を勝手に変えないよう既定は OFF。
    """
    saved = _save_env()
    saved["VAULT_SEARCH_RERANK_JSON_SCHEMA"] = os.environ.get("VAULT_SEARCH_RERANK_JSON_SCHEMA")
    saved["VAULT_SEARCH_RERANK_NO_THINK"] = os.environ.get("VAULT_SEARCH_RERANK_NO_THINK")
    try:
        for key in ("VAULT_SEARCH_RERANK_JSON_SCHEMA", "VAULT_SEARCH_RERANK_NO_THINK"):
            os.environ.pop(key, None)

        spy = _PostSpy(_FakeResponse({"response": "[1]"}))
        with _patched_post(spy):
            _call_ollama_llm("q")
        body = spy.calls[0]["json"]
        assert "format" not in body and "think" not in body, body

        os.environ["VAULT_SEARCH_RERANK_JSON_SCHEMA"] = "1"
        os.environ["VAULT_SEARCH_RERANK_NO_THINK"] = "1"
        spy2 = _PostSpy(_FakeResponse({"response": "[1]"}))
        with _patched_post(spy2):
            _call_ollama_llm("q")
        body2 = spy2.calls[0]["json"]
        assert body2["format"] == hybrid.RERANK_JSON_SCHEMA, body2
        assert body2["think"] is False, body2

        # 明示引数は env より強い
        spy3 = _PostSpy(_FakeResponse({"response": "[1]"}))
        with _patched_post(spy3):
            _call_ollama_llm("q", json_schema=False, no_think=False)
        assert "format" not in spy3.calls[0]["json"], spy3.calls[0]["json"]
        assert "think" not in spy3.calls[0]["json"], spy3.calls[0]["json"]
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("✅ options: format / think は既定 OFF・env と明示引数で入れられる")


def test_embed_payload_carries_cpu_and_keep_alive():
    """prefer_cpu=True のとき num_gpu:0 と keep_alive が実際に送られること。

    この2つが一番効いている最適化 (16.2秒 → 9.0秒)。消えても検索は動くので
    「なんか遅い」以外の症状が出ない = 気付けない。ペイロードを直接固定する。
    """
    from tools.tool_vault_index import _call_ollama_embed, OLLAMA_KEEP_ALIVE

    spy = _PostSpy(_FakeResponse({"embeddings": [[0.1, 0.2]]}))
    with _patched_post(spy):
        _call_ollama_embed(["q"], prefer_cpu=True)
    body = spy.calls[0]["json"]
    assert body["options"]["num_gpu"] == 0, body
    assert body["keep_alive"] == OLLAMA_KEEP_ALIVE, body

    # 既定 (GPU) では余計なオプションを付けない — インデックス構築の経路
    spy2 = _PostSpy(_FakeResponse({"embeddings": [[0.1, 0.2]]}))
    with _patched_post(spy2):
        _call_ollama_embed(["q"])
    body2 = spy2.calls[0]["json"]
    assert "options" not in body2 and "keep_alive" not in body2, body2

    # リランク側も常駐指定を送る (片方だけだと入れ替えが復活する)
    spy3 = _PostSpy(_FakeResponse({"response": "[1]"}))
    with _patched_post(spy3):
        _call_ollama_llm("q")
    assert spy3.calls[0]["json"]["keep_alive"] == OLLAMA_KEEP_ALIVE, spy3.calls[0]
    print("✅ payload: num_gpu:0 / keep_alive が埋め込み・リランク双方に載る")


def test_embed_fn_injection_does_not_double_call():
    """prefer_cpu 非対応の embed_fn でも二度呼ばない・内部の TypeError を隠さない。

    以前は try/except TypeError で握っていたため、
    (a) embed_fn の内部バグ (TypeError) を「引数違い」と誤認して隠し、
    (b) リトライで埋め込みAPIを二度叩いていた。
    """
    from tools.tool_vault_index import _semantic_hits, _connect, _normalize
    import shutil
    import tempfile
    import numpy as np

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "index.db")
    try:
        conn = _connect(db)
        arr = _normalize(np.asarray([1.0, 0.0], dtype=np.float32))
        conn.execute(
            "INSERT INTO chunks(filepath, chunk_index, heading, text, embedding, dim) "
            "VALUES(?, 0, ?, ?, ?, ?)", ("a.md", "見出し", "本文", arr.tobytes(), 2))
        conn.commit()
        conn.close()

        # prefer_cpu を受け取らない旧シグネチャ → 1回だけ呼ばれる
        calls = []

        def legacy_embed(texts):
            calls.append(texts)
            return [[1.0, 0.0]]

        hits = _semantic_hits("q", top_k=1, embed_fn=legacy_embed, db_path=db)
        assert isinstance(hits, list) and hits, hits
        assert len(calls) == 1, f"埋め込みが {len(calls)} 回呼ばれた"

        # **kwargs を持つものには渡す
        got = {}

        def kwargs_embed(texts, **kw):
            got.update(kw)
            return [[1.0, 0.0]]

        _semantic_hits("q", top_k=1, embed_fn=kwargs_embed, db_path=db)
        assert "prefer_cpu" in got, got

        # 内部の TypeError は握りつぶさずに伝播する
        def buggy_embed(texts, prefer_cpu=False):
            raise TypeError("内部のバグ (引数違いではない)")

        try:
            _semantic_hits("q", top_k=1, embed_fn=buggy_embed, db_path=db)
        except TypeError as e:
            assert "内部のバグ" in str(e), e
        else:
            raise AssertionError("内部の TypeError が隠された")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ injection: 二重呼び出しなし・内部 TypeError を隠さない")


def test_index_build_never_uses_cpu_embedding():
    """インデックス構築は CPU 埋め込みにしない (数千チャンクのスループット勝負)。

    query_embed_on_cpu() は _semantic_hits (検索時) からしか呼ばれてはいけない。
    update_vault_index が巻き込まれると夜間バッチが桁違いに遅くなる。
    """
    import ast
    path = os.path.join(_PROJECT_ROOT, "tools", "tool_vault_index.py")
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())

    callers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and getattr(inner.func, "id", None) == "query_embed_on_cpu"):
                callers.append(node.name)
    assert callers == ["_semantic_hits"], \
        f"query_embed_on_cpu の呼び出し元は _semantic_hits だけであるべき: {callers}"
    print("✅ 分離: CPU埋め込みは検索時のみ (インデックス構築は GPU のまま)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
