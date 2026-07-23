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
# Tier2-b: 独自 genai.Client はコストガードの適用外になるため廃止。
# 共有client・MODEL_NAME (lib_shared, 日次上限ラップ済み) を使用箇所で
# 遅延importすることで、メニュー起動の軽さ (lib_shared の重い初期化回避) は維持する。

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
    {"name": "agent", "description": "AIエージェントに自律実行させる", "function": "engine_plan_execute.run_plan_and_execute", "needs_arg": True, "arg_name": "goal", "arg_prompt": "指示: "},
    {"name": "prediction_audit", "description": "過去の仮想通貨予測を実際の価格と照合して精度を検証する", "function": "tools.tool_crypto.tool_audit_predictions", "needs_arg": False},
    {"name": "discord_bot", "description": "Discord Botを起動する", "function": "bot_discord.run_bot", "needs_arg": False, "long_running": True},
    {"name": "scheduler", "description": "スケジューラーを起動する", "function": "agent_scheduler.run_scheduler", "needs_arg": False, "long_running": True},
    {"name": "n8n_backup", "description": "n8nワークフローをバックアップする", "function": "n8n_patrol.fetch_all_workflows", "needs_arg": False},
    {"name": "lora_dataset", "description": "LoRA学習用データセットを作成する", "function": "tools.tool_lora_factory.run_lora_factory", "needs_arg": False},
    {"name": "analytics", "description": "エージェントのパフォーマンスやエラーログを分析する", "function": "tools.tool_analytics.run_analytics", "needs_arg": True, "arg_name": "question", "arg_prompt": "分析したいこと: "},
    {"name": "bitnet", "description": "ローカルAI (BitNet b1.58): オフライン要約・メッセージ下書き・ルーティングテスト", "function": "tools.tool_bitnet.run_bitnet_interactive", "needs_arg": False},
    {"name": "future_search", "description": "未来シナリオ分析: 英訳→Tavily検索→Gemini 10視点マルチレンズ推論→日本語レポート+個人化アドバイス", "function": "tools.tool_future_search.run_future_search", "needs_arg": True, "arg_name": "topic", "arg_prompt": "分析トピック (日本語OK): "},
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

# Watchdog (Tier2-c): 各サービスの heartbeat / PID ファイル。
# poll() は「死亡」しか検知できないため、heartbeat の鮮度で「ハング」を検知する。
# パスは各サービス側の定義 (bot_discord.HEARTBEAT_FILE 等) と一致させること。
# import で揃えないのは、スーパーバイザに discord 等の重量級依存を持ち込まないため。
_ROOT = os.path.dirname(os.path.abspath(__file__))
SERVICES = {
    "discord_bot": {
        "script": "bot_discord.py",
        "hb_file": os.path.join(_ROOT, "logs", "bot_discord.hb"),
        "pid_file": os.path.join(_ROOT, "bot_discord.pid"),
    },
    "scheduler": {
        "script": "agent_scheduler.py",
        "hb_file": os.path.join(_ROOT, "logs", "agent_scheduler.hb"),
        "pid_file": os.path.join(_ROOT, "agent_scheduler.pid"),
    },
}

# kill → ランチャー親 exit → poll() 検知 のタイムラグ中に再killを連打しないための猶予
_KILL_COOLDOWN = 60


def run_and_monitor():
    """Bot と Scheduler を起動し、監視（死亡 = 自動再起動 / ハング = 強制再起動）する"""
    from lib import watchdog
    from lib.pid_guard import read_owner

    print("\n" + "="*50)
    print("  🛰️ ARMS Parallel Launcher & Monitor")
    print("  - Starting Discord Bot & Scheduler...")
    print("  - Auto-restart enabled (5s delay)")
    print(f"  - Watchdog: heartbeat stale > {watchdog.STALE_THRESHOLD}s -> force restart")
    print("  - Ctrl+C to stop all and return to menu")
    print("="*50 + "\n")

    python_exe = sys.executable

    def start_process(name, script):
        print(f"  [Launcher] Starting {name}...")
        # 絶対パスで起動 (cwd が C:\AI_agent 以外でもスーパーバイザが機能するように)
        return subprocess.Popen([python_exe, os.path.join(_ROOT, script)])

    processes = {
        name: {
            "proc": start_process(name, svc["script"]),
            "started_at": time.time(),   # ハング判定の起点 (起動猶予を兼ねる)
            "killed_at": 0.0,            # 直近の watchdog kill 時刻
        }
        for name, svc in SERVICES.items()
    }

    try:
        while True:
            time.sleep(5)
            now = time.time()
            for name, state in processes.items():
                svc = SERVICES[name]

                # --- 死亡検知 (既存): ランチャー親の exit を拾って再生成 ---
                if state["proc"].poll() is not None:
                    print(f"  [Monitor] ⚠️ {name} has stopped "
                          f"(code: {state['proc'].returncode}). Restarting...")
                    state["proc"] = start_process(name, svc["script"])
                    state["started_at"] = time.time()
                    state["killed_at"] = 0.0
                    continue

                # --- ハング検知 (Watchdog): heartbeat の鮮度で判定 ---
                if now - state["killed_at"] < _KILL_COOLDOWN:
                    continue  # kill 直後: 親の exit を poll が拾うまで待つ
                if watchdog.is_stale(svc["hb_file"], state["started_at"], now=now):
                    age = int(watchdog.age_seconds(svc["hb_file"], state["started_at"], now=now))
                    # 殺すのは PID ファイルが指す「実体」のみ。ランチャー親 (Popen
                    # ハンドル) を先に殺すと、実体が Discord Gateway を掴んだまま
                    # 孤児化する (2026-07-17 の再起動手順で確認済みの罠)。
                    # 実体が死ねば親は自然に exit し、上の poll() が再起動する。
                    # PID 再利用による誤kill の心配は不要: 親は実体を wait して
                    # exit するため「親が生存 (= このパスに来る) ⇔ 実体が生存」
                    # であり、owner は常に生きた実体を指す。
                    owner = read_owner(svc["pid_file"])
                    killed = watchdog.kill_process(owner)
                    print(f"  [Watchdog] 🐕 {name} heartbeat stale ({age}s). "
                          f"Killing PID {owner} -> {'ok' if killed else 'failed/gone'}")
                    watchdog.notify_discord(
                        f"🐕 **Watchdog**: `{name}` の heartbeat が {age} 秒更新されていません。"
                        f"実体 (PID {owner}) を強制再起動します (kill={'成功' if killed else '対象なし'})"
                    )
                    state["killed_at"] = now  # 失敗時もクールダウン後に再試行
    except KeyboardInterrupt:
        print("\n  [Launcher] 🛑 Stopping all processes...")
        for state in processes.values():
            state["proc"].terminate()
        print("  [Launcher] All processes terminated.")

# ---------------------------------------------------------
# AI Router & Menu Functions
# ---------------------------------------------------------
def route_to_tool(user_input: str) -> dict:
    tool_list = "\n".join(f"- {t['name']}: {t['description']}" for t in TOOLS)
    prompt = f"あなたはタスクルーターです。ユーザーの入力に最も適したツールを選んでください。\n\n【ツール】\n{tool_list}\n\n【入力】\n{user_input}\n\nJSON形式: {{\"tool\": \"name\", \"arg\": \"val\"}}"
    try:
        from lib.lib_shared import client, MODEL_NAME  # 遅延import: コストガード適用済み共有client
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        import json, re
        text = re.sub(r'```json\s*|\s*```', '', response.text.strip())
        return json.loads(text)
    except Exception as e:
        print(f"[route_to_tool] Falling back to agent tool: {e}")
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
