# -*- coding: utf-8 -*-
"""
tool_xtrend.py の回帰テスト (実 Twitter API / Gemini 不要)

方針:
  - ネットワークに一切出ない。twitterAPI.io / Gemini はスタブに差し替える。
  - 項目9 (収集クエリの動的チューニング) Phase 0 で追加した source_query 逆引きと
    engagement_log への impression 記録が正しく動くことだけを確認する。
    本番の logs/engagement.jsonl には触れない (log_impressions を DI で隔離)。

実行: py tests/test_xtrend.py
"""
import os
import sys
import json
import tempfile
import shutil

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_xtrend as xtrend
from lib.engagement_log import log_impressions as real_log_impressions, read_events

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _feature(name, post_url, score=100, arms=3, diff=2):
    return {"feature_name": name, "summary": "s", "problem_solved": "p", "tech_stack": "t",
            "difficulty": diff, "arms_affinity": arms, "arms_rationale": "r",
            "post_url": post_url, "engagement_score": score}


# ---------------------------------------------------------
# 1. post_url から source_query を逆引きする
# ---------------------------------------------------------

def test_analyze_posts_attaches_source_query():
    print("\n[1] analyze_posts: post_url から source_query を逆引きする")
    posts = [
        {"id": "1", "text": "MCP is great", "url": "https://x.com/1", "likes": 600, "retweets": 100,
         "query": "MCP model context protocol"},
        {"id": "2", "text": "RAG pipeline tips", "url": "https://x.com/2", "likes": 700, "retweets": 120,
         "query": "RAG retrieval augmented generation pipeline"},
    ]
    fake_response = json.dumps([
        _feature("MCP対応", "https://x.com/1", score=800),
        _feature("RAG改善", "https://x.com/2", score=940),
    ], ensure_ascii=False)

    orig = xtrend._call_gemini_analyze
    xtrend._call_gemini_analyze = lambda posts_json: fake_response
    try:
        features = xtrend.analyze_posts(posts)
    finally:
        xtrend._call_gemini_analyze = orig

    by_name = {f["feature_name"]: f for f in features}
    check("2件抽出される", len(features) == 2, str(features))
    check("MCP対応 の source_query が正しい",
          by_name.get("MCP対応", {}).get("source_query") == "MCP model context protocol",
          str(by_name.get("MCP対応")))
    check("RAG改善 の source_query が正しい",
          by_name.get("RAG改善", {}).get("source_query") == "RAG retrieval augmented generation pipeline",
          str(by_name.get("RAG改善")))


# ---------------------------------------------------------
# 2. post_url が posts に見つからなければ空文字にフォールバック
# ---------------------------------------------------------

def test_analyze_posts_unknown_post_url_is_empty():
    print("\n[2] analyze_posts: post_url が posts に無ければ source_query は空文字")
    posts = [{"id": "1", "text": "x", "url": "https://x.com/1", "likes": 600, "retweets": 100,
              "query": "q1"}]
    fake_response = json.dumps([
        _feature("謎機能", "https://x.com/does-not-exist", score=10),
    ], ensure_ascii=False)

    orig = xtrend._call_gemini_analyze
    xtrend._call_gemini_analyze = lambda posts_json: fake_response
    try:
        features = xtrend.analyze_posts(posts)
    finally:
        xtrend._call_gemini_analyze = orig

    check("source_query は空文字にフォールバック",
          features[0].get("source_query") == "", str(features))


# ---------------------------------------------------------
# 3. run(): impression 記録が本番ログを汚染しない (DI で隔離)
# ---------------------------------------------------------

