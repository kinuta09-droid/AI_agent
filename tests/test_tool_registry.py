# -*- coding: utf-8 -*-
"""
Phase② ツールレジストリ統一のユニットテスト
(get_tools_config のキャッシュ / エイリアス解決 / engine 委譲 / run_react_loop 削除)

実行: py tests/test_tool_registry.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.tool_registry as registry
from lib.tool_registry import get_tools_config


def test_config_and_map_consistency():
    config_tools, tools_map = get_tools_config()
    assert config_tools, "config_tools must not be empty"
    for t in config_tools:
        assert set(t.keys()) == {"name", "usage", "description"}, f"unexpected keys: {t.keys()}"
    assert set(tools_map) == {t["name"] for t in config_tools}, "tools_map keys must match config names"
    assert all(callable(v) for v in tools_map.values()), "all tools_map values must be callable"
    print("✅ registry: config/map consistency & callables")


def test_cache_reuse_and_isolation():
    c1, m1 = get_tools_config()
    assert registry._CACHE is not None, "cache must be populated after first call"
    c1.append({"name": "bogus", "usage": "", "description": ""})
    m1["bogus"] = lambda: None
    c1[0]["usage"] = "tampered"
    c2, m2 = get_tools_config()
    assert [t["name"] for t in c2] == [t["name"] for t in c1][:-1], "caller mutation must not leak into cache"
    assert "bogus" not in m2, "caller mutation must not leak into cached map"
    assert c2[0]["usage"] != "tampered", "inner dict mutation must not poison the cache"
    print("✅ registry: cache reuse + returned containers isolated")


def test_gmail_alias_resolution():
    _, tools_map = get_tools_config()
    assert "tool_gmail_check" in tools_map, "alias tool_gmail_check must resolve (get_gmail_summary)"
    assert tools_map["tool_gmail_check"].__name__ == "get_gmail_summary"
    print("✅ registry: tool_gmail_check alias resolved")


def test_notebooklm_wrapper_accepts_list_or_string_sources():
    """sources に list (Plannerがargs_jsonでJSON配列を渡す場合) を渡してもクラッシュしないこと。

    2026-08-17実測: 「複数可」という説明文からPlannerがJSON配列を渡し、
    'list' object has no attribute 'split' で失敗した (sources.split("\n") が
    文字列専用だったため)。文字列/リストどちらで来ても動くことを固定する。
    """
    import tools.tool_notebooklm as nlm
    captured = {}

    def _fake_run(topic, sources, question, genre_override=""):
        captured["sources"] = sources
        return "ok"

    original = nlm.run_notebooklm_research
    nlm.run_notebooklm_research = _fake_run
    try:
        result = registry._tool_notebooklm_wrapper(
            "topic", ["https://a.example", "https://b.example"], "question"
        )
        assert result == "ok", result
        assert captured["sources"] == ["https://a.example", "https://b.example"], captured

        result2 = registry._tool_notebooklm_wrapper(
            "topic", "https://a.example\nhttps://b.example", "question"
        )
        assert result2 == "ok", result2
        assert captured["sources"] == ["https://a.example", "https://b.example"], captured
    finally:
        nlm.run_notebooklm_research = original
    print("✅ registry: tool_notebooklm wrapper accepts list or newline-string sources")


def test_prompt_ordering():
    config_tools, _ = get_tools_config()
    names = [t["name"] for t in config_tools]
    assert names[0] == "tool_web_search"
    # Google Search Grounding版 (2026-08-03): 精度優先の代替として tool_web_search の直後に追加
    assert names[1] == "tool_web_search_grounded"
    # Phase④: keyword/semantic 2エントリ → hybrid 1エントリに置換 (optionalなし=常に存在)
    assert names[2] == "tool_vault_hybrid_search"
    assert names[3] == "tool_create_note"
    print("✅ registry: prompt ordering preserved")


# ---------------------------------------------------------
# Phase C0: parallel_safe の集合をピン留めする
# ---------------------------------------------------------

# fan-out で同時実行してよいツール。**新しいツールに黙って付けられない**ように
# ここに書き写す形にしてある (付け忘れは逐次のまま＝安全、誤って付けると
# データ競合が黙って起きる、という非対称性があるため)。
# 足すときは lib/tool_registry.py の判定基準を読み、実装に書き込みが無いことを
# 確かめてからこの集合を更新すること。
EXPECTED_PARALLEL_SAFE = {
    "tool_web_search",
    "tool_vault_hybrid_search",
    "tool_read_note",
    "tool_get_youtube_transcript",
    "tool_fetch_url",
    "tool_fetch_context",
    "tool_get_tasks",
    "tool_jev",  # Jev への判断API問い合わせのみ。ファイル書き込み等の副作用なし
}

# 逆方向の固定: 書き込み/対話を伴うのに parallel_safe が付いたら落とす。
# 「集合が一致すること」だけだと、期待値の側を一緒に書き換えた変更が素通りする。
MUST_NOT_BE_PARALLEL_SAFE = {
    "tool_create_note", "tool_update_note", "tool_move_note", "tool_refactor_note",
    "tool_fetch_daily_news",
    "tool_analyze_crypto", "tool_scan_crypto_market", "tool_audit_predictions",
    "tool_crypto_stats",     # 03_Crypto にレポートを保存する
    "tool_gmail_check",      # Obsidian へ保存する
    "tool_get_calendar",     # token.json 更新 + OAuth の run_local_server
    "tool_curate_topics", "tool_xtrend", "tool_xtrend_prompt",
    "tool_future_search", "tool_browser_use", "tool_generate_briefing",
    # クリック/フォーム入力を伴うReActループ。1つのBrowserSession(Cookie/セッション
    # 状態を保持)をループ全体で使い回すため、他ステップと同時実行すると壊れる。
    "tool_browser_act",
    "tool_check_vault_insights",  # Discord通知+seen-cacheファイル書き込みの副作用があるため
    "tool_add_event", "tool_create_task",
    # Paper Radar は Gemini を呼ばない読み取り中心のツールだが、Obsidian の
    # レポートと logs/paper_radar_latest.json を書く。Digest はその JSON を
    # 読んでノート/下書きを書き、seen キャッシュも更新する (順序依存)。
    "tool_paper_radar", "tool_paper_digest",
    # 汎用ドキュメント変換 (Phase 2)。tool_read_document も一見読み取り専用だが
    # docling の変換キャッシュ (logs/paper_cache) へ書き込む。tool_import_document
    # はさらに Vault にノートを書く。
    "tool_read_document", "tool_import_document",
    # GROUNDING_COUNTER (grounding_daily_calls.json) への書き込みを伴う
    "tool_web_search_grounded",
    # 動画ファイルをVault (30_Main/Assets/Shorts) に書き込む
    "tool_shorts_from_video",
    # scaffold_webgl は out へファイル書き込み、phase2/phase4_video は
    # out_dir へ画像/動画をダウンロード書き込みする
    "tool_web_design",
    # NotebookLM 上にノートブック/ソースを作成し、処理完了まで待つ (外部状態の書き込み)
    "tool_notebooklm", "tool_notebooklm_research", "tool_notebooklm_studio_create",
    "tool_notebooklm_studio_status",
}


def test_parallel_safe_set_is_pinned():
    actual = set(registry.get_parallel_safe_tools())
    assert actual == EXPECTED_PARALLEL_SAFE, (
        f"parallel_safe の集合が変わった: 追加 {sorted(actual - EXPECTED_PARALLEL_SAFE)} / "
        f"削除 {sorted(EXPECTED_PARALLEL_SAFE - actual)}"
    )
    print("✅ registry: parallel_safe set pinned")


def test_write_tools_are_never_parallel_safe():
    actual = set(registry.get_parallel_safe_tools())
    violations = sorted(actual & MUST_NOT_BE_PARALLEL_SAFE)
    assert not violations, f"副作用のあるツールに parallel_safe が付いている: {violations}"
    print("✅ registry: side-effecting tools are not parallel_safe")


def test_parallel_safe_covers_every_entry():
    """許可/禁止のどちらにも載っていないツールが無いこと。

    これが無いと、新しいツールを足した人がどちらのリストも触らずに済んでしまい、
    「判断されていない」状態が黙って生まれる。
    """
    known = EXPECTED_PARALLEL_SAFE | MUST_NOT_BE_PARALLEL_SAFE
    all_names = {e["name"] for e in registry.TOOL_ENTRIES}
    unclassified = sorted(all_names - known)
    assert not unclassified, (
        f"並列可否が未分類のツール: {unclassified} — "
        f"tests/test_tool_registry.py のどちらかの集合に足すこと"
    )
    print("✅ registry: every tool is classified for fan-out")


def test_parallel_safe_accessor_does_not_import_tools():
    """get_parallel_safe_tools() が tools/* を import しないこと。

    get_tool_descriptions() と同じ契約。壊れると計画走査のたびに
    Tavily/genai のクライアント初期化を引きずる。
    """
    import ast
    src = os.path.join(_PROJECT_ROOT, "lib", "tool_registry.py")
    with open(src, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "get_parallel_safe_tools")
    calls = [n.func.id for n in ast.walk(func)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_build" not in calls and "get_tools_config" not in calls, (
        f"get_parallel_safe_tools が callable 解決を呼んでいる: {calls}"
    )
    print("✅ registry: parallel_safe accessor stays import-free")


def test_engine_delegation_and_react_removed():
    import engine_plan_execute as engine
    e_config, e_map = engine._get_tools_config()
    r_config, r_map = get_tools_config()
    assert [t["name"] for t in e_config] == [t["name"] for t in r_config], "engine must delegate to registry"
    assert set(e_map) == set(r_map)
    assert not hasattr(engine, "run_react_loop"), "run_react_loop must be deleted"
    print("✅ engine: delegates to registry, run_react_loop removed")


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
