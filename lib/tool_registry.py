# -*- coding: utf-8 -*-
"""
lib/tool_registry.py — エージェントツールの単一情報源 (Phase②)

engine_plan_execute._get_tools_config() はここへ委譲する。
TOOL_ENTRIES の並び順は planner プロンプトの表示順そのもの(並び替え禁止)。

エントリ形式:
    name        : ツール名 (planner が参照する識別子)
    usage       : 呼び出しシグネチャ (プロンプト表示用)
    description : 説明 (プロンプト表示用)
    module      : import 先モジュール (attr 省略時は name と同名の属性を取得)
    attr        : module 内の属性名 (エイリアス用、例: tool_gmail_check → get_gmail_summary)
    func        : module の代わりに callable を直接指定 (lazy wrapper 用)
    optional    : True なら ImportError 時に警告してスキップ (calendar / semantic search)
"""
import importlib


# ---------------------------------------------------------
# Lazy Wrappers (重量級ツールは呼び出し時まで import しない)
# ---------------------------------------------------------

def _tool_future_search_wrapper(topic: str = "") -> str:
    """英語圏マルチエージェント未来リサーチをReActツールとして公開する。"""
    try:
        from tools.tool_future_search import run_future_search
        return run_future_search(topic)
    except Exception as e:
        return f"[future_search error] {e}"


def _tool_xtrend_wrapper(query: str = "", min_likes: int = 500) -> str:
    """X(Twitter)のAIバズ投稿を収集し、技術スタック・難易度・ARMS親和性を分析する。"""
    try:
        from tools.tool_xtrend import run
        queries = [q.strip() for q in query.split(",")] if query else None
        features = run(queries=queries, min_likes=min_likes, no_save=False)
        if not features:
            return "[xtrend] 投稿が取得できませんでした"
        summary = "\n".join(
            f"- {f.get('feature_name','?')}: {f.get('summary','')} "
            f"(難易度{f.get('difficulty',0)}/5, ARMS親和{f.get('arms_affinity',0)}/5)"
            for f in features[:10]
        )
        return f"[xtrend] {len(features)}件の機能を分析しました。上位10件:\n{summary}"
    except Exception as e:
        return f"[xtrend error] {e}"


def _tool_browser_use_wrapper(url: str, task: str = "このページの主要な情報を要約して") -> str:
    """URLを開きGeminiでテキスト抽出する。JSレンダリング・動的コンテンツ対応。"""
    try:
        from tools.tool_browser_use import browse_and_extract
        return browse_and_extract(url, task)
    except Exception as e:
        return f"[browser_use error] {e}"


def _tool_xtrend_prompt_wrapper(feature_index: int = 1) -> str:
    """XTrendで分析した機能のClaude Code実装指示書を生成する。feature_indexは1始まり。"""
    try:
        from tools.tool_xtrend import _load_feature_by_index, generate_claude_prompt
        feature = _load_feature_by_index(int(feature_index))
        if not feature:
            return f"[xtrend_prompt] feature {feature_index} が見つかりません。先にtool_xtrend()を実行してください。"
        prompt_text = generate_claude_prompt(feature, save=True)
        name = feature.get("feature_name", "?")
        return f"[xtrend_prompt] 「{name}」の実装指示書を生成しました。\n\n{prompt_text}"
    except Exception as e:
        return f"[xtrend_prompt error] {e}"


def _tool_curate_wrapper() -> str:
    """The Curator のトピック提案をReActツールとして公開する。"""
    try:
        from tools.tool_curator import read_main_context, get_curation_suggestions
        context = read_main_context()
        suggestions = get_curation_suggestions(context, "normal")
        topics = suggestions.get("topics", [])
        if not topics:
            return "No suggestions generated."
        lines = [f"- {t['title']}: {t['description']}" for t in topics]
        return "Suggested topics:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------
# Registry Entries (並び順 = プロンプト表示順)
# ---------------------------------------------------------

