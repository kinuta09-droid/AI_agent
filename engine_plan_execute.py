# -*- coding: utf-8 -*-
"""
Antigravity Plan-and-Execute Engine
agent_hello.py から抽出された実行エンジン。
Plan-and-Execute, ReAct Loop, Self-Improvement Protocol を提供する。
"""

import json
import datetime
import time
import traceback

from lib.lib_shared import (
    client, MODEL_NAME, MAX_STEPS,
    safe_json_loads, load_json, save_json,
    LEARNING_DB_PATH, PERFORMANCE_DASHBOARD_PATH, VAULT_ROOT,
)


# ---------------------------------------------------------
# Curator Wrapper (lazy import to avoid circular dependency)
# ---------------------------------------------------------

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
# Plan-and-Execute Engine
# ---------------------------------------------------------

def _get_tools_config():
    """ツール一覧と実行マップを返す（ReAct / Plan-and-Execute 共用）。
    
    agent_hello のツール関数を lazy import して循環依存を回避。
    """
    import agent_hello as _ah

    # Calendar/Task tools (lazy import)
    try:
        from tools.tool_calendar import (
            tool_get_calendar, tool_add_event,
            tool_create_task, tool_get_tasks,
            tool_sync_to_daily_note,
        )
        _has_calendar = True
    except ImportError:
        _has_calendar = False

    # crypto tools from tool_crypto
    from tools.tool_crypto import tool_analyze_crypto, tool_scan_crypto_market, tool_audit_predictions

    # briefing tool
    from tools.tool_briefing_manager import tool_generate_briefing

    # gmail tool
    from tools.tool_gmail_manager import tool_gmail_check

    config_tools = [
        {"name": "tool_web_search", "usage": "tool_web_search(query)", "description": "Web検索 (Tavily)"},
        {"name": "tool_vault_search", "usage": "tool_vault_search(keywords)", "description": "Vault内を検索"},
        {"name": "tool_create_note", "usage": "tool_create_note(filename, content, summary)", "description": "ファイル作成"},
        {"name": "tool_read_note", "usage": "tool_read_note(filename)", "description": "ファイル読込"},
        {"name": "tool_update_note", "usage": "tool_update_note(filename, append_text)", "description": "追記"},
        {"name": "tool_get_youtube_transcript", "usage": "tool_get_youtube_transcript(url='URL')", "description": "YouTube文字起こし取得"},
        {"name": "tool_merge_weekly_reports", "usage": "tool_merge_weekly_reports()", "description": "週次レポートのマージとアーカイブ"},
        {"name": "tool_fetch_daily_news", "usage": "tool_fetch_daily_news()", "description": "ニュース収集・編集・保存"},
        {"name": "tool_fetch_url", "usage": "tool_fetch_url(url)", "description": "Webページのテキスト取得"},
        {"name": "tool_analyze_crypto", "usage": "tool_analyze_crypto(symbol='BTC/USDT')", "description": "仮想通貨クオンツ分析"},
        {"name": "tool_scan_crypto_market", "usage": "tool_scan_crypto_market()", "description": "主要20銘柄一括スキャン"},
        {"name": "tool_audit_predictions", "usage": "tool_audit_predictions()", "description": "予測ログの答え合わせ"},
        {"name": "tool_curate_topics", "usage": "tool_curate_topics()", "description": "ナレッジトピック提案"},
        {"name": "tool_generate_briefing", "usage": "tool_generate_briefing()", "description": "デイリーブリーフィング生成（Crypto + Vault更新の3項目要約）"},
        {"name": "tool_gmail_check", "usage": "tool_gmail_check(hours=12)", "description": "直近の重要メール要約（AIトリアージ + Obsidian保存）"},
    ]
    tools_map = {
        "tool_web_search": _ah.tool_web_search,
        "tool_vault_search": _ah.tool_vault_search,
        "tool_create_note": _ah.tool_create_note,
        "tool_read_note": _ah.tool_read_note,
        "tool_update_note": _ah.tool_update_note,
        "tool_get_youtube_transcript": _ah.tool_get_youtube_transcript,
        "tool_merge_weekly_reports": _ah.tool_merge_weekly_reports,
        "tool_fetch_daily_news": _ah.tool_fetch_daily_news,
        "tool_fetch_url": _ah.tool_fetch_url,
        "tool_analyze_crypto": tool_analyze_crypto,
        "tool_scan_crypto_market": tool_scan_crypto_market,
        "tool_audit_predictions": tool_audit_predictions,
        "tool_curate_topics": _tool_curate_wrapper,
        "tool_generate_briefing": tool_generate_briefing,
        "tool_gmail_check": tool_gmail_check,
    }

    # Calendar/Task tools を動的に追加
    if _has_calendar:
        config_tools.extend([
            {"name": "tool_get_calendar", "usage": "tool_get_calendar(date='today', days=1)", "description": "Google Calendarから予定を取得"},
            {"name": "tool_add_event", "usage": "tool_add_event(title, start_time, end_time='', description='')", "description": "Google Calendarに予定を追加"},
            {"name": "tool_create_task", "usage": "tool_create_task(title, due_date='', priority=1, project='Inbox')", "description": "Todoistにタスクを追加"},
            {"name": "tool_get_tasks", "usage": "tool_get_tasks(filter='today')", "description": "Todoistからタスク一覧を取得"},
            {"name": "tool_sync_to_daily_note", "usage": "tool_sync_to_daily_note()", "description": "予定とタスクをデイリーノートに同期"},
        ])
        tools_map.update({
            "tool_get_calendar": tool_get_calendar,
            "tool_add_event": tool_add_event,
            "tool_create_task": tool_create_task,
            "tool_get_tasks": tool_get_tasks,
            "tool_sync_to_daily_note": tool_sync_to_daily_note,
        })

    return config_tools, tools_map


