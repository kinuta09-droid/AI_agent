# -*- coding: utf-8 -*-
"""tool_web_search の recency_days マージ検索の回帰テスト

このテストが存在する理由 (2026-08-03):
  「今日ガソリン価格が急に下がった理由」への回答が、直近1週間の一次情報
  (補助金単価改定) ではなく数ヶ月前の別の制度変更を持ち出して誤答した。
  原因は tool_web_search が Tavily を鮮度フィルタなしで呼んでおり、
  SEO最適化された古い解説記事の方が一般検索で上位に来やすかったこと。

  recency_days を追加する際、単純に topic="news", days=N へ「置き換える」
  設計にすると、答えが検索窓より前の出来事だった場合に正しい情報ごと
  落ちる新しい欠陥を作る (/grill で指摘・2026-08-03)。このテストは
  「一般検索は必ず残り、news検索は追加でしかない」ことを固定する。

Tavily / Gemini はスタブ (tests/test_curate_note_save.py と同じ手法)。
外部通信は一切しない。

実行: py tests/test_tool_web_recency.py
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
    def generate_content(self, model=None, contents=None):
        return types.SimpleNamespace(text="STUB")


class _FakeGenaiClient:
    def __init__(self, **kw):
        self.models = _FakeModels()


class _FakeTavily:
    """呼び出し引数を記録し、仕込んだ応答を順番に払い出す。"""

    def __init__(self, **kw):
        self.calls = []
        self.responses = []

    def search(self, **kw):
        self.calls.append(kw)
        if self.responses:
            return self.responses.pop(0)
        return {"results": []}


def _install_module_stubs():
    _stub("dotenv", load_dotenv=lambda *a, **k: None)
    genai = _stub("google.genai", Client=_FakeGenaiClient)
    _stub("google.genai.types", HttpOptions=lambda **kw: None)
    genai.types = sys.modules["google.genai.types"]
    google_mod = _stub("google")
    google_mod.genai = genai
    _stub("tavily", TavilyClient=_FakeTavily)
    os.environ.setdefault("GOOGLE_API_KEY", "stub")
    os.environ.setdefault("TAVILY_API_KEY", "stub")


_install_module_stubs()

import tools.tool_web as tw  # noqa: E402


def _reset():
    tw.tavily_client.calls = []
    tw.tavily_client.responses = []


# ---------------------------------------------------------
# 1. recency_days 未指定時は挙動不変
# ---------------------------------------------------------

def test_no_recency_single_general_call():
    print("\n[1] recency_days未指定 → 一般検索1回だけ、挙動不変")
    _reset()
    tw.tavily_client.responses = [
        {"results": [{"title": "A", "url": "https://a.example", "content": "a"}]},
    ]
    out = tw.tool_web_search("テストクエリ")

    check("Tavily呼び出しは1回のみ", len(tw.tavily_client.calls) == 1,
          str(tw.tavily_client.calls))
    call = tw.tavily_client.calls[0]
    check("topicを渡していない (一般検索のまま)", "topic" not in call, call)
    check("daysを渡していない", "days" not in call, call)
    check("結果が反映されている", "https://a.example" in out, out)


# ---------------------------------------------------------
# 2. recency_days 指定時は一般検索+news検索の2回になる
# ---------------------------------------------------------

def test_recency_days_adds_news_call():
    print("\n[2] recency_days=7 → 一般検索+news検索の2回")
    _reset()
    tw.tavily_client.responses = [
        {"results": [{"title": "General", "url": "https://gen.example", "content": "g"}]},
        {"results": [{"title": "News", "url": "https://news.example", "content": "n"}]},
    ]
    out = tw.tool_web_search("ガソリン価格 今日 値下げ", recency_days=7)

    check("Tavily呼び出しが2回になる", len(tw.tavily_client.calls) == 2,
          str(tw.tavily_client.calls))
    general_call, news_call = tw.tavily_client.calls
    check("1回目は一般検索のまま (topic指定なし)", "topic" not in general_call, general_call)
    check("2回目はtopic=newsで呼ばれる", news_call.get("topic") == "news", news_call)
    check("2回目はdays=7で呼ばれる", news_call.get("days") == 7, news_call)
    check("両方の結果が本文に含まれる",
          "https://gen.example" in out and "https://news.example" in out, out)


# ---------------------------------------------------------
# 3. 重複除去と取りこぼしゼロの確認 (置き換え設計への回帰を防ぐ)
# ---------------------------------------------------------

def test_recency_dedup_by_url():
    print("\n[3] 重複URLは1件にまとめ、一般検索限定のURLも残る")
    _reset()
    tw.tavily_client.responses = [
        {"results": [
            {"title": "OnlyGeneral", "url": "https://only-general.example", "content": "og"},
            {"title": "Both", "url": "https://both.example", "content": "b-general"},
        ]},
        {"results": [
            {"title": "Both", "url": "https://both.example", "content": "b-news"},
            {"title": "OnlyNews", "url": "https://only-news.example", "content": "on"},
        ]},
    ]
    out = tw.tool_web_search("直近の出来事", recency_days=14)

    check("一般検索限定のURLが残っている (取りこぼしゼロ)",
          "https://only-general.example" in out, out)
    check("news検索限定のURLも追加されている",
          "https://only-news.example" in out, out)
    check("重複URLは1回しか出てこない",
          out.count("https://both.example") == 1, out)


if __name__ == "__main__":
    print("=" * 55)
    print(" tool_web_search recency_days マージ検索 回帰テスト")
    print("=" * 55)

    test_no_recency_single_general_call()
    test_recency_days_adds_news_call()
    test_recency_dedup_by_url()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
