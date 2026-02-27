# -*- coding: utf-8 -*-
"""
Antigravity Standalone Scheduler
Discord Botとは独立して、定時にレポート作成ツールを実行する。
ログ出力とDiscord通知機能付き。
"""

import time
import datetime
import sys
import os
import requests
import logging
import traceback
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
import signal

# ---------------------------------------------------------
# PID Guard: 多重起動防止
# ---------------------------------------------------------
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_scheduler.pid")

def _check_existing_scheduler():
    """PID ファイルを確認し、既にスケジューラが動作しているかチェックする。"""
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE, 'r') as f:
            old_pid = int(f.read().strip())
        if old_pid == os.getpid():
            return
        
        # Windows でプロセスが存在するか確認
        import ctypes
        PROCESS_QUERY_INFORMATION = 0x0400
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, old_pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            print(f"[Scheduler] ⚠️ Another scheduler instance is already running (PID {old_pid}). Exiting.")
            sys.exit(0)
    except (ValueError, OSError):
        pass
    finally:
        try: os.remove(PID_FILE)
        except OSError: pass

def _write_pid():
    with open(PID_FILE, 'w') as f: f.write(str(os.getpid()))

def _cleanup_pid():
    try: os.remove(PID_FILE)
    except OSError: pass

# agent_hello.py と同じディレクトリなので直接 import
# ツールを直接インポート
from tools.tool_web import tool_fetch_daily_news
from tools.tool_crypto import tool_scan_crypto_market, tool_analyze_crypto, tool_audit_predictions
from engine_plan_execute import generate_performance_dashboard
from tools.tool_obsidian import tool_merge_weekly_reports

# Calendar sync (optional — missing dependency won't break scheduler)
try:
    from tools.tool_calendar import tool_sync_to_daily_note
    _HAS_CALENDAR = True
except ImportError:
    _HAS_CALENDAR = False

# Daily Note generation (optional)
try:
    from tools.tool_daily_note import generate_daily_note
    _HAS_DAILY_NOTE = True
except ImportError:
    _HAS_DAILY_NOTE = False

# Load environment variables
load_dotenv(r'C:\AI_agent\.env')
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID")

sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------
LOG_FILE = "scheduler.log"
logger = logging.getLogger("Scheduler")
logger.setLevel(logging.INFO)

# File Handler (Rotating)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

# Console Handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('[Scheduler] %(message)s'))
logger.addHandler(console_handler)

def send_discord_notification(message):
    """Discord Bot Tokenを使用して指定チャンネルにメッセージを送信する"""
    if not DISCORD_BOT_TOKEN or not DISCORD_LOG_CHANNEL_ID:
        logger.warning("Discord credentials not set. Skipping notification.")
        return

    url = f"https://discord.com/api/v10/channels/{DISCORD_LOG_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"content": message}
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code not in [200, 201]:
            logger.error(f"Failed to send Discord notification: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Error sending Discord notification: {e}")

# ---------------------------------------------------------
# Schedule Configuration
# ---------------------------------------------------------
# 新しいスケジュールを追加する場合は、ここに辞書を追加するだけでOKです。
SCHEDULE = [
    # Daily Note Generation (template-based with Calendar/Tasks/News)
    *(
        [{
            "time": "07:00",
            "name": "Daily Note Generation",
            "func": lambda: generate_daily_note(include_news=False),
        }] if _HAS_DAILY_NOTE else (
            [{
                "time": "07:30",
                "name": "Daily Sync (Calendar/Tasks)",
                "func": tool_sync_to_daily_note,
            }] if _HAS_CALENDAR else []
        )
    ),
    {
        "time": "08:00",
        "name": "Daily News",
        "func": tool_fetch_daily_news
    },
    {
        "time": "08:30",
        "name": "BTC Analysis",
        "func": lambda: tool_analyze_crypto("BTC/USDT")
    },
    {
        "time": "09:00",
        "name": "Crypto Market Scan",
        "func": tool_scan_crypto_market
    },
    {
        "time": "23:00",
        "name": "Performance Dashboard",
        "func": generate_performance_dashboard
    },
    {
        "time": "23:30",
        "name": "Weekly Archive (30_Main → 50_Archive)",
        "func": tool_merge_weekly_reports,
        "weekday": 6,  # 日曜日のみ (0=月, 6=日)
    },
]

# ---------------------------------------------------------
# Main Logic
# ---------------------------------------------------------

def run_scheduler():
    _check_existing_scheduler()
    _write_pid()
    try:
        logger.info("⏳ Started. Waiting for scheduled time...")
        
        # 設定されたスケジュールを一覧表示
        for task in SCHEDULE:
            logger.info(f" - {task['time']}: {task['name']}")

        # 最後に実行した分を記録（重複実行防止用）
        last_run_minute = ""

        while True:
            try:
                now = datetime.datetime.now()
                current_time = now.strftime("%H:%M")
                
                # 分が変わった瞬間のみチェック
                if current_time != last_run_minute:
                    for task in SCHEDULE:
                        if current_time == task["time"] and (
                            "weekday" not in task or now.weekday() == task["weekday"]
                        ):
                            task_name = task['name']
                            logger.info(f"🚀 {current_time} Executing {task_name}...")
                            send_discord_notification(f"⏳ **Processing Started**: {task_name}")

                            try:
                                # Execute Task
                                result = task["func"]()
                                
                                # Log and Notify
                                if result and len(result) > 1500:
                                    short_result = result[:1500] + "... (truncated)"
                                else:
                                    short_result = result if result else "Done (No output)"

                                logger.info(f"✅ Completed {task_name}")
                                send_discord_notification(f"✅ **Task Completed**: {task_name}\n>>> {short_result}")
                                
                            except Exception as e:
                                logger.error(f"❌ Error in {task_name}: {e}\n{traceback.format_exc()}")
                                error_msg = str(e)[:1000]
                                send_discord_notification(f"❌ **Task Failed**: {task_name}\nError: ```{error_msg}```")
                            
                    last_run_minute = current_time

                # CPU負荷軽減
                time.sleep(1)
                
            except KeyboardInterrupt:
                logger.info("🛑 Scheduler stopped by user.")
                break
            except Exception as e:
                logger.critical(f"🔥 Critical Scheduler Error: {e}")
                time.sleep(5)
    finally:
        _cleanup_pid()

if __name__ == "__main__":
    run_scheduler()
