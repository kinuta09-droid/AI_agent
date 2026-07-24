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
import json
import requests
import logging
import threading
import traceback
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# ---------------------------------------------------------
# PID Guard: 多重起動防止
# 取得ロジックは lib.pid_guard (O_EXCL によるアトミック取得) に委譲する。
# 旧実装の「存在チェック → 書き込み」は非アトミックで、ほぼ同時に起動した
# 2プロセスが両方ロックを取れてしまう。二重起動すると定時ジョブ
# (06:00 の XTrend 等) が二重実行される。
# 経緯: 同じ穴で Discord Bot が2重起動した (2026-07-17)。
# ---------------------------------------------------------
from lib.pid_guard import acquire as _acquire_pid_lock, release as _release_pid_lock, read_owner as _read_pid_owner
from lib import watchdog as _watchdog
from lib.cost_guard import GeminiBudgetExceeded

PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_scheduler.pid")

# Watchdog Heartbeat (Tier2-c): メインループが 30秒毎に更新する。
# ジョブはワーカースレッドで実行されるためループは常に回っており、
# 更新停止 = プロセスの機能停止。main.py のスーパーバイザが強制再起動する。
HEARTBEAT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "agent_scheduler.hb")

# Catch-up 状態ファイル: catch_up=True のジョブの「最終成功時刻」を記録する。
# 起動時に「直近の予定発火を過ぎているのに未実行」の週次ジョブを1回だけ追いかける
# 判定基準になる (日曜深夜にPCオフで週次アーカイブ/レポートが空振りする問題への対策)。
# logs/ は gitignore/.claudeignore 済みなのでコミット対象外。
JOB_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "scheduler_job_state.json")

# state ファイルの read-modify-write は worker スレッドと catch-up スレッドの
# 双方から呼ばれ得るため、プロセス内で直列化する。
_JOB_STATE_LOCK = threading.Lock()

# agent_hello.py と同じディレクトリなので直接 import
# ツールを直接インポート
from tools.tool_web import tool_fetch_daily_news
from tools.tool_crypto import tool_scan_crypto_market, tool_analyze_crypto, tool_audit_predictions
from tools.tool_crypto_watcher import tool_crypto_watch
from tools.tool_crypto_stats import tool_weekly_crypto_report
from engine_plan_execute import generate_performance_dashboard
from tools.tool_obsidian import tool_merge_weekly_reports

# Vault Semantic Index (optional — import失敗でスケジューラ全体を壊さない)
try:
    from tools.tool_vault_index import update_vault_index
except ImportError as _e:
    _vault_index_err = str(_e)
    def update_vault_index():
        return f"Vault Semantic Index: tool_vault_index not available ({_vault_index_err})"

# User Memory Distillation (optional)
try:
    from tools.tool_user_memory import distill_daily_memory
except ImportError as _e:
    _user_memory_err = str(_e)
    def distill_daily_memory():
        return f"User Memory: tool_user_memory not available ({_user_memory_err})"

# ボタンの custom_id は bot プロセス側の永続 View (commands/bot_views.py) と
# 一致させる必要があるため定数を直接 import する (discord.Client は起動しない)
from commands.bot_views import DIVE_CUSTOM_IDS, SCHEDULER_LOG_CUSTOM_ID

# Morning Briefing (optional)
try:
    from tools.tool_briefing_manager import tool_generate_briefing
    _HAS_BRIEFING = True
except ImportError:
    _HAS_BRIEFING = False

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

# Future Search / Auto Deep-Dive (optional)
try:
    from tools.tool_future_search import run_future_search as _future_search
    from lib.lib_shared import WORK_DIR as _WORK_DIR
    _HAS_FUTURE_SEARCH = True
except ImportError:
    _HAS_FUTURE_SEARCH = False