def test_run_logs_impressions_without_touching_production_log():
    print("\n[3] run(): impression記録がDIで隔離した先に記録される")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")

        orig_fetch = xtrend.fetch_trending_ai_posts
        orig_analyze = xtrend.analyze_posts
        orig_log_impressions = xtrend.log_impressions

        posts = [{"id": "1", "text": "x", "url": "https://x.com/1", "likes": 600, "retweets": 100,
              "query": "q1"}]
        features = [_feature("機能A", "https://x.com/1")]
        features[0]["source_query"] = "q1"

        xtrend.fetch_trending_ai_posts = lambda queries, min_likes, min_rt: posts
        xtrend.analyze_posts = lambda posts: features

        calls = []

        def _spy(items, source, meta=None, item_type=""):
            calls.append((items, source))
            return real_log_impressions(items, source, meta=meta, item_type=item_type,
                                        log_path=log_path)

        xtrend.log_impressions = _spy
        try:
            xtrend.run(queries=["q1"], no_save=True)
        finally:
            xtrend.fetch_trending_ai_posts = orig_fetch
            xtrend.analyze_posts = orig_analyze
            xtrend.log_impressions = orig_log_impressions

        check("log_impressions が1回呼ばれる", len(calls) == 1, str(calls))
        events = read_events(log_path=log_path)
        check("隔離したログに1件記録される", len(events) == 1, str(events))
        check("source は xtrend", events and events[0]["source"] == "xtrend",
              str(events))
        check("meta.source_query が伝播する",
              events and events[0]["meta"].get("source_query") == "q1", str(events))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_analyze_prompt_includes_arms_architecture():
    """arms_affinity の判定材料として ARMS の構成要約がプロンプトに入っている。
    抜けると Gemini は名前だけで採点し、導入済みの Claude Code/MCP/Jev を★5 にする。"""
    from tools.tool_skill_radar import _ARMS_ARCHITECTURE_SUMMARY
    check("ANALYZE_SYSTEM に ARMS 構成要約が含まれる",
          _ARMS_ARCHITECTURE_SUMMARY in xtrend.ANALYZE_SYSTEM)
    check("構成要約に導入済み技術と動作環境が載っている",
          "導入済み" in _ARMS_ARCHITECTURE_SUMMARY and "Windows" in _ARMS_ARCHITECTURE_SUMMARY)


def test_note_article_picks_by_engagement_not_arms():
    """note記事は一般読者向け: ARMS親和性★1でも話題の技術を上位に選ぶ。
    save_to_obsidian の出力を xtrend_to_note が読む往復で確認する。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "xtrend_to_note", os.path.join(_PROJECT_ROOT, "scripts", "xtrend_to_note.py"))
    to_note = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(to_note)

    tmp = tempfile.mkdtemp()
    orig_dir = xtrend.XTREND_DIR
    xtrend.XTREND_DIR = tmp
    try:
        features = [
            {"feature_name": "Niche", "difficulty": 2, "arms_affinity": 5, "engagement_score": 900},
            {"feature_name": "Claude Code", "difficulty": 3, "arms_affinity": 1, "engagement_score": 7597},
            {"feature_name": "Mid", "difficulty": 1, "arms_affinity": 3, "engagement_score": "2750.0"},
        ]
        xtrend.save_to_obsidian(features, [])
        md_files = [f for f in os.listdir(tmp) if f.endswith(".md")]
        md = open(os.path.join(tmp, md_files[0]), encoding="utf-8").read()
        names = [f["name"] for f in to_note.extract_top_features(md, top_n=3)]
        check("記事の題材はエンゲージメント順", names == ["Claude Code", "Mid", "Niche"], str(names))
        old_md = md.replace("| エンゲージメント |", "| (旧形式) |")
        names_old = [f["name"] for f in to_note.extract_top_features(old_md, top_n=3)]
        check("エンゲージメント行の無い旧レポートは元の順を保つ",
              names_old == ["Niche", "Claude Code", "Mid"], str(names_old))
    finally:
        xtrend.XTREND_DIR = orig_dir
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("=== test_xtrend ===")
    test_note_article_picks_by_engagement_not_arms()
    test_analyze_prompt_includes_arms_architecture()
    test_analyze_posts_attaches_source_query()
    test_analyze_posts_unknown_post_url_is_empty()
    test_run_logs_impressions_without_touching_production_log()

    print(f"\n=== 結果: {len(_PASS)} passed / {len(_FAIL)} failed ===")
    if _FAIL:
        for f in _FAIL:
            print(f"  - {f}")
        sys.exit(1)
