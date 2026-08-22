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
    parallel_safe : True なら fan-out (Phase C1) で他ステップと同時に実行してよい

parallel_safe の判定基準 (opt-in・既定は逐次):
    「他の parallel_safe ツールと同時に走らせても、副作用が壊れないこと」。
    読み取り専用であることが実質の条件で、以下のどれかに当たるツールは付けない。
      - ファイル/DB へ書き込む (Obsidian 保存・trade_log 追記・token.json 更新)
      - 対話を要求しうる (OAuth の run_local_server)
      - 外部の状態を変える (メール送信・カレンダー登録)
    **付け忘れは安全側に倒れる** (逐次のまま) のに対し、誤って付けると
    データ競合が黙って起きるので、既定を「付いていない」にしてある。
    集合は tests/test_tool_registry.py がピン留めしているので、
    足すときはテストも一緒に直すことになる (黙って増やせない)。
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


def _tool_paper_radar_wrapper(field: str = "", top_k: int = 5, days: int = 7) -> str:
    """分野の注目論文を指標 (HF upvote / 被引用速度 / 新しさ) で判別する。"""
    try:
        from tools.tool_paper_radar import run_paper_radar
        return run_paper_radar(field=field, top_k=int(top_k), days=int(days))
    except Exception as e:
        return f"[paper_radar error] {e}"


def _tool_paper_digest_wrapper(index: int = 1, arxiv_id: str = "", mode: str = "sns") -> str:
    """注目論文を docling でキャプチャ→要約→SNS/note下書きまで作る。"""
    try:
        from tools.tool_paper_digest import run_paper_digest
        return run_paper_digest(index=int(index), arxiv_id=arxiv_id, mode=mode)
    except Exception as e:
        return f"[paper_digest error] {e}"


def _tool_notebooklm_wrapper(topic: str, sources, question: str, genre_override: str = "") -> str:
    """NotebookLMにソース(URL/テキスト)を読み込ませ、根拠(引用元)付きで質問に回答させる。

    sources は usage 上は改行区切り文字列だが、Plannerが args_json でJSON配列
    (Python list) を渡してくることがある(2026-08-17実測: 'list' object has no
    attribute 'split' で失敗)。「複数可」という説明文からLLMがJSON配列を自然に
    連想するのは避けがたいため、文字列/リストどちらで来ても動くようにする。
    """
    try:
        from tools.tool_notebooklm import run_notebooklm_research
        if isinstance(sources, list):
            source_list = [str(s).strip() for s in sources if str(s).strip()]
        else:
            source_list = [s.strip() for s in str(sources).split("\n") if s.strip()]
        return run_notebooklm_research(topic, source_list, question, genre_override=genre_override)
    except Exception as e:
        return f"[notebooklm error] {e}"


def _tool_notebooklm_research_wrapper(
    topic: str, query: str, genre_override: str = "", source: str = "web", mode: str = "fast"
) -> str:
    """NotebookLMのDeepSearch相当: クエリからソースを自動発見しノートブックへ根拠付きでインポートする。"""
    try:
        from tools.tool_notebooklm import research_and_import
        return research_and_import(topic, query, genre_override=genre_override, source=source, mode=mode)
    except Exception as e:
        return f"[notebooklm_research error] {e}"


def _tool_notebooklm_studio_create_wrapper(
    topic: str, artifact_type: str, genre_override: str = "", focus_prompt: str = "", description: str = ""
) -> str:
    """NotebookLMのStudio機能で音声/動画/マインドマップ/インフォグラフィック/クイズ/フラッシュカード等を生成開始する。"""
    try:
        from tools.tool_notebooklm import create_studio_artifact
        return create_studio_artifact(
            topic, artifact_type, genre_override=genre_override,
            focus_prompt=focus_prompt, description=description,
        )
    except Exception as e:
        return f"[notebooklm_studio_create error] {e}"


def _tool_notebooklm_studio_status_wrapper(
    topic: str, genre_override: str = "", artifact_id: str = ""
) -> str:
    """create_studio_artifactで開始したStudio成果物の生成状況/完成物URLを確認する。"""
    try:
        from tools.tool_notebooklm import check_studio_status
        return check_studio_status(topic, genre_override=genre_override, artifact_id=artifact_id)
    except Exception as e:
        return f"[notebooklm_studio_status error] {e}"