def generate_plan(user_query: str) -> dict:
    """ユーザーのクエリから実行計画を生成する。

    Returns:
        {
            "plan": [
                {"step": 1, "task": "...", "tool": "...", "args_hint": "..."},
                ...
            ],
            "expected_outcome": "最終的に何が達成されるか"
        }
    """
    config_tools, _ = _get_tools_config()
    tools_desc = "\n".join(
        f"- {t['name']}: {t['description']}  使い方: {t['usage']}"
        for t in config_tools
    )

    # Self-Improvement: 過去の学習からヒントを注入
    learning_hint = get_enhanced_prompt(user_query)
    learning_block = f"\n\n{learning_hint}" if learning_hint else ""

    today_str = datetime.datetime.now().strftime("%Y年%m月%d日")

    prompt = f"""あなたはタスクプランナーです。
ユーザーの要求を達成するための実行計画を作成してください。

【現在の日付】: {today_str}
※ 「最新」「今年」などの表現は、上記の日付を基準にしてください。

【要求】: {user_query}

【利用可能なツール】:
{tools_desc}

【ルール】
- 各ステップは1つのツール呼び出しに対応させる
- 情報収集 → 加工・保存 → 完了報告 の順序を守る
- ニュース収集には tool_fetch_daily_news のみを使い、tool_create_note での二重作成は禁止
- レポート・要約・分析の作成を求められた場合、必ず tool_create_note で30_Mainに保存する
- 必要なステップだけに絞り、冗長な計画にしない（最大6ステップ）

【制約の抽出と遵守】（重要！）
ユーザーの要求から以下のパターンで「禁止事項」を抽出し、厳守してください：
- 「〜せずに」「〜なしで」「〜を使わないで」「〜は禁止」「〜を避けて」
  → 該当するツールや行動を計画から除外する
- 例: 「Web検索せずに」→ tool_web_search, tool_fetch_daily_news, tool_fetch_url を使用禁止
- 例: 「ファイルを作成せずに」→ tool_create_note を使用禁止

【制約がある場合の代替案】（必ず代替手段を探す！空のplanは禁止！）
- Web検索禁止で「ニュース」を求められた場合:
  Step 1: tool_vault_search(keywords="news ニュース") で過去のニュースファイルを探す
  Step 2: tool_read_note でそのファイルを読んで内容を返す
- ファイル作成禁止の場合: 結果をテキストで返すだけにする
- **重要**: 代替手段がある限り、必ず1つ以上のステップを持つ計画を作成すること
- 「実行不可」と返すのは本当にどうしようもない場合のみ

【ツール選択の指針】
- **外部情報が必要な場合** (最新ニュース、一般知識、技術情報など): tool_web_search を使う
- **ユーザーの過去のメモや文脈が必要な場合**: tool_vault_search を使う
- **両方が必要な場合**: まず tool_web_search で外部情報を取得し、その後 tool_vault_search で関連する過去ノートを探す
- **URLが与えられた場合**: tool_fetch_url でページ内容を取得する

{learning_block}
【出力フォーマット (JSONのみ)】:
{{
    "plan": [
        {{"step": 1, "task": "タスクの説明", "tool": "使用するツール名", "args_hint": "引数のヒント"}},
        {{"step": 2, "task": "タスクの説明", "tool": "使用するツール名", "args_hint": "引数のヒント"}}
    ],
    "expected_outcome": "最終的に何が達成されるか"
}}"""

    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return safe_json_loads(response.text)


