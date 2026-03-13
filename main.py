# -*- coding: utf-8 -*-
import os
import sys
import importlib
import subprocess
import time
import signal
from dotenv import load_dotenv

# Windows console encoding fix
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv(r'C:\AI_agent\.env')

# ---------------------------------------------------------
# Gemini Client (lightweight init)
# ---------------------------------------------------------
from google import genai
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.0-flash")
client = genai.Client(api_key=GOOGLE_API_KEY)

# ---------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------
TOOLS = [
    {"name": "daily_news", "description": "最新ニュースを収集・要約してObsidianに保存する", "function": "tools.tool_web.tool_fetch_daily_news", "needs_arg": False},
    {"name": "crypto_analysis", "description": "仮想通貨の詳細クオンツ分析を行う", "function": "tools.tool_crypto.tool_analyze_crypto", "needs_arg": True, "arg_name": "symbol", "arg_prompt": "通貨ペア (例: BTC/USDT): ", "default_arg": "BTC/USDT"},
    {"name": "market_scan", "description": "主要20銘柄の仮想通貨一括スキャン", "function": "tools.tool_crypto.tool_scan_crypto_market", "needs_arg": False},
    {"name": "curate", "description": "興味に基づいてトピックを提案・調査し、ナレッジノートを作成する", "function": "tools.tool_curator.run_curation", "needs_arg": False},
    {"name": "setup", "description": "初回セットアップ: ナレッジベースを構築", "function": "tools.tool_curator.run_setup", "needs_arg": False},
    {"name": "topic_search", "description": "指定したキーワードで調査し、ナレッジノートを作成する", "function": "tools.tool_curator.search_and_save_topic", "needs_arg": True, "arg_name": "topic", "arg_prompt": "キーワード: "},
    {"name": "agent", "description": "AIエージェントに自律実行させる", "function": "engine_plan_execute.run_react_loop", "needs_arg": True, "arg_name": "goal", "arg_prompt": "指示: "},
    {"name": "prediction_audit", "description": "過去の仮想通貨予測を実際の価格と照合して精度を検証する", "function": "tools.tool_crypto.tool_audit_predictions", "needs_arg": False},
    {"name": "discord_bot", "description": "Discord Botを起動する", "function": "bot_discord.run_bot", "needs_arg": False, "long_running": True},
    {"name": "scheduler", "description": "スケジューラーを起動する", "function": "agent_scheduler.run_scheduler", "needs_arg": False, "long_running": True},
    {"name": "n8n_backup", "description": "n8nワークフローをバックアップする", "function": "n8n_patrol.fetch_all_workflows", "needs_arg": False},
    {"name": "lora_dataset", "description": "LoRA学習用データセットを作成する", "function": "tools.tool_lora_factory.run_lora_factory", "needs_arg": False},
    {"name": "analytics", "description": "エージェントのパフォーマンスやエラーログを分析する", "function": "tools.tool_analytics.run_analytics", "needs_arg": True, "arg_name": "question", "arg_prompt": "分析したいこと: "},
    {"name": "bitnet", "description": "ローカルAI (BitNet b1.58): オフライン要約・メッセージ下書き・ルーティングテスト", "function": "tools.tool_bitnet.run_bitnet_interactive", "needs_arg": False},
    {"name": "future_search", "description": "英語圏マルチエージェント未来リサーチ: 10体が議論→Geminiが日本語レポート生成", "function": "tools.tool_future_search.run_future_search", "needs_arg": True, "arg_name": "topic", "arg_prompt": "分析トピック (英語推奨): "},
]

# ---------------------------------------------------------
# Utils
# ---------------------------------------------------------
_module_cache = {}

def _get_function(func_path: str):
    module_name, func_name = func_path.rsplit(".", 1)
    if module_name not in _module_cache:
        _module_cache[module_name] = importlib.import_module(module_name)
    return getattr(_module_cache[module_name], func_name)

