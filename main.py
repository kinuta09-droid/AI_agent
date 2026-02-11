# -*- coding: utf-8 -*-
"""
Antigravity Universal Launcher
自然言語で指示するだけで、適切なツールを自動選択・実行する統合エントリーポイント。
"""

import os
import sys
import importlib

sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------
# Gemini Client (lightweight init)
# ---------------------------------------------------------

from google import genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash")
client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------

TOOLS = [
    {
        "name": "daily_news",
        "description": "最新ニュースを収集・要約してObsidianに保存する",
        "function": "agent_hello.tool_fetch_daily_news",
        "needs_arg": False,
    },
    {
        "name": "crypto_analysis",
        "description": "仮想通貨の詳細クオンツ分析を行う（ADX、SMC、Kelly基準）",
        "function": "agent_hello.tool_analyze_crypto",
        "needs_arg": True,
        "arg_name": "symbol",
        "arg_prompt": "通貨ペアを入力 (例: BTC/USDT): ",
        "default_arg": "BTC/USDT",
    },
    {
        "name": "market_scan",
        "description": "主要20銘柄の仮想通貨一括スキャン",
        "function": "agent_hello.tool_scan_crypto_market",
        "needs_arg": False,
    },
    {
        "name": "curate",
        "description": "興味に基づいてトピックを提案・調査し、ナレッジノートを作成する",
        "function": "tool_curator.run_curation",
        "needs_arg": False,
    },
    {
        "name": "setup",
        "description": "初回セットアップ: 興味のあるトピックを一括調査してナレッジベースを構築",
        "function": "tool_curator.run_setup",
        "needs_arg": False,
    },
    {
        "name": "topic_search",
        "description": "指定したキーワードで調査し、ナレッジノートを作成する",
        "function": "tool_curator.search_and_save_topic",
        "needs_arg": True,
        "arg_name": "topic",
        "arg_prompt": "調べたいキーワード: ",
    },
    {
        "name": "agent",
        "description": "AIエージェントに自由な指示を与えて自律実行させる（ReActループ）",
        "function": "agent_hello.run_react_loop",
        "needs_arg": True,
        "arg_name": "goal",
        "arg_prompt": "エージェントへの指示: ",
    },
    {
        "name": "prediction_audit",
        "description": "過去の仮想通貨予測を実際の価格と照合して精度を検証する",
        "function": "agent_hello.tool_audit_predictions",
        "needs_arg": False,
    },
    {
        "name": "discord_bot",
        "description": "Discord Botを起動する（常駐プロセス、Ctrl+Cで停止）",
        "function": "bot_discord.run_bot",
        "needs_arg": False,
        "long_running": True,
    },
    {
        "name": "scheduler",
        "description": "スケジューラーを起動する（定時タスクの自動実行、Ctrl+Cで停止）",
        "function": "agent_scheduler.run_scheduler",
        "needs_arg": False,
        "long_running": True,
    },
    {
        "name": "n8n_backup",
        "description": "n8nワークフローをバックアップする",
        "function": "n8n_patrol.fetch_all_workflows",
        "needs_arg": False,
    },
    {
        "name": "lora_dataset",
        "description": "LoRA学習用データセットを作成する（ComfyUI WD14自動タグ付け + フォルダ整理）",
        "function": "tool_lora_factory.run_lora_factory",
        "needs_arg": False,
    },
]


# ---------------------------------------------------------
# Lazy Import Helper
# ---------------------------------------------------------

_module_cache = {}

def _get_function(func_path: str):
    """'module.func_name' → 実際の関数オブジェクトを返す（lazy import）"""
    module_name, func_name = func_path.rsplit(".", 1)
    if module_name not in _module_cache:
        _module_cache[module_name] = importlib.import_module(module_name)
    return getattr(_module_cache[module_name], func_name)


# ---------------------------------------------------------
# AI Router
# ---------------------------------------------------------