def execute_plan(plan: list, step_callback=None) -> dict:
    """計画を順番に実行する。

    Args:
        plan: generate_plan() が返した plan リスト
        step_callback: Discord等へのリアルタイム通知用コールバック

    Returns:
        {"success": bool, "results": [...], "final_output": "..."}
    """
    _, tools_map = _get_tools_config()
    results = []
    accumulated_context = []

    for item in plan:
        step_num = item.get("step", len(results) + 1)
        task = item.get("task", "")
        tool_name = item.get("tool", "")
        args_hint = item.get("args_hint", "")

        print(f"[P&E] ▶️ Step {step_num}: {task} (tool={tool_name})")

        if step_callback:
            step_callback({
                "phase": "execute",
                "step": step_num,
                "total": len(plan),
                "task": task,
                "tool": tool_name,
                "status": "running",
            })

        # ツールの引数を Gemini に解決させる
        args = _resolve_tool_args(tool_name, args_hint, task, accumulated_context)

        if tool_name not in tools_map:
            observation = f"Error: Unknown tool '{tool_name}'"
            success = False
        else:
            try:
                observation = tools_map[tool_name](**args)
                success = True
            except Exception as e:
                observation = f"Error: {e}"
                success = False

        print(f"  {'✅' if success else '❌'} {str(observation)[:200]}...")

        step_result = {
            "step": step_num,
            "task": task,
            "tool": tool_name,
            "args": args,
            "observation": observation,
            "success": success,
        }
        results.append(step_result)
        accumulated_context.append(step_result)

        if step_callback:
            step_callback({
                "phase": "execute",
                "step": step_num,
                "total": len(plan),
                "task": task,
                "tool": tool_name,
                "status": "done" if success else "error",
                "observation": str(observation)[:500],
            })

        if not success:
            print(f"[P&E] ⚠️ Step {step_num} failed, passing to Critic...")
            break

    all_ok = all(r["success"] for r in results)
    return {
        "success": all_ok,
        "results": results,
        "final_output": _summarize_results(results) if all_ok else "",
    }


def _resolve_tool_args(tool_name: str, args_hint: str, task: str,
                        context: list) -> dict:
    """Gemini を使ってツールの引数を具体化する。"""
    if not args_hint and tool_name in (
        "tool_merge_weekly_reports", "tool_fetch_daily_news",
        "tool_scan_crypto_market", "tool_audit_predictions",
        "tool_curate_topics",
    ):
        return {}

    # ツールのシグネチャ（必須引数情報）を取得
    config_tools, _ = _get_tools_config()
    tool_usage = ""
    for t in config_tools:
        if t["name"] == tool_name:
            tool_usage = t["usage"]
            break

    ctx_summary = ""
    if context:
        ctx_summary = "\n".join(
            f"Step {c['step']}: {c['task']} → {str(c['observation'])[:500]}"
            for c in context[-3:]
        )

    today_str = datetime.datetime.now().strftime("%Y年%m月%d日")

    prompt = f"""以下のツール呼び出しに必要な引数をJSON辞書で出力してください。

【現在の日付】: {today_str}
※ 「最新」「今年」などは{today_str[:4]}年を指します。検索クエリにも正しい年を使用してください。

ツール: {tool_name}
シグネチャ: {tool_usage}
タスク: {task}
引数ヒント: {args_hint}

これまでの実行結果（引数の値はここから取得してください）:
{ctx_summary}

【重要な注意事項】
- シグネチャにある引数は必ず全て含めてください
- tool_web_search の場合:
  - query: 「最新」と言われたら {today_str[:4]}年 を含めてください（例: "AI 最新トレンド {today_str[:4]}"）
- tool_create_note の場合:
  - filename: レポートのタイトル（日本語OK）
  - content: レポートの本文（これまでの実行結果から内容を生成してください）
  - summary: 1-2行の要約
- 引数の値は「これまでの実行結果」の observation から抽出・生成してください
- 引数が不要なら空の辞書 {{}} を返してください

JSONのみ出力（説明不要）:"""

    for attempt in range(3):
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            args = safe_json_loads(response.text)
            # 必須引数チェック (tool_create_note)
            if tool_name == "tool_create_note":
                if "filename" not in args or "content" not in args:
                    print(f"[P&E] ⚠️ Missing required args for tool_create_note, retry {attempt+1}/3")
                    continue
            return args
        except Exception as e:
            print(f"[P&E] ⚠️ _resolve_tool_args error: {e}, retry {attempt+1}/3")
    # 最終手段: デフォルト引数
    if tool_name == "tool_create_note":
        return {"filename": "Report.md", "content": "生成に失敗しました", "summary": ""}
    return {}