def _run_auto_deep_dive() -> str:
    """XTrend TOP1（ARMS親和度最高）を FutureSearch で自動調査。辛口除外。"""
    if not _HAS_FUTURE_SEARCH:
        return "Auto Deep-Dive: tool_future_search not available"
    import json
    xtrend_json = os.path.join(_WORK_DIR, "40_Report", "XTrend", "latest_features.json")
    if not os.path.exists(xtrend_json):
        return "Auto Deep-Dive: latest_features.json が存在しません。スキップ。"
    try:
        with open(xtrend_json, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"Auto Deep-Dive: JSON読み込みエラー: {e}"
    features = data.get("features", [])
    if not features:
        return "Auto Deep-Dive: フィーチャーが0件。スキップ。"
    top = max(features, key=lambda x: (x.get("arms_affinity", 0), x.get("likes", 0)))
    topic = top.get("feature_name", "").strip()
    if not topic:
        return "Auto Deep-Dive: TOP1のfeature_nameが空。スキップ。"
    logger.info(f"Auto Deep-Dive: topic='{topic}'")
    return _future_search(topic, depth="standard", uncensored=False)

# X Trend (optional)
try:
    from tools.tool_xtrend import run as _xtrend_run
    def _run_xtrend():
        features = _xtrend_run()
        return f"XTrend: {len(features)} features analyzed and saved."
    _HAS_XTREND = True
except ImportError:
    _HAS_XTREND = False
    def _run_xtrend():
        return "XTrend: tool_xtrend not available"

# Audio Transcription Sync (optional)
try:
    from tools.tool_audio_automation import tool_sync_audio_transcriptions
    _HAS_AUDIO = True
except ImportError:
    _HAS_AUDIO = False
    def tool_sync_audio_transcriptions():
        return "Audio: tool_audio_automation not available"

# Load environment variables
load_dotenv(r'C:\AI_agent\.env')
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID")

sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------
# CWD 非依存の絶対パス（tool_analytics.py も同じパスを読む）
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler.log")
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

def _button_row(buttons: list[tuple[str, str]]) -> list:
    """[(label, custom_id), ...] から Discord REST の components JSON を組み立てる。

    実際のクリック処理は bot プロセス側の永続 View (commands/bot_views.py) が
    custom_id で判別して行う。スケジューラ自身はコールバックを受信しない。
    """
    return [{
        "type": 1,  # Action Row
        "components": [
            {"type": 2, "style": 1, "label": label, "custom_id": custom_id}
            for label, custom_id in buttons
        ],
    }]


def send_discord_notification(message, components=None):
    """Discord Bot Tokenを使用して指定チャンネルにメッセージを送信する（任意でボタン付き）"""
    if not DISCORD_BOT_TOKEN or not DISCORD_LOG_CHANNEL_ID:
        logger.warning("Discord credentials not set. Skipping notification.")
        return

    url = f"https://discord.com/api/v10/channels/{DISCORD_LOG_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"content": message}
    if components:
        payload["components"] = components

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code not in [200, 201]:
            logger.error(f"Failed to send Discord notification: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Error sending Discord notification: {e}")


def _truncate_preserving_xtrend(content: str, max_len: int = 1900) -> str:
    """Discord(raw REST)送信は2000字上限。XTrendセクションは固定・短いため優先的に
    保持し、Gemini生成の冒頭サマリー側を切り詰める (末尾切り詰めだとXTrendが
    丸ごと消える/一部欠けるバグがあった)。"""
    if len(content) <= max_len:
        return content
    marker = "## 🔥 X Trend"
    idx = content.find(marker)
    if idx == -1:
        return content[:max_len] + "... (truncated)"
    head, tail = content[:idx], content[idx:]
    suffix = "...(続きは省略)\n\n"
    budget = max_len - len(tail) - len(suffix)
    if budget < 100:
        # XTrend自体を含めても収まらない極端なケース: 従来通り単純切り詰め
        return content[:max_len] + "... (truncated)"
    return head[:budget] + suffix + tail


def _run_morning_briefing() -> str:
    """朝ブリーフィングを生成し、深掘りボタン付きで Discord に配信する。"""
    if not _HAS_BRIEFING:
        msg = "Morning Briefing: tool_briefing_manager not available"
        # silent タスクのため、この早期リターンでも自前で通知しないと無言で終わってしまう
        send_discord_notification(f"❌ **Morning Briefing Skipped**: {msg}")
        return msg

    content = tool_generate_briefing(include_content=True)
    display = _truncate_preserving_xtrend(content)

    # latest_features.json が新鮮であれば深掘りボタンを添付する
    from commands.bot_views import _load_top_features
    features = _load_top_features(top_n=3)
    components = None
    if features:
        buttons = [(f"🔍 深掘り {i+1}", cid) for i, cid in enumerate(DIVE_CUSTOM_IDS[:len(features)])]
        components = _button_row(buttons)

    send_discord_notification(f"☀️ **Morning Briefing**\n>>> {display}", components=components)
    return "Morning Briefing: sent"

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
        "time": "06:00",
        "name": "X Trend Update",
        "func": _run_xtrend,
    },
    {
        "time": "07:30",
        "name": "Morning Briefing",
        "func": _run_morning_briefing,
        "silent": True,  # 自前でボタン付き Discord 通知を送るため定型通知は抑止
    },
    {
        "time": "06:30",
        "name": "Auto Deep-Dive (XTrend TOP1)",
        "func": _run_auto_deep_dive,
    },
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
        "time": "22:30",
        "name": "Crypto Prediction Audit",
        "func": tool_audit_predictions
    },
    # Weekly Crypto Report (Phase 4): 日曜22:30監査の直後・23:30アーカイブ前。
    # 判定済みデータが最新の状態で週次集計 → Obsidian保存 + 定型通知
    {
        "time": "22:50",
        "name": "Weekly Crypto Report",
        "func": tool_weekly_crypto_report,
        "weekday": 6,  # 日曜のみ
        "catch_up": True,  # 起動時に取りこぼしを追いかけ実行
    },
    # Crypto Event Watcher (Phase 2): 15分毎の無料監視。閾値超過時のみ
    # フル分析+Discord通知を自前で行うため silent (15分毎の定型通知はスパム)
    {
        "every_minutes": 15,
        "name": "Crypto Event Watcher",
        "func": tool_crypto_watch,
        "silent": True,
    },
    {
        "time": "23:00",
        "name": "Performance Dashboard",
        "func": generate_performance_dashboard
    },
    # User Memory Distillation (前日までのChatlogから長期記憶を蒸留。Gemini 1回/日)
    # 02:45蒸留 → 03:15 Vault Index が更新後の User_Memory.md を同夜に再埋め込み
    {
        "time": "02:45",
        "name": "User Memory Distillation",
        "func": distill_daily_memory,
    },
    # Vault Semantic Index (ローカルOllama埋め込み、API課金なし。:15分台は他ジョブと衝突しない)
    {
        "time": "03:15",
        "name": "Vault Semantic Index",
        "func": update_vault_index,
    },
    {
        "time": "23:30",
        "name": "Weekly Archive (30_Main → 50_Archive)",
        "func": tool_merge_weekly_reports,
        "weekday": 6,  # 日曜日のみ (0=月, 6=日)
        "catch_up": True,  # 起動時に取りこぼしを追いかけ実行
    },
]