def _tool_read_document_wrapper(path_or_url: str, ocr: bool = False) -> str:
    """任意のPDF/Office文書 (URL/ローカルパス) をdoclingでMarkdown化して返す (ノートは保存しない)。"""
    try:
        from tools.tool_document import tool_read_document
        return tool_read_document(path_or_url, ocr=bool(ocr))
    except Exception as e:
        return f"[read_document error] {e}"


def _tool_import_document_wrapper(path_or_url: str, folder: str = "30_Main", mode: str = "none") -> str:
    """任意のPDF/Office文書を変換→Gemini要約→Vaultにノート保存する(元ファイルへの参照付き)。"""
    try:
        from tools.tool_document import tool_import_document
        return tool_import_document(path_or_url, folder=folder, mode=mode)
    except Exception as e:
        return f"[import_document error] {e}"


def _tool_shorts_from_video_wrapper(
    video_path: str,
    target_seconds: int = 30,
    topic_hint: str = "",
    bgm_path: str = "",
    vertical_mode: str = "pad",
) -> str:
    """既存の長尺動画ファイルからSNSショート動画を自動生成する。"""
    try:
        from tools.tool_shorts_editor import tool_shorts_from_video
        return tool_shorts_from_video(
            video_path,
            target_seconds=int(target_seconds),
            topic_hint=topic_hint or None,
            bgm_path=bgm_path or None,
            vertical_mode=vertical_mode,
        )
    except Exception as e:
        return f"[shorts error] {e}"


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


def _tool_web_design_wrapper(action: str, pattern: str = "A", plugins: str = "trail",
                              image_url: str = "", prompt: str = "", title: str = "",
                              out: str = "") -> str:
    """AI駆動型ウェブデザインSOP(.claude/skills/ai-web-design-workflow)の自動化パーツ。
    action: 'phase2' | 'phase4_video' | 'scaffold_webgl'"""
    try:
        from tools.tool_web_design import run_phase2, run_phase4_video, scaffold_webgl
        if action == "phase2":
            return run_phase2()
        if action == "phase4_video":
            if not image_url:
                return "Error: image_url is required for phase4_video"
            return run_phase4_video(image_url, prompt=prompt)
        if action == "scaffold_webgl":
            return scaffold_webgl(pattern=pattern, plugins=plugins, title=title or "Your Name", out=out)
        return f"Error: unknown action '{action}' (expected phase2 | phase4_video | scaffold_webgl)"
    except Exception as e:
        return f"[tool_web_design error] {e}"


# ---------------------------------------------------------
# Registry Entries (並び順 = プロンプト表示順)
# ---------------------------------------------------------