def _summarize_results(results: list) -> str:
    """実行結果リストから最終サマリーを生成する。"""
    parts = []
    for r in results:
        obs = str(r.get("observation", ""))
        if obs.startswith("Success:") or obs.startswith("✅"):
            parts.append(f"- Step {r['step']}: {obs[:200]}")
        else:
            parts.append(f"- Step {r['step']} ({r['task']}): 完了")
    return "\n".join(parts)


def critique_results(user_query: str, plan: list, results: list) -> dict:
    """実行結果を評価し、必要なら修正計画を返す。

    Returns:
        {
            "status": "ok" | "needs_revision" | "failed",
            "feedback": "フィードバック内容",
            "revised_plan": [...] (needs_revision の場合のみ)
        }
    """
    config_tools, _ = _get_tools_config()
    valid_tools = [t["name"] for t in config_tools]
    valid_tools_str = ", ".join(valid_tools)

    results_summary = json.dumps(
        [{"step": r["step"], "task": r["task"], "success": r["success"],
          "observation": str(r["observation"])[:800]}
         for r in results],
        ensure_ascii=False, indent=2,
    )

    plan_summary = "\n".join(
        f"  {p.get('step', i+1)}. {p.get('task', '')} [{p.get('tool', '')}]"
        for i, p in enumerate(plan)
    )

    prompt = f"""あなたはタスク評価者（Critic）です。
実行結果を評価し、ユーザーの目標が達成されたか判定してください。

【ユーザーの要求】: {user_query}

【元の計画】:
{plan_summary}

【実行結果】:
{results_summary}

【利用可能なツール（これ以外は使用禁止）】:
{valid_tools_str}

【ユーザー制約の遵守】（revised_plan作成時に厳守！）
- ユーザーの要求に「〜せずに」「〜なしで」「〜を使わないで」「〜禁止」があれば、該当ツールは使用禁止
- 例: 「Web検索せずに」→ revised_plan に tool_web_search, tool_fetch_daily_news, tool_fetch_url を含めてはいけない
- 代替手段がある場合は必ずその代替案で revised_plan を作成すること

【評価基準】
1. 全ステップが成功しているか
2. ユーザーの目標が達成されたか（情報が十分か、ファイルが作成されたか等）
3. **コンテンツ品質チェック**（最重要）:
   - 「調べて」「まとめて」系の要求 → observationに具体的な情報（手順、数字、固有名詞）が含まれているか？
   - 一般論や概要だけで具体性がない場合 → needs_revision（「具体的な手順/データを追加検索せよ」と指示）
   - 例: 「ストレッチの方法を調べて」→ 具体的なストレッチ名・やり方・回数がなければ不十分
4. 不足があれば、追加で必要なステップを提案
- **重要**: revised_planで提案するツールは上記の利用可能なツールのみ使用可能
- **重要**: revised_planはユーザーの制約を違反してはいけない

【revision判断の基準】（時間効率を考慮！）
- 全ステップ成功 + 具体的な情報が含まれている → status=ok（revisionしない）
- ステップ失敗あり → needs_revision
- 全ステップ成功だが情報が薄い → needs_revision（ただし追加ステップは最大2つまで）
- 同じツールの再実行は避け、別の検索クエリや別ツールで補完すること

【出力フォーマット (JSONのみ)】:
{{
    "status": "ok" or "needs_revision" or "failed",
    "feedback": "評価コメント",
    "revised_plan": [
        {{"step": 1, "task": "追加タスク", "tool": "ツール名", "args_hint": "引数ヒント"}}
    ]
}}

- status=ok: 目標達成、追加作業不要
- status=needs_revision: 部分的に成功、追加ステップが必要（revised_planに記載、最大2ステップ）
- status=failed: 根本的に失敗、リトライ不可"""

    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return safe_json_loads(response.text)
    except Exception as e:
        return {"status": "ok", "feedback": f"Critic evaluation failed ({e}), accepting results."}


MAX_REVISIONS = 1  # 品質向上のため1回に制限（2回は時間浪費の原因）