# ---------------------------------------------------------
# Parallel Launcher & Monitor
# ---------------------------------------------------------
def run_and_monitor():
    """Bot と Scheduler を別ウィンドウで起動し、監視（自動再起動）する"""
    print("\n" + "="*50)
    print("  🛰️ ARMS Parallel Launcher & Monitor")
    print("  - Starting Discord Bot & Scheduler...")
    print("  - Auto-restart enabled (5s delay)")
    print("  - Ctrl+C to stop all and return to menu")
    print("="*50 + "\n")

    python_exe = sys.executable
    processes = {}

    def start_process(name, script):
        print(f"  [Launcher] Starting {name}...")
        # 新しいコンソールウィンドウで起動 (Windows)
        return subprocess.Popen(
            [python_exe, script]
            # creationflags=subprocess.CREATE_NEW_CONSOLE
        )

    processes["discord_bot"] = start_process("Discord Bot", "bot_discord.py")
    processes["scheduler"] = start_process("Scheduler", "agent_scheduler.py")

    try:
        while True:
            time.sleep(5)
            for name, proc in processes.items():
                if proc.poll() is not None:
                    print(f"  [Monitor] ⚠️ {name} has stopped (code: {proc.returncode}). Restarting...")
                    script = "bot_discord.py" if name == "discord_bot" else "agent_scheduler.py"
                    processes[name] = start_process(name, script)
    except KeyboardInterrupt:
        print("\n  [Launcher] 🛑 Stopping all processes...")
        for proc in processes.values():
            proc.terminate()
        print("  [Launcher] All processes terminated.")

# ---------------------------------------------------------
# AI Router & Menu Functions
# ---------------------------------------------------------
def route_to_tool(user_input: str) -> dict:
    tool_list = "\n".join(f"- {t['name']}: {t['description']}" for t in TOOLS)
    prompt = f"あなたはタスクルーターです。ユーザーの入力に最も適したツールを選んでください。\n\n【ツール】\n{tool_list}\n\n【入力】\n{user_input}\n\nJSON形式: {{\"tool\": \"name\", \"arg\": \"val\"}}"
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        import json, re
        text = re.sub(r'```json\s*|\s*```', '', response.text.strip())
        return json.loads(text)
    except:
        return {"tool": "agent", "arg": user_input}

def execute_tool(tool_name: str, arg: str = None):
    tool = next((t for t in TOOLS if t["name"] == tool_name), None)
    if not tool:
        tool = next(t for t in TOOLS if t["name"] == "agent")
        arg = arg or tool_name

    if tool.get("needs_arg") and not arg:
        prompt = tool.get("arg_prompt", "Input: ")
        arg = input(f"  {prompt}").strip() or tool.get("default_arg")

    try:
        func = _get_function(tool["function"])
        return func(arg) if tool.get("needs_arg") else func()
    except Exception as e:
        return f"❌ Error: {e}"

def menu_loop():
    print("\n" + "="*50)
    print("  🚀 Antigravity AI Router")
    print("  0: Auto Launch (Bot + Scheduler)")
    for i, t in enumerate(TOOLS, 1):
        print(f"  {i}: {t['name']} - {t['description']}")
    print("="*50 + "\n")

    while True:
        try:
            cmd = input("あなた (または番号): ").strip()
            if cmd.lower() in ("exit", "quit", "q"): break
            
            if cmd == "0":
                run_and_monitor()
                continue

            if cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(TOOLS):
                    res = execute_tool(TOOLS[idx]["name"])
                    print(f"\n---\n{res}\n---")
                    continue

            route = route_to_tool(cmd)
            print(f"  🎯 → [{route['tool']}]")
            res = execute_tool(route["tool"], route.get("arg"))
            print(f"\n---\n{res}\n---")
        except (EOFError, KeyboardInterrupt):
            break

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--menu":
        menu_loop()
    else:
        # デフォルトは並列起動。Ctrl+Cでプログラム終了。
        try:
            run_and_monitor()
            print("\n  [System] All systems shut down cleanly.")
            sys.exit(0)
        except KeyboardInterrupt:
            print("\n  [System] Forced exit.")
            sys.exit(0)
