# -*- coding: utf-8 -*-
"""
FutureSearch リニューアルの回帰テスト (実LLM/実Ollama/実Tavily不要、全DI)

実行: py tests/test_future_search_pipeline.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_future_search as fs

SOURCES = [
    {"title": f"Source {i}", "url": f"https://example.com/{i}", "content": f"content {i} " * 50}
    for i in range(15)
]


def test_is_mostly_ascii():
    assert fs._is_mostly_ascii("AI agents 2026") is True
    assert fs._is_mostly_ascii("AIエージェントの未来") is False
    assert fs._is_mostly_ascii("") is True
    print("✅ _is_mostly_ascii: 英語/日本語判定")


def test_translate_english_passthrough():
    # 英語トピックはLLMを呼ばず素通し
    called = []
    def stub(prompt):
        called.append(prompt)
        return "SHOULD NOT BE USED"
    result = fs._translate_to_english_query("AI agents 2026", llm_fn=stub)
    assert result == "AI agents 2026"
    assert called == [], "英語入力でLLMが呼ばれてはいけない"
    print("✅ 英語トピックは翻訳スキップ (LLM呼び出しゼロ)")


def test_translate_japanese_uses_llm():
    def stub(prompt):
        assert "AIエージェントの未来" in prompt
        return '"future of AI agents"'  # 引用符付きで返しても除去される
    result = fs._translate_to_english_query("AIエージェントの未来", llm_fn=stub)
    assert result == "future of AI agents", result
    print("✅ 日本語トピックは英訳 + 引用符除去")


def test_translate_failure_fallback():
    def raising(prompt):
        raise RuntimeError("API down")
    result = fs._translate_to_english_query("日本語トピック", llm_fn=raising)
    assert result == "日本語トピック", "翻訳失敗時は元トピックにフォールバック"
    print("✅ 翻訳失敗 → 元トピックにフォールバック (検索は続行)")


def test_build_multi_lens_prompt_contents():
    prompt = fs._build_multi_lens_prompt("量子コンピュータ", SOURCES[:10], "ユーザーは研究者", "standard")
    # 10視点すべて含む
    for persona in fs.AGENT_PERSONAS:
        assert persona["name"] in prompt, f"視点 {persona['name']} が欠落"
    # 全ソースが含まれる
    assert "SOURCE 1" in prompt and "SOURCE 10" in prompt
    # user_context が含まれ、双方向の個人化ルール (積極活用 + こじつけ抑制) が有効化される
    assert "ユーザーは研究者" in prompt
    assert "積極的に活かし" in prompt          # 関連あれば積極的に
    assert "こじつけは避ける" in prompt        # 無関係なら抑制
    assert "量子コンピュータ" in prompt
    print("✅ マルチレンズプロンプト: 10視点 + 全ソース + 双方向個人化ルール")


def test_build_prompt_without_user_context():
    prompt = fs._build_multi_lens_prompt("topic", SOURCES[:5], "", "quick")
    # user_context 空なら個人化ルールは入らない (プロンプトを汚さない)
    assert "積極的に活かし" not in prompt
    assert "こじつけ" not in prompt
    print("✅ user_context 空 → 個人化ルールなし")


def test_depth_source_counts():
    assert fs.DEPTH_CONFIG["quick"]["sources"] == 5
    assert fs.DEPTH_CONFIG["standard"]["sources"] == 10
    assert fs.DEPTH_CONFIG["deep"]["sources"] == 15
    print("✅ depth → ソース数マッピング")


def _patch(monkey: dict):
    """fs モジュールの関数を一時差し替えするコンテキスト用ヘルパー。"""
    originals = {name: getattr(fs, name) for name in monkey}
    for name, fn in monkey.items():
        setattr(fs, name, fn)
    return originals


def _restore(originals: dict):
    for name, fn in originals.items():
        setattr(fs, name, fn)


def test_run_pipeline_orchestration():
    """run_future_search が各段階を正しい順序で呼び、progress_cb を発火すること。"""
    progress = []
    calls = []
    monkey = {
        "_translate_to_english_query": lambda topic, llm_fn=None: (calls.append("translate"), "en query")[1],
        "_search_topic": lambda q, max_results=10: (calls.append(("search", max_results)), SOURCES[:max_results])[1],
        "_load_user_context": lambda: (calls.append("context"), "ctx")[1],
        "_multi_lens_analysis": lambda t, s, c, d: (calls.append(("analyze", d, len(s))), "# レポート本文")[1],
        "_generate_happiness_advice": lambda t, r, c: (calls.append("happiness"), "## 幸福アドバイス")[1],
        "_save_to_obsidian": lambda t, r: (calls.append(("save", t)), "SavedFile")[1],
        "_notify_discord": lambda t, r, f: calls.append("notify"),
        "_generate_uncensored_take": lambda t, r, c: (calls.append("UNCENSORED"), "辛口")[1],
    }
    originals = _patch(monkey)
    try:
        result = fs.run_future_search("トピック", depth="quick", uncensored=False,
                                      progress_cb=lambda m: progress.append(m))
    finally:
        _restore(originals)

    # quick → 5ソース検索、辛口は呼ばれない
    assert ("search", 5) in calls, calls
    assert ("analyze", "quick", 5) in calls, calls
    assert "UNCENSORED" not in calls, "uncensored=False で辛口が呼ばれてはいけない"
    assert "happiness" in calls and "notify" in calls
    assert len(progress) >= 4, f"進捗が少なすぎる: {progress}"
    assert "[[SavedFile]]" in result
    print(f"✅ パイプライン順序 + progress_cb {len(progress)}回発火 + 辛口スキップ")


def test_run_pipeline_uncensored_and_no_callback():
    calls = []
    monkey = {
        "_translate_to_english_query": lambda topic, llm_fn=None: "en",
        "_search_topic": lambda q, max_results=10: SOURCES[:max_results],
        "_load_user_context": lambda: "",
        "_multi_lens_analysis": lambda t, s, c, d: "# レポート",
        "_generate_happiness_advice": lambda t, r, c: "## 幸福",
        "_save_to_obsidian": lambda t, r: "F",
        "_notify_discord": lambda t, r, f: None,
        "_generate_uncensored_take": lambda t, r, c: (calls.append("UNCENSORED"), "辛口")[1],
    }
    originals = _patch(monkey)
    try:
        # progress_cb=None でもクラッシュしない
        result = fs.run_future_search("topic", depth="deep", uncensored=True, progress_cb=None)
    finally:
        _restore(originals)
    assert "UNCENSORED" in calls, "uncensored=True で辛口が呼ばれるべき"
    assert "辛口診断" in result
    print("✅ uncensored=True → 辛口生成 / progress_cb=None でも動作")


def test_empty_topic_rejected():
    assert fs.run_future_search("").startswith("Error")
    assert fs.run_future_search("   ").startswith("Error")
    print("✅ 空トピックはエラー返し (input()でブロックしない)")


def test_invalid_depth_falls_back():
    monkey = {
        "_translate_to_english_query": lambda topic, llm_fn=None: "en",
        "_search_topic": lambda q, max_results=10: SOURCES[:max_results],
        "_load_user_context": lambda: "",
        "_multi_lens_analysis": lambda t, s, c, d: "# R",
        "_generate_happiness_advice": lambda t, r, c: "## H",
        "_save_to_obsidian": lambda t, r: "F",
        "_notify_discord": lambda t, r, f: None,
    }
    originals = _patch(monkey)
    try:
        result = fs.run_future_search("topic", depth="bogus")
    finally:
        _restore(originals)
    # 不正 depth は standard に丸められ、10ソース
    assert "standard" in result and "10ソース" in result, result
    print("✅ 不正 depth は standard にフォールバック")


if __name__ == "__main__":
    test_is_mostly_ascii()
    test_translate_english_passthrough()
    test_translate_japanese_uses_llm()
    test_translate_failure_fallback()
    test_build_multi_lens_prompt_contents()
    test_build_prompt_without_user_context()
    test_depth_source_counts()
    test_run_pipeline_orchestration()
    test_run_pipeline_uncensored_and_no_callback()
    test_empty_topic_rejected()
    test_invalid_depth_falls_back()
    print("\n🎉 ALL FUTURE SEARCH PIPELINE TESTS PASSED")