def run_plan_and_execute(user_query: str, step_callback=None) -> str:
    """Plan-and-Execute パターンのメインエントリーポイント。

    1. generate_plan() で計画を作成
    2. execute_plan() で実行
    3. critique_results() で評価
    4. 必要なら再計画・再実行（最大 MAX_REVISIONS 回）
    5. log_execution() で学習DBに記録
    6. 最終結果を返す
    """
    t_start = time.time()
    print(f"[P&E] 🧠 Planning for: {user_query[:80]}...")

    if step_callback:
        step_callback({"phase": "plan", "status": "planning"})

    try:
        plan_data = generate_plan(user_query)
    except Exception as e:
        print(f"[P&E] ❌ Plan generation failed: {e}")
        return f"計画生成に失敗しました: {e}"

    plan = plan_data.get("plan", [])
    original_plan = list(plan)
    expected = plan_data.get("expected_outcome", "")

    if not plan:
        return "計画を生成できませんでした。"

    print(f"[P&E] 📋 Plan ({len(plan)} steps): {expected}")
    for p in plan:
        print(f"  {p['step']}. {p['task']} [{p['tool']}]")

    if step_callback:
        step_callback({
            "phase": "plan",
            "status": "ready",
            "plan": plan,
            "expected_outcome": expected,
        })

    all_results = []
    last_critique = {}

    for revision in range(MAX_REVISIONS + 1):
        if revision > 0:
            print(f"[P&E] 🔄 Revision {revision}/{MAX_REVISIONS}")
            if step_callback:
                step_callback({"phase": "revision", "attempt": revision})

        exec_result = execute_plan(plan, step_callback)
        all_results.extend(exec_result["results"])

        if step_callback:
            step_callback({"phase": "critique", "status": "evaluating"})

        critique = critique_results(user_query, plan, exec_result["results"])
        last_critique = critique
        print(f"[P&E] 🔍 Critic: {critique.get('status')} - {critique.get('feedback', '')[:100]}")

        if critique["status"] == "ok":
            duration = time.time() - t_start
            try:
                log_execution(user_query, original_plan, all_results, critique, duration)
            except Exception as e:
                print(f"[P&E] ⚠️ Failed to log execution: {e}")
            return _generate_final_summary(user_query, all_results)
        elif critique["status"] == "needs_revision":
            revised = critique.get("revised_plan", [])
            if revised:
                plan = revised
                print(f"[P&E] 📝 Revised plan ({len(plan)} steps)")
            else:
                duration = time.time() - t_start
                try:
                    log_execution(user_query, original_plan, all_results, critique, duration)
                except Exception as e:
                    print(f"[P&E] ⚠️ Failed to log execution: {e}")
                return _generate_final_summary(user_query, all_results)
        else:
            duration = time.time() - t_start
            try:
                log_execution(user_query, original_plan, all_results, critique, duration)
            except Exception as e:
                print(f"[P&E] ⚠️ Failed to log execution: {e}")
            return f"タスク失敗: {critique.get('feedback', 'Unknown error')}"

    duration = time.time() - t_start
    try:
        log_execution(user_query, original_plan, all_results, last_critique, duration)
    except Exception as e:
        print(f"[P&E] ⚠️ Failed to log execution: {e}")
    return _generate_final_summary(user_query, all_results)


def _generate_final_summary(user_query: str, results: list) -> str:
    """Gemini を使って実行結果から最終サマリーを生成する。"""
    results_text = "\n".join(
        f"Step {r['step']}: {r['task']} → {'✅' if r['success'] else '❌'} {str(r['observation'])[:500]}"
        for r in results
    )

    prompt = f"""以下のタスク実行結果を、ユーザーへの簡潔な報告にまとめてください。

【ユーザーの要求】: {user_query}

【実行結果】:
{results_text}

【ルール】
- 作成したファイルがあれば [[ファイル名]] 形式でリンクを記載
- 簡潔に（5行以内）
- 実行サマリーのみ（詳細はファイルに保存済み）"""

    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return response.text.strip()
    except Exception:
        return _summarize_results(results)


# ---------------------------------------------------------
# Self-Improvement Protocol
# ---------------------------------------------------------

def _load_learning_db() -> dict:
    """学習DBを読み込む。存在しなければ空のDBを返す。"""
    return load_json(LEARNING_DB_PATH, default={"executions": []})


def _save_learning_db(db: dict) -> None:
    """学習DBを保存する。"""
    save_json(LEARNING_DB_PATH, db)


def log_execution(query: str, plan: list, results: list,
                  critique: dict, duration_seconds: float) -> None:
    """実行結果を学習DBに記録する。"""
    db = _load_learning_db()

    all_success = all(r.get("success", False) for r in results)
    any_success = any(r.get("success", False) for r in results)
    if all_success:
        outcome = "success"
    elif any_success:
        outcome = "partial"
    else:
        outcome = "failed"

    failure_reason = None
    failed_step = None
    for r in results:
        if not r.get("success", False):
            failed_step = r.get("step")
            obs = str(r.get("observation", ""))
            failure_reason = obs[:300]
            break

    tools_used = list(dict.fromkeys(r.get("tool", "") for r in results))

    now = datetime.datetime.now()
    exec_id = f"exec_{now.strftime('%Y%m%d')}_{len(db['executions']) + 1:03d}"

    entry = {
        "id": exec_id,
        "timestamp": now.isoformat(timespec="seconds"),
        "query": query[:200],
        "plan_steps": len(plan),
        "outcome": outcome,
        "failure_reason": failure_reason,
        "failed_step": failed_step,
        "steps_count": len(results),
        "duration_seconds": round(duration_seconds, 1),
        "tools_used": tools_used,
        "critique_status": critique.get("status", "unknown"),
    }

    db["executions"].append(entry)
    _save_learning_db(db)
    print(f"[P&E] 📝 Logged execution: {exec_id} ({outcome})")