# ---------------------------------------------------------
# Main Logic
# ---------------------------------------------------------

def _load_job_state() -> dict:
    """catch-up 状態 (ジョブ名 -> 最終成功ISO時刻) を読み込む。

    ファイル無し/JSON破損はスケジューラを止めず {} にフォールバックする。
    """
    if not os.path.exists(JOB_STATE_FILE):
        return {}
    try:
        with open(JOB_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Job state 読み込み失敗 ({e}) — 空状態で続行")
        return {}


def _record_job_run(name: str, dt: datetime.datetime) -> None:
    """ジョブ name の成功時刻を state に記録する (lock 下で read-modify-write)。"""
    with _JOB_STATE_LOCK:
        state = _load_job_state()
        state[name] = dt.isoformat()
        try:
            os.makedirs(os.path.dirname(JOB_STATE_FILE), exist_ok=True)
            with open(JOB_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Job state 書き込み失敗 ({e}) — catch-up 判定に影響し得る")


def _last_scheduled_occurrence(
    now: datetime.datetime, weekday: int, hh: int, mm: int
) -> datetime.datetime:
    """now 以前で最も近い「その曜日・その時刻」の日時を返す純関数。

    例: now=月10:00, weekday=6(日), 23:30 -> 直前の日曜 23:30
    """
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    candidate = target - datetime.timedelta(days=(now.weekday() - weekday) % 7)
    if candidate > now:
        candidate -= datetime.timedelta(days=7)
    return candidate


def _execute_task(task):
    """1タスクを実行する（ワーカースレッド内で完結: 例外処理・通知含む）。

    task["silent"] = True の場合、タスク関数が自前で Discord 通知を行う想定
    (例: ボタン付きメッセージ) のため、開始/完了の定型通知は送らない。
    """
    task_name = task['name']
    silent = task.get("silent", False)

    # 前回実行が終わっていない場合は多重実行しない
    if not task["_lock"].acquire(blocking=False):
        logger.warning(f"⏭️ {task_name} is still running — skipping this trigger.")
        return
    try:
        logger.info(f"🚀 Executing {task_name}...")
        if not silent:
            send_discord_notification(f"⏳ **Processing Started**: {task_name}")

        try:
            result = task["func"]()

            # Log and Notify
            result = str(result) if result is not None else ""
            if len(result) > 1500:
                short_result = result[:1500] + "... (truncated)"
            else:
                short_result = result if result else "Done (No output)"

            logger.info(f"✅ Completed {task_name}")
            # catch-up 対象ジョブは成功時刻を記録し、次回起動時の未実行判定の基準にする。
            # 例外/予算超過時は記録しない = 未実行扱いのまま残し、次回 catch-up で拾える。
            if task.get("catch_up"):
                _record_job_run(task_name, datetime.datetime.now())

            if not silent:
                send_discord_notification(f"✅ **Task Completed**: {task_name}\n>>> {short_result}")

        except GeminiBudgetExceeded as e:
            # 日次予算超過: 初回到達時に cost_guard がログチャンネルへ通知済み。
            # 残りジョブ分の「❌ Task Failed」アラーム連打はスパムになるため
            # ログのみでスキップ扱いにする。
            logger.warning(f"⏸️ {task_name} skipped: {e}")
        except Exception as e:
            logger.error(f"❌ Error in {task_name}: {e}\n{traceback.format_exc()}")
            error_msg = str(e)[:1000]
            send_discord_notification(
                f"❌ **Task Failed**: {task_name}\nError: ```{error_msg}```",
                components=_button_row([("📋 ログ表示", SCHEDULER_LOG_CUSTOM_ID)]),
            )
    finally:
        task["_lock"].release()


def _run_catch_up():
    """起動時、未実行の週次ジョブ (catch_up=True) を1回だけ追いかけ実行する。

    「直近の予定発火時刻」より最終成功が古ければ未実行とみなして発火。
    初回ブート (記録なし) は「取りこぼした」判断ができないので、状態を now で
    シードするだけで発火はしない (想定外の一斉実行を回避)。
    複数週の取りこぼしも 1回に集約する (履歴のリプレイではなく現状への追いつき)。
    """
    now = datetime.datetime.now()
    state = _load_job_state()

    for task in SCHEDULE:
        if not task.get("catch_up"):
            continue
        # catch_up は日次 HH:MM + weekday の週次ジョブを対象にする
        when = task.get("time")
        weekday = task.get("weekday")
        if when is None or weekday is None:
            logger.warning(
                f"catch_up 指定だが time/weekday が無い: {task['name']} — スキップ")
            continue
        name = task["name"]
        hh, mm = (int(x) for x in when.split(":"))
        occurrence = _last_scheduled_occurrence(now, weekday, hh, mm)

        last_run_iso = state.get(name)
        if not last_run_iso:
            # 初回ブート: 履歴が無いので発火せず now でシードのみ
            _record_job_run(name, now)
            logger.info(f"🌱 catch-up seed: {name} (履歴なし — 今回は発火しない)")
            continue

        try:
            last_run = datetime.datetime.fromisoformat(last_run_iso)
        except ValueError:
            _record_job_run(name, now)
            logger.warning(f"🌱 catch-up seed: {name} (last_run 解析不能 — 再シード)")
            continue

        if last_run < occurrence:
            logger.info(
                f"⏰ catch-up 発火: {name} "
                f"(予定 {occurrence:%Y-%m-%d %H:%M} > 前回 {last_run:%Y-%m-%d %H:%M})")
            threading.Thread(
                target=_execute_task, args=(task,), daemon=True).start()
        else:
            logger.info(f"✔️ catch-up 不要: {name} (前回 {last_run:%Y-%m-%d %H:%M} は最新)")


def run_scheduler():
    if not _acquire_pid_lock(PID_FILE):
        print(f"[Scheduler] ⚠️ Another scheduler instance is already running "
              f"(PID {_read_pid_owner(PID_FILE)}). Exiting.")
        sys.exit(0)  # 正常終了として扱う（ループ防止）
    try:
        logger.info("⏳ Started. Waiting for scheduled time...")

        # 設定されたスケジュールを一覧表示（+ 多重実行防止ロックを初期化）
        for task in SCHEDULE:
            task["_lock"] = threading.Lock()
            when = task.get("time") or f"every {task['every_minutes']}min"
            logger.info(f" - {when}: {task['name']}")

        # 起動時キャッチアップ: 未実行の週次ジョブを1回だけ追いかける
        # (日曜深夜PCオフで空振りした週次アーカイブ/レポートの取りこぼし対策)。
        # catch-up は補助機能。ここで失敗してもスケジューラ本体(メインループ/
        # heartbeat)は必ず起動させる。例外を飲まないと watchdog 再起動ループに陥る。
        try:
            _run_catch_up()
        except Exception as e:
            logger.error(f"catch-up 実行失敗 (本体は継続): {e}\n{traceback.format_exc()}")

        # 最後に実行した分を記録（重複実行防止用）
        last_run_minute = ""

        # Watchdog heartbeat の前回送出時刻 (30秒スロットリング)
        last_beat = 0.0

        while True:
            try:
                if time.time() - last_beat >= _watchdog.BEAT_INTERVAL:
                    _watchdog.beat(HEARTBEAT_FILE)
                    last_beat = time.time()

                now = datetime.datetime.now()
                current_time = now.strftime("%H:%M")

                # 分が変わった瞬間のみチェック
                if current_time != last_run_minute:
                    for task in SCHEDULE:
                        # "time": 日次 HH:MM 発火 / "every_minutes": N分間隔発火
                        if "every_minutes" in task:
                            due = now.minute % task["every_minutes"] == 0
                        else:
                            due = current_time == task["time"]
                        if due and (
                            "weekday" not in task or now.weekday() == task["weekday"]
                        ):
                            # 長時間タスクがループをブロックして他ジョブが
                            # スキップされないよう、ワーカースレッドで実行する
                            threading.Thread(
                                target=_execute_task, args=(task,), daemon=True
                            ).start()

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
        _release_pid_lock(PID_FILE)

if __name__ == "__main__":
    run_scheduler()