TOOL_ENTRIES = [
    {"name": "tool_web_search", "usage": "tool_web_search(query)", "description": "Web検索 (Tavily)",
     "module": "tools.tool_web"},
    {"name": "tool_vault_hybrid_search", "usage": "tool_vault_hybrid_search(query, top_k=5)", "description": "Vault内検索 (キーワード+意味検索のハイブリッド)。過去のメモ・ノート・文脈を探す場合は常にこれを使う",
     "module": "tools.tool_vault_hybrid"},
    {"name": "tool_create_note", "usage": "tool_create_note(filename, content, summary)", "description": "ファイル作成",
     "module": "tools.tool_obsidian"},
    {"name": "tool_read_note", "usage": "tool_read_note(filename)", "description": "ファイル読込",
     "module": "tools.tool_obsidian"},
    {"name": "tool_update_note", "usage": "tool_update_note(filename, append_text)", "description": "追記",
     "module": "tools.tool_obsidian"},
    {"name": "tool_get_youtube_transcript", "usage": "tool_get_youtube_transcript(url='URL')", "description": "YouTube文字起こし取得",
     "module": "tools.tool_web"},
    {"name": "tool_merge_weekly_reports", "usage": "tool_merge_weekly_reports()", "description": "週次レポートのマージとアーカイブ",
     "module": "tools.tool_obsidian"},
    {"name": "tool_fetch_daily_news", "usage": "tool_fetch_daily_news()", "description": "ニュース収集・編集・保存",
     "module": "tools.tool_web"},
    {"name": "tool_fetch_url", "usage": "tool_fetch_url(url)", "description": "Webページのテキスト取得",
     "module": "tools.tool_web"},
    {"name": "tool_analyze_crypto", "usage": "tool_analyze_crypto(symbol='BTC/USDT')", "description": "仮想通貨クオンツ分析",
     "module": "tools.tool_crypto"},
    {"name": "tool_scan_crypto_market", "usage": "tool_scan_crypto_market()", "description": "主要20銘柄一括スキャン",
     "module": "tools.tool_crypto"},
    {"name": "tool_audit_predictions", "usage": "tool_audit_predictions()", "description": "予測ログの答え合わせ",
     "module": "tools.tool_crypto"},
    {"name": "tool_curate_topics", "usage": "tool_curate_topics()", "description": "ナレッジトピック提案",
     "func": _tool_curate_wrapper},
    {"name": "tool_generate_briefing", "usage": "tool_generate_briefing()", "description": "デイリーブリーフィング生成（Crypto + Vault更新の3項目要約）",
     "module": "tools.tool_briefing_manager"},
    {"name": "tool_gmail_check", "usage": "tool_gmail_check(hours=12)", "description": "直近の重要メール要約（AIトリアージ + Obsidian保存）",
     "module": "tools.tool_gmail_manager", "attr": "get_gmail_summary"},
    {"name": "tool_fetch_context", "usage": "tool_fetch_context(filenames_comma_separated)", "description": "複数ファイルの文書をまとめて読み込む(カンマ区切り)",
     "module": "tools.tool_obsidian"},
    {"name": "tool_move_note", "usage": "tool_move_note(old_filename, new_dir_or_filename)", "description": "ノートを別フォルダに移動・リネーム",
     "module": "tools.tool_obsidian"},
    {"name": "tool_refactor_note", "usage": "tool_refactor_note(filename, instructions)", "description": "指示に基づいて既存のノート全体の内容を再構築・上書きする",
     "module": "tools.tool_obsidian"},
    {"name": "tool_future_search", "usage": "tool_future_search(topic='AIエージェントの未来')", "description": "未来シナリオ分析: 日本語トピックを英訳→Tavily英語ソース収集→Gemini 10視点マルチレンズ推論→個人化された日本語レポートをObsidianに保存",
     "func": _tool_future_search_wrapper},
    {"name": "tool_browser_use", "usage": "tool_browser_use(url='https://...', task='このページのXXXを教えて')", "description": "ブラウザでURLを開きJS描画後のページテキストをGeminiで解析（動的サイト・SPA対応）",
     "func": _tool_browser_use_wrapper},
    {"name": "tool_xtrend", "usage": "tool_xtrend(query='AI agent,MCP', min_likes=500)", "description": "X(Twitter)のAIバズ投稿を収集し機能名/技術スタック/難易度/ARMS親和性を構造化分析してObsidianに保存",
     "func": _tool_xtrend_wrapper},
    {"name": "tool_xtrend_prompt", "usage": "tool_xtrend_prompt(feature_index=1)", "description": "XTrend分析済み機能(1始まりの番号)のClaude Code実装指示書をGeminiで生成してlogsに保存",
     "func": _tool_xtrend_prompt_wrapper},
    # Calendar/Task (optional — 認証情報や依存パッケージ欠落時はスキップ)
    {"name": "tool_get_calendar", "usage": "tool_get_calendar(date='today', days=1)", "description": "Google Calendarから予定を取得",
     "module": "tools.tool_calendar", "optional": True},
    {"name": "tool_add_event", "usage": "tool_add_event(title, start_time, end_time='', description='')", "description": "Google Calendarに予定を追加",
     "module": "tools.tool_calendar", "optional": True},
    {"name": "tool_create_task", "usage": "tool_create_task(title, due_date='', priority=1, project='Inbox')", "description": "Todoistにタスクを追加",
     "module": "tools.tool_calendar", "optional": True},
    {"name": "tool_get_tasks", "usage": "tool_get_tasks(filter='today')", "description": "Todoistからタスク一覧を取得",
     "module": "tools.tool_calendar", "optional": True},
    {"name": "tool_sync_to_daily_note", "usage": "tool_sync_to_daily_note()", "description": "予定とタスクをデイリーノートに同期",
     "module": "tools.tool_calendar", "optional": True},
]

_CACHE = None


def _resolve(entry):
    """エントリから実行callableを解決する。func直接指定 > module+attr import。"""
    if "func" in entry:
        return entry["func"]
    mod = importlib.import_module(entry["module"])
    return getattr(mod, entry.get("attr", entry["name"]))


def _build():
    config_tools, tools_map = [], {}
    for entry in TOOL_ENTRIES:
        try:
            func = _resolve(entry)
        except (ImportError, AttributeError) as e:
            # 元実装の from-import は属性欠落も ImportError だったが、
            # importlib + getattr では AttributeError になるため両方捕捉する
            if entry.get("optional"):
                print(f"[ToolRegistry] ⚠️ {entry['name']} unavailable ({e}) — skipped.")
                continue
            raise
        config_tools.append({
            "name": entry["name"],
            "usage": entry["usage"],
            "description": entry["description"],
        })
        tools_map[entry["name"]] = func
    return config_tools, tools_map


def get_tools_config():
    """(config_tools, tools_map) を返す。初回呼び出しで構築し、以降プロセス内キャッシュ。

    返却値はコピー (entry dict含む): 呼び出し側が変更してもキャッシュは汚れない。
    """
    global _CACHE
    if _CACHE is None:
        _CACHE = _build()
    config_tools, tools_map = _CACHE
    return [dict(t) for t in config_tools], dict(tools_map)