def extract_patterns() -> dict:
    """学習DBから成功/失敗パターンとツール信頼性を抽出する。

    Returns:
        {
            "total": int,
            "success_rate": float,
            "avg_duration": float,
            "tool_reliability": {tool_name: rate, ...},
            "failure_patterns": ["..."],
            "insights": "Gemini による自然言語サマリー"
        }
    """
    db = _load_learning_db()
    execs = db.get("executions", [])

    if len(execs) < 5:
        return {"total": len(execs), "message": "データ不足（5件未満）"}

    total = len(execs)
    success_count = sum(1 for e in execs if e["outcome"] == "success")
    success_rate = success_count / total if total > 0 else 0
    avg_duration = sum(e.get("duration_seconds", 0) for e in execs) / total

    # ツール別信頼性
    tool_calls = {}
    tool_success = {}
    for e in execs:
        is_ok = e["outcome"] in ("success", "partial")
        for t in e.get("tools_used", []):
            if not t:
                continue
            tool_calls[t] = tool_calls.get(t, 0) + 1
            if is_ok:
                tool_success[t] = tool_success.get(t, 0) + 1

    tool_reliability = {}
    for t, count in tool_calls.items():
        tool_reliability[t] = round(tool_success.get(t, 0) / count, 2)

    # 失敗パターン
    failure_patterns = []
    for e in execs:
        if e["outcome"] == "failed" and e.get("failure_reason"):
            failure_patterns.append(
                f"Query: {e['query'][:60]} → {e['failure_reason'][:100]}"
            )

    # Gemini でパターン要約（データが十分な場合のみ）
    insights = ""
    if len(execs) >= 10:
        try:
            summary_data = json.dumps({
                "total": total,
                "success_rate": round(success_rate, 2),
                "avg_duration": round(avg_duration, 1),
                "tool_reliability": tool_reliability,
                "recent_failures": failure_patterns[-5:],
            }, ensure_ascii=False)

            prompt = f"""以下のAIエージェント実行統計を分析し、改善ポイントを3つ箇条書きで述べてください。

{summary_data}

簡潔に（各項目1-2文）:"""

            resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            insights = resp.text.strip()
        except Exception:
            insights = ""

    return {
        "total": total,
        "success_rate": round(success_rate, 2),
        "avg_duration": round(avg_duration, 1),
        "tool_reliability": tool_reliability,
        "failure_patterns": failure_patterns[-10:],
        "insights": insights,
    }


def get_enhanced_prompt(user_query: str) -> str:
    """過去の学習から関連情報を取得し、Planner プロンプトに追記する文字列を返す。"""
    parts = []
    
    # 顧客評価・自己改善ループからの学習パッチ注入
    try:
        from engine_self_improve import get_approved_patch_hints
        patches = get_approved_patch_hints()
        if patches:
            parts.append(patches)
    except Exception as e:
        print(f"[SelfImprove] Error loading patches: {e}")

    db = _load_learning_db()
    execs = db.get("executions", [])

    if len(execs) >= 5:
        # 直近の失敗パターンを収集
        recent_failures = []
        for e in reversed(execs[-20:]):
            if e["outcome"] == "failed" and e.get("failure_reason"):
                recent_failures.append(
                    f"- クエリ「{e['query'][:50]}」→ {e['failure_reason'][:100]}"
                )
            if len(recent_failures) >= 3:
                break

        # ツール信頼性の低いものを警告
        tool_calls = {}
        tool_success = {}
        for e in execs:
            is_ok = e["outcome"] in ("success", "partial")
            for t in e.get("tools_used", []):
                if not t:
                    continue
                tool_calls[t] = tool_calls.get(t, 0) + 1
                if is_ok:
                    tool_success[t] = tool_success.get(t, 0) + 1

        low_reliability = []
        for t, count in tool_calls.items():
            if count >= 3:
                rate = tool_success.get(t, 0) / count
                if rate < 0.8:
                    low_reliability.append(f"- {t}: 成功率 {rate:.0%}（要注意）")

        # 平均ステップ数
        avg_steps = sum(e.get("plan_steps", 0) for e in execs) / len(execs)

        if recent_failures:
            parts.append("【過去の失敗から学んだ注意事項】")
            parts.extend(recent_failures)
        if low_reliability:
            parts.append("【信頼性の低いツール】")
            parts.extend(low_reliability)
        if avg_steps:
            parts.append(f"【参考】平均的なタスクは {avg_steps:.1f} ステップで完了している")

    return "\n".join(parts) if parts else ""