TOOL_ENTRIES = [
    {"name": "tool_web_search", "usage": "tool_web_search(query, recency_days=None)", "description": "Web検索 (Tavily)。一般的な調べ物はこちらを使う（無料枠が大きい）",
     "module": "tools.tool_web", "parallel_safe": True},
    {"name": "tool_web_search_grounded", "usage": "tool_web_search_grounded(query)",
     "description": "Web検索 (Google Search Grounding、精度優先)。「今日/本日/急に」など直近の出来事で、"
                     "tool_web_search (Tavily) では狙った一次情報を安定して拾えない/答えの精度が特に重要な質問にだけ使う。"
                     "日次呼び出し数に別枠の上限があるため乱用しない",
     "module": "tools.tool_web"},
    # SQLite は _connect() が呼び出し毎に開いて finally で閉じる (WAL・読み取りのみ)。
    # Ollama を最大3回叩くが、そちらは lib.concurrency.ollama_slot が直列化する。
    {"name": "tool_vault_hybrid_search", "usage": "tool_vault_hybrid_search(query, top_k=5)", "description": "Vault内検索 (キーワード+意味検索のハイブリッド)。過去のメモ・ノート・文脈を探す場合は常にこれを使う",
     "module": "tools.tool_vault_hybrid", "parallel_safe": True},
    {"name": "tool_create_note", "usage": "tool_create_note(filename, content, summary)", "description": "ファイル作成",
     "module": "tools.tool_obsidian"},
    {"name": "tool_read_note", "usage": "tool_read_note(filename)", "description": "ファイル読込",
     "module": "tools.tool_obsidian", "parallel_safe": True},
    # description は Planner の唯一のツール選択根拠。"追記" の2文字だった頃は
    # 「既存のデイリーノートに追記して」が tool_sync_to_daily_note (説明文に
    # 「デイリーノート」を含む) に語彙一致で奪われていた (eval T09・2026-07-28)。
    {"name": "tool_update_note", "usage": "tool_update_note(filename, append_text)",
     "description": "既存ノートの末尾にテキストを追記する（新規ファイルは作らない）。デイリーノート(YYYY-MM-DD.md)への任意の文章の追記もこれを使う",
     "module": "tools.tool_obsidian"},
    {"name": "tool_get_youtube_transcript", "usage": "tool_get_youtube_transcript(url='URL')", "description": "YouTube文字起こし取得",
     "module": "tools.tool_web", "parallel_safe": True},
    {"name": "tool_fetch_daily_news", "usage": "tool_fetch_daily_news()", "description": "ニュース収集・編集・保存",
     "module": "tools.tool_web"},
    {"name": "tool_fetch_url", "usage": "tool_fetch_url(url)", "description": "Webページのテキスト取得",
     "module": "tools.tool_web", "parallel_safe": True},
    {"name": "tool_analyze_crypto", "usage": "tool_analyze_crypto(symbol='BTC/USDT')", "description": "仮想通貨クオンツ分析",
     "module": "tools.tool_crypto"},
    {"name": "tool_scan_crypto_market", "usage": "tool_scan_crypto_market()", "description": "主要20銘柄一括スキャン",
     "module": "tools.tool_crypto"},
    {"name": "tool_audit_predictions", "usage": "tool_audit_predictions()", "description": "予測ログの答え合わせ",
     "module": "tools.tool_crypto"},
    {"name": "tool_crypto_stats", "usage": "tool_crypto_stats()", "description": "仮想通貨の予測成績ダッシュボード（signal_type別の勝率・期待値R・OPEN中の予測一覧）",
     "module": "tools.tool_crypto_stats"},
    {"name": "tool_curate_topics", "usage": "tool_curate_topics()", "description": "ナレッジトピック提案",
     "func": _tool_curate_wrapper},
    {"name": "tool_generate_briefing", "usage": "tool_generate_briefing()", "description": "デイリーブリーフィング生成（Crypto + Vault更新の3項目要約）",
     "module": "tools.tool_briefing_manager"},
    {"name": "tool_check_vault_insights", "usage": "tool_check_vault_insights()",
     "description": "30_Mainの最近変更されたノートを検索し、既存ノートとの関係(補完/矛盾/発展)をGeminiが判定してDiscordに提案する。notableな関係が無ければ何も送らない",
     "module": "tools.tool_vault_watcher", "attr": "check_vault_insights"},
    {"name": "tool_gmail_check", "usage": "tool_gmail_check(hours=12)", "description": "直近の重要メール要約（AIトリアージ + Obsidian保存）",
     "module": "tools.tool_gmail_manager", "attr": "get_gmail_summary"},
    {"name": "tool_fetch_context", "usage": "tool_fetch_context(filenames_comma_separated)", "description": "複数ファイルの文書をまとめて読み込む(カンマ区切り)",
     "module": "tools.tool_obsidian", "parallel_safe": True},
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
    # 論文レーダー/ダイジェスト。radar は Gemini を呼ばない (arXiv/HF/S2 の公開API
    # のみ・課金ゼロ) が、Obsidian と候補JSONへ書くので parallel_safe は付けない。
    {"name": "tool_paper_radar", "usage": "tool_paper_radar(field='ai_agent', top_k=5, days=7)",
     "description": "指定分野の注目論文を自動判別: arXiv新着 × HuggingFace upvote × 被引用速度 × 新しさでスコアリングしてObsidianに保存",
     "func": _tool_paper_radar_wrapper},
    {"name": "tool_notebooklm", "usage": "tool_notebooklm(topic, sources, question, genre_override='')",
     "description": "NotebookLMにソース(URL改行区切り複数可/生テキスト)を読み込ませ、"
                     "根拠(引用元)付きで質問に回答させる。捏造しない設計のため、"
                     "『このソースに書かれている内容だけ』を厳密に知りたいときに使う。"
                     "topicは正確なジャンル名でなくてよい(ざっくりした内容の説明でよく、"
                     "既存のノートブックと自動でジャンル分けされる)。新しいジャンルが必要と"
                     "判定された場合は何も作成せず確認メッセージを返すので、その内容をユーザーに"
                     "伝えて選んでもらい、選ばれたジャンル名を genre_override に入れて再度呼び出すこと。"
                     "処理に数十秒〜数分かかる",
     "func": _tool_notebooklm_wrapper},
    {"name": "tool_notebooklm_research",
     "usage": "tool_notebooklm_research(topic, query, genre_override='', source='web', mode='fast')",
     "description": "NotebookLMのDeepSearch相当: クエリからWeb(またはGoogle Drive)を自動検索し、"
                     "見つかったソースをノートブックへ根拠付きで自動インポートする。ユーザーが"
                     "「リサーチして」と依頼した場合はtool_web_searchより優先してこちらを使う"
                     "(「調べて」だけの通常の依頼はtool_web_search)。"
                     "mode='fast'は約30秒、'deep'は約5分かかりWeb限定。"
                     "topic/genre_override は tool_notebooklm と同じジャンル確認ゲートに従う",
     "func": _tool_notebooklm_research_wrapper},
    {"name": "tool_notebooklm_studio_create",
     "usage": "tool_notebooklm_studio_create(topic, artifact_type, genre_override='', focus_prompt='', description='')",
     "description": "NotebookLMのノートブック内ソースから音声概要/動画概要/マインドマップ/"
                     "インフォグラフィック/スライド/レポート/クイズ/フラッシュカード(Anki相当)/"
                     "データテーブルを生成開始する (artifact_type: audio/video/mind_map/infographic/"
                     "slide_deck/report/quiz/flashcards/data_table)。data_table指定時はdescription必須。"
                     "生成に数分〜数十分かかるため起動のみ行い待機しない。戻り値のジャンル名とartifact_idを"
                     "tool_notebooklm_studio_status に渡して進捗を確認すること",
     "func": _tool_notebooklm_studio_create_wrapper},
    {"name": "tool_notebooklm_studio_status",
     "usage": "tool_notebooklm_studio_status(topic, genre_override='', artifact_id='')",
     "description": "tool_notebooklm_studio_create で開始した生成物の進捗と完成物URLを確認する。"
                     "genre_override には studio_create が返した確定ジャンル名をそのまま渡すこと",
     "func": _tool_notebooklm_studio_status_wrapper},
    {"name": "tool_paper_digest", "usage": "tool_paper_digest(index=1, arxiv_id='', mode='sns')",
     "description": "注目論文の本文をdoclingでキャプチャし要約→Obsidian保存→X投稿スレッド/note記事の下書きを生成 (mode: sns/note/both/none)",
     "func": _tool_paper_digest_wrapper},
    {"name": "tool_read_document", "usage": "tool_read_document(path_or_url, ocr=False)",
     "description": "PDF/Office文書 (URLまたはローカルパス) をdoclingでMarkdown化して返す。ノートには保存しない読み取り専用（本文を読んで判断したいときに使う）",
     "func": _tool_read_document_wrapper},
    {"name": "tool_import_document", "usage": "tool_import_document(path_or_url, folder='30_Main', mode='none')",
     "description": "PDF/Office文書 (URLまたはローカルパス) をdoclingで変換しGeminiで要約してObsidianにノート保存する（元ファイルへの参照付き）。"
                     "mode指定でX投稿/note記事の下書きも生成 (mode: sns/note/both/none)",
     "func": _tool_import_document_wrapper},
    # Calendar/Task (optional — 認証情報や依存パッケージ欠落時はスキップ)
    # tool_get_calendar に parallel_safe を付けない理由 (取得系なのに例外扱い):
    #   主経路は gws CLI だが、失敗時のフォールバック _tool_get_calendar_legacy →
    #   _get_calendar_service() が (a) token.json を書き換え、
    #   (b) 期限切れ時に InstalledAppFlow.run_local_server() でブラウザ認証を待つ。
    #   ワーカースレッドでこれが起きると、無言でウェーブ全体が止まる。
    #   「読み取りツールだから安全」は呼び出し口の名前で判断した結果であって、
    #   実装を読んだ結果ではない。
    {"name": "tool_get_calendar", "usage": "tool_get_calendar(date='today', days=1)", "description": "Google Calendarから予定を取得",
     "module": "tools.tool_calendar", "optional": True},
    {"name": "tool_add_event", "usage": "tool_add_event(title, start_time, end_time='', description='')", "description": "Google Calendarに予定を追加",
     "module": "tools.tool_calendar", "optional": True},
    {"name": "tool_create_task", "usage": "tool_create_task(title, due_date='', priority=1, project='Inbox')", "description": "Todoistにタスクを追加",
     "module": "tools.tool_calendar", "optional": True},
    # Todoist 側は認証がAPIキー1本 (トークンファイルの更新が無い) ので取得は純粋。
    {"name": "tool_get_tasks", "usage": "tool_get_tasks(filter='today')", "description": "Todoistからタスク一覧を取得",
     "module": "tools.tool_calendar", "optional": True, "parallel_safe": True},
    # 起動条件 (予定/タスクの同期) と非対象 (任意テキスト) の両方を書く。
    # tool_update_note 側に「デイリーノート」を書いた分、こちらの縄張りを
    # 明示しないと逆に同期要求を奪われる。
    {"name": "tool_sync_to_daily_note", "usage": "tool_sync_to_daily_note()",
     "description": "Google Calendarの予定とTodoistのタスクをデイリーノートの所定欄へ流し込む専用ツール。任意の文章の追記には使えない（それはtool_update_note）",
     "module": "tools.tool_calendar", "optional": True},
    # 動画ファイルをVault (30_Main/Assets/Shorts) に書き込むため parallel_safe は付けない。
    {"name": "tool_shorts_from_video", "usage": "tool_shorts_from_video(video_path, target_seconds=30, topic_hint='', bgm_path='', vertical_mode='pad')",
     "description": "既存の長尺動画ファイルから縦型9:16のSNSショート動画を自動編集する"
                     "(ハイライト区間の自動選定→トリミング→字幕焼き込み→BGM合成)。"
                     "動画ファイルのパスが必須。テキスト/画像からの新規動画生成には使えない",
     "func": _tool_shorts_from_video_wrapper},
    {"name": "tool_web_design",
     "usage": "tool_web_design(action='scaffold_webgl', pattern='A', plugins='trail,bloom', image_url='', prompt='', title='', out='')",
     "description": "AI駆動型ウェブデザインSOPの自動化パーツ(Phase2背景/UI画像生成、Phase4動画ループ生成、Option BのThree.js雛形生成)",
     "func": _tool_web_design_wrapper},
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


def get_tool_descriptions() -> dict:
    """{ツール名: 説明} を返す。**ツールモジュールを import しない**。

    get_tools_config() と分けてある理由:
      あちらは callable を解決するので tools/* を全部 import する (Tavily や
      genai のクライアント初期化を伴う)。説明文だけが欲しい呼び出し側
      (lib/activity_corpus の行動ログ文書) にその代償を払わせない。

    用途: ツールの実行実績を検索可能な文書にするとき、`tool_gmail_check` という
    識別子だけではユーザーの語彙 (「メール」) と繋がらない。説明文を本文に
    入れて初めて意味検索で引ける。
    """
    return {e["name"]: e["description"] for e in TOOL_ENTRIES}


def get_parallel_safe_tools() -> frozenset:
    """fan-out で同時実行してよいツール名の集合を返す。**ツールを import しない**。

    get_tool_descriptions() と同じ理由でアクセサを分けている: 呼び出し元
    (engine_plan_execute の計画走査) が欲しいのは名前だけで、callable の解決
    (tools/* の全 import) の代償を払う理由がない。

    optional なツールも含む: ここは「並列にしてよいか」の宣言であって
    「今この環境で使えるか」ではない。使えるかどうかは get_tools_config() の
    tools_map に居るかで決まり、居なければそもそも計画に現れない。
    """
    return frozenset(e["name"] for e in TOOL_ENTRIES if e.get("parallel_safe"))


def get_tools_config():
    """(config_tools, tools_map) を返す。初回呼び出しで構築し、以降プロセス内キャッシュ。

    返却値はコピー (entry dict含む): 呼び出し側が変更してもキャッシュは汚れない。
    """
    global _CACHE
    if _CACHE is None:
        _CACHE = _build()
    config_tools, tools_map = _CACHE
    return [dict(t) for t in config_tools], dict(tools_map)