def route_to_tool(user_input: str) -> dict:
    """Gemini でユーザー入力を解析し、最適なツールと引数を返す。"""
    tool_list = "\n".join(
        f"- {t['name']}: {t['description']}" for t in TOOLS
    )

    prompt = f"""あなたはタスクルーターです。ユーザーの入力に最も適したツールを1つ選んでください。

【利用可能なツール】
{tool_list}

【ユーザー入力】
{user_input}

以下のJSON形式のみで出力してください:
{{"tool": "ツール名", "arg": "引数（不要ならnull）"}}

ルール:
- tool は上記リストの name と完全一致させること
- 引数が必要なツール(crypto_analysis, topic_search, agent)は、ユーザーの入力から引数を抽出すること
- crypto_analysis の arg はシンボル（例: "BTC/USDT"）。通貨名だけなら "/USDT" を補完
- topic_search の arg は調査キーワード
- agent の arg はエージェントへの指示文
- どのツールにも該当しない場合は agent を選び、arg にユーザー入力をそのまま入れること"""

    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        text = response.text.strip()
        # JSONを抽出
        import json
        import re
        # ```json ... ``` を除去
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        return json.loads(text)
    except Exception as e:
        print(f"  [Router] ルーティング失敗: {e}")
        return {"tool": "agent", "arg": user_input}


# ---------------------------------------------------------
# Tool Executor
# ---------------------------------------------------------

def execute_tool(tool_name: str, arg: str = None) -> str:
    """指定されたツールを実行し、結果を返す。"""
    tool = next((t for t in TOOLS if t["name"] == tool_name), None)
    if not tool:
        print(f"  ⚠️ 不明なツール: {tool_name}")
        print("  → agent にフォールバックします")
        tool = next(t for t in TOOLS if t["name"] == "agent")
        arg = arg or tool_name

    # 長時間実行の警告
    if tool.get("long_running"):
        print(f"  ⚠️ {tool['name']} は常駐プロセスです。Ctrl+C で停止できます。")

    # 引数が必要なのに未指定の場合、対話で取得
    if tool.get("needs_arg") and not arg:
        default = tool.get("default_arg", "")
        prompt_text = tool.get("arg_prompt", f"{tool.get('arg_name', '引数')}: ")
        if default:
            prompt_text = prompt_text.rstrip(": ") + f" (デフォルト: {default}): "
        user_arg = input(f"  {prompt_text}").strip()
        arg = user_arg if user_arg else default

    # 関数を取得して実行
    try:
        func = _get_function(tool["function"])
        if tool.get("needs_arg") and arg:
            result = func(arg)
        else:
            result = func()
        return result if result else "(完了)"
    except KeyboardInterrupt:
        return "\n⏹️ 中断しました。"
    except Exception as e:
        return f"❌ エラー: {e}"


# ---------------------------------------------------------
# Main Loop
# ---------------------------------------------------------

def main():
    print()
    print("=" * 50)
    print("  🚀 Antigravity Launcher")
    print("  何でも聞いてください（exit で終了）")
    print("=" * 50)
    print()

    # ツール一覧を表示
    print("📋 利用可能なツール:")
    for t in TOOLS:
        flag = " [常駐]" if t.get("long_running") else ""
        print(f"  • {t['name']}: {t['description']}{flag}")
    print()

    while True:
        try:
            user_input = input("あなた: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 お疲れさまでした！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q", "終了"):
            print("👋 お疲れさまでした！")
            break

        # AI ルーティング
        print("  🤔 ルーティング中...")
        route = route_to_tool(user_input)
        tool_name = route.get("tool", "agent")
        arg = route.get("arg")

        # null/None の正規化
        if arg in (None, "null", "None", ""):
            arg = None

        print(f"  🎯 → [{tool_name}]", end="")
        if arg:
            print(f" (arg: {arg})", end="")
        print()

        # 実行
        result = execute_tool(tool_name, arg)

        # 結果表示
        print()
        print("-" * 40)
        if result and len(result) > 2000:
            print(result[:2000])
            print(f"\n  ... ({len(result)} 文字中 2000文字を表示)")
        else:
            print(result)
        print("-" * 40)
        print()


if __name__ == "__main__":
    main()