def generate_performance_dashboard() -> str:
    """Performance Dashboard を Obsidian に出力する。"""
    patterns = extract_patterns()

    if patterns.get("message"):
        return f"データ不足: {patterns['message']}"

    total = patterns["total"]
    rate = patterns["success_rate"]
    avg_dur = patterns["avg_duration"]
    tool_rel = patterns.get("tool_reliability", {})
    failures = patterns.get("failure_patterns", [])
    insights = patterns.get("insights", "")

    # ツール信頼性ランキング
    sorted_tools = sorted(tool_rel.items(), key=lambda x: x[1], reverse=True)
    tool_lines = "\n".join(
        f"| {t} | {r:.0%} |" for t, r in sorted_tools
    )

    # 失敗パターン
    fail_lines = "\n".join(f"- {f}" for f in failures[-5:]) if failures else "なし"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    dashboard = f"""# Agent Performance Dashboard
> Updated: {now}

## 全体統計
| 指標 | 値 |
|---|---|
| 総実行回数 | {total} |
| 成功率 | {rate:.0%} |
| 平均実行時間 | {avg_dur:.1f}秒 |

## ツール信頼性
| ツール | 成功率 |
|---|---|
{tool_lines}

## 最近の失敗パターン
{fail_lines}

## AI分析
{insights if insights else '_データ蓄積中（10件以上で分析開始）_'}
"""

    with open(PERFORMANCE_DASHBOARD_PATH, 'w', encoding='utf-8') as f:
        f.write(dashboard)

    return f"Dashboard updated: {PERFORMANCE_DASHBOARD_PATH}"


# ---------------------------------------------------------
# ReAct Engine (Legacy — 後方互換性のために残す)
# ---------------------------------------------------------

