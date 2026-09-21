# -*- coding: utf-8 -*-
"""tool_web_search_grounded (Google Search Grounding) の回帰テスト

plans/google-search-grounding.md Step 5。Step 0 の実機確認 (2026-08-03) で
判明した以下の実際の挙動を固定する:
  - パスは response.candidates[0].grounding_metadata.grounding_chunks[i].web
  - web.title はドメイン風の短い文字列、web.uri はredirect URL (元URLではない)
  - web.domain は実測では常に None (使わない)
  - grounding_metadata が None / grounding_chunks が空リストになるケースは
    実機では未発生だったが、API設計上あり得るため例外にならず本文のみ返す
    ガードが必要 (推測部分)

GROUNDING_COUNTER は grounding_daily_calls.json への実書き込みを避けるため
FakeCounter に差し替える (tests/test_crypto_sweep.py と同じ手法)。
google.genai / dotenv / tavily はスタブし外部通信は一切しない
(tests/test_tool_web_recency.py と同じ手法)。

実行: py tests/test_tool_web_grounded.py
"""
import os
import sys
import types

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------
# 外部依存のスタブ (lib.lib_shared の import より前に入れる)
# ---------------------------------------------------------

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeModels:
    """呼び出し引数を記録し、仕込んだ応答を順番に払い出す。"""

    def __init__(self):
        self.calls = []
        self.responses = []

    def generate_content(self, model=None, contents=None, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return types.SimpleNamespace(text="STUB", candidates=[])


class _FakeGenaiClient:
    def __init__(self, **kw):
        self.models = _FakeModels()


class _FakeTavily:
    def __init__(self, **kw):
        pass

    def search(self, **kw):
        return {"results": []}


def _install_module_stubs():
    _stub("dotenv", load_dotenv=lambda *a, **k: None)

    class _FakeGoogleSearch:
        def __init__(self, **kw):
            self.kw = kw

    class _FakeTool:
        def __init__(self, **kw):
            self.kw = kw

    class _FakeGenerateContentConfig:
        def __init__(self, **kw):
            self.kw = kw

    genai = _stub("google.genai", Client=_FakeGenaiClient)
    genai_types = _stub(
        "google.genai.types",
        HttpOptions=lambda **kw: None,
        GoogleSearch=_FakeGoogleSearch,
        Tool=_FakeTool,
        GenerateContentConfig=_FakeGenerateContentConfig,
    )
    genai.types = genai_types
    google_mod = _stub("google")
    google_mod.genai = genai
    _stub("tavily", TavilyClient=_FakeTavily)
    os.environ.setdefault("GOOGLE_API_KEY", "stub")
    os.environ.setdefault("TAVILY_API_KEY", "stub")


_install_module_stubs()

import tools.tool_web as tw  # noqa: E402

# dotenv スタブで .env の DAILY_GEMINI_*_LIMIT=0 が読まれず既定200に落ちる一方、
# lib_shared の予算ラッパーは本番 logs/gemini_*_daily_calls.json を加算し続ける →
# 本番カウントが200を超えた日だけ赤くなる (test_curate_note_save と同じ罠、2026-09-21再発)。
# カウンタを一時パス・上限無し・通知無しに差し替えて隔離する。
import tempfile  # noqa: E402
from lib import cost_guard as _cg  # noqa: E402
_counter_dir = tempfile.mkdtemp(prefix="web_test_counter_")
for _attr in ("GEMINI_LIGHT_COUNTER", "GEMINI_HEAVY_COUNTER"):
    setattr(_cg, _attr, _cg.DailyCounter(path=os.path.join(_counter_dir, _attr + ".json"),
                                        limit=0, name=f"test:{_attr}", notify=False))
from lib.cost_guard import GeminiBudgetExceeded  # noqa: E402


class FakeCounter:
    """GROUNDING_COUNTER の差替え。limit 回まで通し、以降は予算超過を送出。"""

    def __init__(self, limit=99):
        self.limit = limit
        self.count = 0

    def check_and_increment(self, n=1):
        if self.count >= self.limit:
            raise GeminiBudgetExceeded("Google Search Grounding", self.limit)
        self.count += n
        return self.count


def _reset(counter_limit=99):
    tw.client.models.calls = []
    tw.client.models.responses = []
    orig_counter = tw.GROUNDING_COUNTER
    tw.GROUNDING_COUNTER = FakeCounter(limit=counter_limit)
    return orig_counter


def _chunk(title, uri):
    web = types.SimpleNamespace(title=title, uri=uri)
    return types.SimpleNamespace(web=web)


def _response(text, chunks=None, with_candidate=True):
    if not with_candidate:
        return types.SimpleNamespace(text=text, candidates=[])
    gm = types.SimpleNamespace(grounding_chunks=chunks) if chunks is not None else None
    return types.SimpleNamespace(
        text=text, candidates=[types.SimpleNamespace(grounding_metadata=gm)]
    )


# ---------------------------------------------------------
# 1. 正常系: 複数URLがある場合に整形される
# ---------------------------------------------------------

def test_grounded_search_formats_multiple_sources():
    print("\n[1] 複数の参照元URLが本文の後に整形されて付く")
    orig_counter = _reset()
    try:
        tw.client.models.responses = [_response(
            "ガソリン価格は補助金により抑えられています。",
            chunks=[_chunk("oanda.jp", "https://vertexaisearch.example/redirect1"),
                    _chunk("fnn.jp", "https://vertexaisearch.example/redirect2")],
        )]
        out = tw.tool_web_search_grounded("今日のガソリン価格のニュース")

        check("本文がそのまま含まれる", "ガソリン価格は補助金により抑えられています。" in out, out)
        check("参照元見出しが付く", "[参照元 (Google検索)]" in out, out)
        check("1件目のtitle/uriが含まれる",
              "oanda.jp" in out and "https://vertexaisearch.example/redirect1" in out, out)
        check("2件目のtitle/uriが含まれる",
              "fnn.jp" in out and "https://vertexaisearch.example/redirect2" in out, out)
        check("GROUNDING_COUNTERが1回消費される", tw.GROUNDING_COUNTER.count == 1,
              tw.GROUNDING_COUNTER.count)
    finally:
        tw.GROUNDING_COUNTER = orig_counter


# ---------------------------------------------------------
# 2. 異常系: grounding_metadata が None
# ---------------------------------------------------------

def test_grounding_metadata_none_returns_text_only():
    print("\n[2] grounding_metadataがNoneでも例外にならず本文のみ返す")
    orig_counter = _reset()
    try:
        tw.client.models.responses = [_response("一般的な回答本文。", chunks=None)]
        out = tw.tool_web_search_grounded("一般的な質問")

        check("本文がそのまま返る", out == "一般的な回答本文。", out)
        check("参照元見出しは付かない", "[参照元" not in out, out)
        check("Errorにならない", not out.startswith("Error"), out)
    finally:
        tw.GROUNDING_COUNTER = orig_counter


# ---------------------------------------------------------
# 3. 異常系: candidates が空リスト (grounding_metadata に到達すらしない)
# ---------------------------------------------------------

def test_no_candidates_returns_text_only():
    print("\n[3] candidatesが空でも例外にならず本文のみ返す")
    orig_counter = _reset()
    try:
        tw.client.models.responses = [_response("candidatesなしの本文。", with_candidate=False)]
        out = tw.tool_web_search_grounded("質問")

        check("本文がそのまま返る", out == "candidatesなしの本文。", out)
        check("Errorにならない", not out.startswith("Error"), out)
    finally:
        tw.GROUNDING_COUNTER = orig_counter


# ---------------------------------------------------------
# 4. 異常系: grounding_chunks が空リスト
# ---------------------------------------------------------

def test_empty_chunks_returns_text_only():
    print("\n[4] grounding_chunksが空リストでも例外にならず本文のみ返す")
    orig_counter = _reset()
    try:
        tw.client.models.responses = [_response("空リストの本文。", chunks=[])]
        out = tw.tool_web_search_grounded("質問")

        check("本文がそのまま返る", out == "空リストの本文。", out)
        check("Errorにならない", not out.startswith("Error"), out)
    finally:
        tw.GROUNDING_COUNTER = orig_counter


# ---------------------------------------------------------
# 5. 参照元の件数を上限で絞る (下流の1500字切り詰め対策)
# ---------------------------------------------------------

def test_sources_are_capped():
    print("\n[5] 参照元は_GROUNDING_MAX_SOURCES件までに絞られる")
    orig_counter = _reset()
    try:
        many_chunks = [_chunk(f"site{i}.example", f"https://example/{i}") for i in range(10)]
        tw.client.models.responses = [_response("本文", chunks=many_chunks)]
        out = tw.tool_web_search_grounded("質問")

        shown = sum(1 for i in range(10) if f"https://example/{i}" in out)
        check(f"参照元は{tw._GROUNDING_MAX_SOURCES}件までしか含まれない",
              shown == tw._GROUNDING_MAX_SOURCES, f"shown={shown}, out={out}")
    finally:
        tw.GROUNDING_COUNTER = orig_counter


# ---------------------------------------------------------
# 6. GROUNDING_COUNTER 連携: 上限到達時に Error: ⛔... が返る
# ---------------------------------------------------------

def test_source_extraction_failure_does_not_discard_text():
    """引用元抽出中の例外 (SDKバージョン差異等) が、既に取得済みの本文まで
    "Error" 扱いで握りつぶさないことを固定する (/grill 指摘・2026-08-03)。
    本文取得と引用元抽出を同じtry/exceptに入れると、抽出側だけの不具合で
    正常な回答が丸ごと失敗表示になる回帰が起きる。
    """
    print("\n[7] 引用元抽出が例外を投げても、取得済みの本文は失われない")
    orig_counter = _reset()
    try:
        class _BoomGroundingMetadata:
            @property
            def grounding_chunks(self):
                raise RuntimeError("boom: unexpected grounding_metadata shape")

        response = types.SimpleNamespace(
            text="取得済みの正常な回答本文。",
            candidates=[types.SimpleNamespace(grounding_metadata=_BoomGroundingMetadata())],
        )
        tw.client.models.responses = [response]
        out = tw.tool_web_search_grounded("質問")

        check("本文が保持される (Errorに落ちない)", out == "取得済みの正常な回答本文。", out)
        check("Errorにならない", not out.startswith("Error"), out)
    finally:
        tw.GROUNDING_COUNTER = orig_counter


def test_budget_exceeded_returns_failure_prefix():
    print("\n[6] 日次上限到達時、Gemini呼び出し前に失敗プレフィックス付きで返る")
    orig_counter = _reset(counter_limit=0)
    try:
        tw.client.models.responses = [_response("呼ばれてはいけない本文")]
        out = tw.tool_web_search_grounded("質問")

        check("失敗プレフィックスを含む", out.startswith("Error searching web (grounded):"), out)
        check("上限超過の日本語メッセージを含む", "⛔" in out, out)
        check("上限到達時はGemini呼び出し自体が起きない (課金前にガード)",
              len(tw.client.models.calls) == 0, tw.client.models.calls)
    finally:
        tw.GROUNDING_COUNTER = orig_counter


if __name__ == "__main__":
    print("=" * 55)
    print(" tool_web_search_grounded (Google Search Grounding) 回帰テスト")
    print("=" * 55)

    test_grounded_search_formats_multiple_sources()
    test_grounding_metadata_none_returns_text_only()
    test_no_candidates_returns_text_only()
    test_empty_chunks_returns_text_only()
    test_sources_are_capped()
    test_source_extraction_failure_does_not_discard_text()
    test_budget_exceeded_returns_failure_prefix()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