def run_react_loop(user_query, initial_history=None, step_callback=None):
    """
    Think-Act-Observe Loop
    """
    print(f"[ReAct] 🚀 Starting for: {user_query[:50]}...")

    # _get_tools_config() から一元取得（calendar含む全ツール）
    _config_tools, tools_map = _get_tools_config()

    history = initial_history if initial_history else []
    
    for step_i in range(MAX_STEPS):
        print(f"\n[ReAct] Step {step_i + 1}/{MAX_STEPS}")
        
        prompt = f"""
        あなたは自律型AIアシスタントです。
        【ゴール】: {user_query}
        
        現在までの経緯（履歴）:
        {json.dumps(history, ensure_ascii=False, indent=2)}
        
        利用可能なツール:
        1. tool_web_search(query): Web検索 (Tavilyを使用)
        2. tool_vault_search(keywords): Vault内を検索
        3. tool_create_note(filename, content, summary): ファイル作成
        4. tool_read_note(filename): ファイル読込
        5. tool_update_note(filename, append_text): 追記
        6. tool_get_youtube_transcript(url): YouTube動画の文字起こしを取得
        7. tool_fetch_url(url): 指定URLのWebページの内容（テキスト）を取得。ニュース記事やブログなどを読む時に使う。
        8. tool_merge_weekly_reports(): 週次レポートのマージとアーカイブ（ファイルが増えすぎた時に提案/実行）
        9. tool_fetch_daily_news(days): Web検索+RSS巡回でニュースを収集し、Geminiで編集・要約して02_Newsに保存、デイリーノートにリンク追記（daysはオプション、デフォルト1）
           **重要**: このツールはニュースの収集・編集・保存・デイリーノート連携を全て完結する。実行後に tool_create_note で別のニュースファイルを作成してはならない。
        10. tool_analyze_crypto(symbol): Strategy 2.0 総合クオンツ分析。ファンダメンタルズ分析(Why検索) + 落ちるナイフ回避(ADX+DM) + RSIダイバージェンス検知 + SMC流動性回収 + ケリー基準 + DCA計画を統合。レポートを03_Cryptoに保存。symbol例: "BTC/USDT", "ETH/USDT"
        11. tool_scan_crypto_market(): 主要20銘柄を一括スキャンし、RSI・出来高変化・F&Gから注目すべき通貨をランキング。結果を03_Crypto/Market_Scan_{{date}}.mdに保存。「どの通貨が今アツい？」「おすすめは？」等の質問に使う。
        12. tool_audit_predictions(): 過去の予測ログ(trade_log.json)を検証。Binanceから実績価格を取得し、TP到達→WIN / SL到達→LOSE を判定。LOSEの場合はTavilyで敗因ニュースを検索。結果を03_Crypto/Audit_Report/Audit_Report_{{date}}.mdに保存。「予測の答え合わせ」「過去の成績は？」等の質問に使う。
        13. tool_curate_topics(): ユーザーの興味に基づいてナレッジトピックを3つ提案する（The Curator）。「おすすめのトピック」「何か面白い話題ない？」等の質問に使う。
        14. tool_get_calendar(date='today', days=1): Google Calendarから予定を取得。「今日の予定」「明日のスケジュール」等に使う。
        15. tool_add_event(title, start_time, end_time='', description=''): Google Calendarに予定を追加。「会議を追加して」等に使う。
        16. tool_create_task(title, due_date='', priority=1, project='Inbox'): Todoistにタスクを追加。「リマインドして」「タスクを作って」等に使う。priority: 1=最高〜4=最低。
        17. tool_get_tasks(filter='today'): Todoistからタスク一覧を取得。filter: 'today','overdue','week','all'。
        18. tool_sync_to_daily_note(): Google Calendar + Todoistの内容をデイリーノートに同期する。

        【指示】
        - 「次に何をするべきか」を考え、JSONで出力してください。
        - 必要な情報が揃ったら、"action": "finish" としてください。
        
        ## 出力およびツール利用の厳格なルール
        1. **レポート作成の強制**:
           ユーザーから「レポート」「要約」「分析」「まとめ」等の作成を求められた場合、その内容を直接最終回答（Agent_output）に記述してはならない。
           必ず `tool_create_note` を使用し、`30_Main` フォルダ内に新しい Markdown ファイルとして保存すること。
           **例外**: ニュース関連の依頼では `tool_fetch_daily_news` のみを使い、`tool_create_note` は使わない（二重作成防止）。

        2. **Agent_output（最終回答）の役割制限**:
            `Agent_output.md` に書き出す内容は、以下の「実行サマリー」のみに限定すること。
            - 実行したステップの簡潔な説明
            - 作成したレポートのファイル名（例：[[レポート名.md]] を作成しました）
            - エラーが発生した場合の報告

        3. **作業フローの順序**:
            ① 情報収集（YouTube/Vault/Web）
            ② `tool_create_note` による詳細レポートの作成（30_Main）
            ③ 作成成功を確認後、`finish` を呼び出しサマリーのみを報告

        - **重要**: ノートを作成する際、中身がプレースホルダー（例: [調査結果]）であってはいけません。必ず履歴にある「Observation」の内容を使ってください。
        - **重要**: 検索結果が不十分なら、まだノート作成せず、追加検索を行ってください。
        - **最重要 (パーソナライズ)**: Vault内検索（`tool_vault_search`）で関連する過去のメモが見つかった場合は、**必ずその内容を最終成果物に統合してください。**
          - 「あなたの過去のメモ [[ファイル名]] によると〜」のように引用し、ユーザーの文脈に合わせた内容にしてください。
          - 関連ノートのセクションには、見つかったノートへのリンク `[[ファイル名]]` を追記してください。

        出力フォーマット (JSONのみ):
        {{
            "thought": "思考プロセス...",
            "action": "ツール名" or "finish",
            "args": {{ "引数名": "値" ... }}  (actionがfinishの場合は "summary": "完了報告（作成したファイル名を含む）" をargsに入れる)
        }}
        """

        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            text = response.text
            
            # JSON Parse
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]
            
            data = safe_json_loads(text)
            
            thought = data.get("thought", "")
            action = data.get("action", "")
            args = data.get("args", {})
            
            print(f"  💭 Thought: {thought}")
            print(f"  🎬 Action: {action}")

            # Callback for Discord/UI
            if step_callback:
                step_callback({
                    "step": step_i + 1,
                    "thought": thought,
                    "action": action,
                    "args": args,
                    "observation": "Running..."
                })
            
            if action == "finish":
                print(f"[ReAct] ✅ Finished!")
                return args.get("summary", "Done")
            
            if action in tools_map:
                try:
                    observation = tools_map[action](**args)
                except Exception as e:
                    observation = f"Error: {e}"
            else:
                observation = f"Error: Unknown tool '{action}'"
            
            print(f"  👀 Observation: {str(observation)[:200]}...")
            
            history.append({
                "step": step_i + 1,
                "thought": thought,
                "action": action,
                "args": args,
                "observation": observation # Context for next turn
            })

            # Update Callback with Observation
            if step_callback:
                step_callback({
                    "step": step_i + 1,
                    "thought": thought,
                    "action": action,
                    "args": args,
                    "observation": str(observation)[:500] + "..." # Truncate for display
                })
            
        except Exception as e:
            print(f"[ReAct] ❌ Error in loop: {e}")
            traceback.print_exc()
            time.sleep(2)
            
    return "Max steps reached."
