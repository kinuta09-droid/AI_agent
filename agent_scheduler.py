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
import subprocess
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
from lib.bot_errors import is_failure_result
from lib.engagement_log import snapshot_open_files

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
# 暗号資産系ツールの import は意図的に外してある (2026-08-17 稼働停止)。
# 理由と再開条件は下の SCHEDULE 内のコメントを参照。
from engine_plan_execute import generate_performance_dashboard, low_reliability_tool_lines
from lib.agent_feedback import summarize_feedback
from tools.tool_obsidian import tag_untagged_clips

# Vault Semantic Index (optional — import失敗でスケジューラ全体を壊さない)
try:
    from tools.tool_vault_index import update_vault_index
except ImportError as _e:
    _vault_index_err = str(_e)
    def update_vault_index():
        return f"Vault Semantic Index: tool_vault_index not available ({_vault_index_err})"

# Activity Index (optional — Vault Semantic Index と同じ扱い)
try:
    from tools.tool_activity_index import update_activity_index
except ImportError as _e:
    _activity_index_err = str(_e)
    def update_activity_index():
        return f"Activity Index: tool_activity_index not available ({_activity_index_err})"

# Document Conversion (Phase 3-0 — docling 未導入でもスケジューラ全体を壊さない)
try:
    from tools.tool_document_sidecar import sync_document_sidecars
except ImportError as _e:
    _doc_sidecar_err = str(_e)
    def sync_document_sidecars():
        return f"Document Conversion: tool_document_sidecar not available ({_doc_sidecar_err})"

# User Memory Distillation (optional)
try:
    from tools.tool_user_memory import distill_daily_memory
except ImportError as _e:
    _user_memory_err = str(_e)
    def distill_daily_memory():
        return f"User Memory: tool_user_memory not available ({_user_memory_err})"

# Note Promotion (optional — Vault Semantic Index と同じ扱い)
try:
    from tools.tool_note_promotion import run_note_promotion_job
except ImportError as _e:
    _note_promotion_err = str(_e)
    def run_note_promotion_job(*args, **kwargs):
        return f"Note Promotion: tool_note_promotion not available ({_note_promotion_err})"

# Vault Insight Watcher (optional — import失敗でスケジューラ全体を壊さない)
try:
    from tools.tool_vault_watcher import check_vault_insights
except ImportError as _e:
    _vault_watcher_err = str(_e)
    def check_vault_insights(*args, **kwargs):
        return f"Vault Insight Watcher: tool_vault_watcher not available ({_vault_watcher_err})"

# Vault Health (optional — Vault Semantic Index と同じ扱い)
try:
    from tools.tool_vault_health import check_vault_health
except ImportError as _e:
    _vault_health_err = str(_e)
    def check_vault_health():
        return f"Vault Health: tool_vault_health not available ({_vault_health_err})"

# Orphan Relink (optional — Vault Semantic Index と同じ扱い)
try:
    from tools.tool_orphan_relink import run_weekly_orphan_relink_job
except ImportError as _e:
    _orphan_relink_err = str(_e)
    def run_weekly_orphan_relink_job(*args, **kwargs):
        return f"Orphan Relink: tool_orphan_relink not available ({_orphan_relink_err})"

# Reference Promotion (optional — Vault Semantic Index と同じ扱い)
try:
    from tools.tool_reference_promotion import run_reference_promotion_job
except ImportError as _e:
    _reference_promotion_err = str(_e)
    def run_reference_promotion_job(*args, **kwargs):
        return f"Reference Promotion: tool_reference_promotion not available ({_reference_promotion_err})"

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
    # feature_name は短く曖昧な語のことがあり (例: "Maple-Preview" がMapleStoryと衝突)、
    # そのままTavily検索に投げると無関係なドメインのソースを引く事故が起きるため、
    # xtrendが既に抽出済みのtech_stackで検索文脈を補強する
    context_hint = top.get("tech_stack", "").strip()
    logger.info(f"Auto Deep-Dive: topic='{topic}' context_hint='{context_hint}'")
    return _future_search(topic, depth="standard", uncensored=False, context_hint=context_hint)

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

# Paper Radar / Digest (optional — import失敗でスケジューラ全体を壊さない)
try:
    from tools.tool_paper_radar import run_paper_radar_grouped as _paper_radar_grouped
    _HAS_PAPER_RADAR = True
except ImportError as _e:
    _paper_radar_err = str(_e)
    _HAS_PAPER_RADAR = False

def _run_paper_radar() -> str:
    """注目論文レーダー (スコアリング自体は課金ゼロ。上位件数分のタイトル・要旨の
    日本語訳のみ Gemini LIGHT を呼ぶ — tools/tool_paper_radar.py の docstring参照)。
    定番分野(ai_agent/llm)の実績に新設分野(gui_agent/ai_jobs/astro_jwst)が
    埋もれないよう、グループ単位で上位件数を確保する。既存分野は合算top5、
    新設分野は互いに競合させず分野ごとに個別でtop3ずつ確保する。"""
    if not _HAS_PAPER_RADAR:
        return f"Paper Radar: tool_paper_radar not available ({_paper_radar_err})"
    return _paper_radar_grouped(
        groups=[
            ("ai_agent,llm", 5),
            ("gui_agent", 3),
            ("ai_jobs", 3),
            ("astro_jwst", 3),
        ],
        days=7,
    )


# Skill Radar (optional — import失敗でスケジューラ全体を壊さない)
try:
    from tools.tool_skill_radar import run_skill_radar as _skill_radar
    _HAS_SKILL_RADAR = True
except ImportError as _e:
    _skill_radar_err = str(_e)
    _HAS_SKILL_RADAR = False

def _run_skill_radar() -> str:
    """GitHub巡回でARMS/Claude Code向けの新着Skillを見つける (Gemini LIGHTで全候補の
    ARMS適合度を判定しスコアに反映、無料枠運用のため実課金なし
    — tools/tool_skill_radar.py の docstring参照)。"""
    if not _HAS_SKILL_RADAR:
        return f"Skill Radar: tool_skill_radar not available ({_skill_radar_err})"
    return _skill_radar(top_k=5)


def _run_gws_auth_health_check() -> str:
    """gws (Gmail/Calendar CLI) の認証切れを検知して通知する。

    OAuthテストモードの7日失効 (→ 08-12にIn productionへ公開して対処済みのはず)
    が再発していないか、または別要因で切れていないかを毎朝チェックする。
    `gws auth status` の例外有無ではなく `token_valid` フィールドで判定する
    (Known Bug Patterns A: 「例外が出なかった=成功」は無言の死につながる)。
    silent=True なので正常時は何も通知しない — 通知するのは異常時のみ。
    """
    try:
        proc = subprocess.run(
            ["gws", "auth", "status"], capture_output=True,
            encoding="utf-8", errors="replace", shell=True, timeout=15,
        )
        status = json.loads(proc.stdout)
    except Exception as e:
        msg = f"gws auth status の実行に失敗: {e}"
        send_discord_notification(f"⚠️ **gws認証チェック失敗**\n{msg}")
        return f"Error: {msg}"

    if not status.get("token_valid"):
        reason = status.get("token_error", "不明")
        send_discord_notification(
            f"🔴 **gws (Gmail/Calendar) の認証切れ**\n"
            f"理由: `{reason}`\n"
            f"再認証: `gws auth login`\n"
            f"7日で再発する場合はOAuth同意画面が「In production」のままか確認: "
            f"https://console.cloud.google.com/apis/credentials/consent?project={status.get('project_id', '')}"
        )
        return f"Error: gws token invalid ({reason})"
    return ""


def _run_eval_radar() -> str:
    """週次の自己評価レーダー (検索品質 + エージェント計画品質の回帰テスト)。

    eval は本来「壊れたことに気付くための仕組み」なのに、手動実行しか無いと
    測った記録すら残らない。ここで週次に固定し、閾値割れのときだけ通知する。
    silent=True なので正常時は無言 (通知は scripts/eval_radar.py が自前で行う)。

    測れなかったとき (Ollama 停止・予算不足・タイムアウト) は "Error: ..." が
    返る。_execute_task は失敗を catch_up に記録しないので、PC が落ちていた週と
    同じく**次回のスケジューラ起動時に繰り越される**。
    """
    from scripts.eval_radar import run_eval_radar
    return run_eval_radar()


def _run_eval_staleness() -> str:
    """レーダー自体が止まっていないかを見る (API 呼び出しゼロ)。

    ジョブが発火しなくなった / 毎回スキップされ続けた場合、上のジョブは何も
    通知しないので「静かに監視が消える」。それを検知する唯一の口。
    """
    from scripts.eval_radar import run_eval_staleness
    return run_eval_staleness()


# Vault NotebookLM Sync (optional — import失敗でスケジューラ全体を壊さない)
try:
    from tools.tool_notebooklm import sync_vault_export, VAULT_SYNC_GENRE as _VAULT_SYNC_GENRE
    _HAS_NOTEBOOKLM = True
except ImportError as _e:
    _notebooklm_err = str(_e)
    _HAS_NOTEBOOKLM = False


def _run_vault_notebooklm_sync() -> str:
    """Vaultを再エクスポートしてNotebookLMへ差分同期する (変更が無いファイルはスキップ)。"""
    if not _HAS_NOTEBOOKLM:
        return f"Vault NotebookLM Sync: tool_notebooklm not available ({_notebooklm_err})"
    return sync_vault_export(_VAULT_SYNC_GENRE)


def _run_paper_digest() -> str:
    """レーダー1位を docling でキャプチャして要約 + 投稿下書きを生成する。

    週次にしてある理由: docling の変換 (数十秒〜数分) と Gemini 2回を伴うため
    毎日回すとコストが読めない。レーダー自体は毎朝走っているので、
    「今週いちばん効いた1本」を週末にまとめる形にしている。
    """
    try:
        from tools.tool_paper_digest import run_paper_digest
    except ImportError as e:
        return f"Paper Digest: tool_paper_digest not available ({e})"
    return run_paper_digest(index=1, mode="both")


def _run_agent_feedback_weekly_report() -> str:
    """/agent 👍👎 フィードバックと Planner 低信頼性警告を人間向けにまとめて返す。

    Planner への実際の注入 (engine_plan_execute.get_enhanced_prompt) は毎リクエスト
    自動で行われる (plans記載の Phase 1-2)。このジョブはプロンプト注入のためではなく、
    「今どんな注入がされているか」を人間が確認するための週次スナップショット。
    recent_bad_hints (注入用の直近5件) と窓を揃えるため days=90 を使う。
    """
    parts = [summarize_feedback(days=90)]
    low_rel = low_reliability_tool_lines()
    if low_rel:
        parts.append("\n### Plannerへの信頼性警告 (現在注入中)")
        parts.extend(low_rel)
    return "\n".join(parts)

# Audio Transcription Sync (optional)
try:
    from tools.tool_audio_automation import tool_sync_audio_transcriptions
    _HAS_AUDIO = True
except ImportError:
    _HAS_AUDIO = False
    def tool_sync_audio_transcriptions():
        return "Audio: tool_audio_automation not available"

# Load environment variables
from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)
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

def setup_logging():
    """ログハンドラを attach する。**実プロセスの起動時にのみ呼ぶ** (run_scheduler)。

    import 時に呼んではいけない。以前はモジュールスコープで実行しており、
    `import agent_scheduler` しただけで本番の scheduler.log が開かれていた。
    結果、テストのスタブ (TestTask / Dummy / Boom / simulated failure) が
    本番ログへ書き込まれ、半年で386行混入していた。実際の警告は1件しか
    無いのに、テスト由来のエラー115件に埋もれて障害調査の役に立たない
    状態だった (Known Bug Patterns C「テストが本番と共有する可変資源」)。
    LOG_FILE をテスト側で差し替える方式では直せない — import した時点で
    ハンドラが実ファイルに繋がってしまい、後から差し替えられないため。

    ハンドラの二重登録ガード:
    実プロセスはこのファイルを __main__ として動かすので、tools 側が実行時に
    `from agent_scheduler import ...` すると Python は同じファイルを別名モジュール
    として **もう一度実行する**。logging.getLogger("Scheduler") は同一シングルトンを
    返すため、ガードが無いとハンドラが2組になり以降の全ログが2重に出る
    (実際 2026-08-11 03:20 の tool_vault_watcher 初回実行を境に発生していた)。
    """
    if logger.handlers:
        return

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
# 朝の生成系ジョブ (06:00〜08:00) には catch_up=True を付ける。理由:
#   Windows Update の無人再起動などで朝にプロセスが居ないと、これらは予定の分に
#   完全一致しないと発火しない仕様上、無言でその日の分を落とす (2026-08-12 に発生:
#   05:00 に TrustedInstaller が再起動 → ログオンまで7時間 ARMS 不在 → 全滅)。
# 相場系のジョブには付けない: 遅れて実行しても古い値動きを「今の分析」として
#   出すだけで、無いより有害 (現在は暗号資産系ジョブ自体を停止中。下記参照)。
SCHEDULE = [
    # Daily Note Generation (template-based with Calendar/Tasks/News)
    *(
        [{
            "time": "07:00",
            "name": "Daily Note Generation",
            "func": lambda: generate_daily_note(include_news=False),
            "catch_up": True,
        }] if _HAS_DAILY_NOTE else (
            [{
                "time": "07:30",
                "name": "Daily Sync (Calendar/Tasks)",
                "func": tool_sync_to_daily_note,
                "catch_up": True,
            }] if _HAS_CALENDAR else []
        )
    ),
    {
        "time": "06:00",
        "name": "X Trend Update",
        "func": _run_xtrend,
        "catch_up": True,
    },
    # Inbox Clip Auto-Tagging: Obsidian Web Clipperが00_inboxに保存したノート(source:持ち)へ
    # BitNet(無料)でトピックタグ、Gemini(_determine_parent_moc)でMOC候補タグを付与する。
    # タグ付けした分だけ関数末尾で update_vault_index() を自前で呼ぶ (差分更新なので安価) —
    # 03:15 Vault Semantic Index はこの朝より前に走るため、ここで拾わないと翌日まで検索に乗らない。
    {
        "time": "07:00",
        "name": "Inbox Clip Auto-Tagging",
        "func": tag_untagged_clips,
        "silent": True,  # "0件でした" の定型通知はスパムになるため
        "catch_up": True,
    },
    {
        "time": "07:30",
        "name": "Morning Briefing",
        "func": _run_morning_briefing,
        "silent": True,  # 自前でボタン付き Discord 通知を送るため定型通知は抑止
        "catch_up": True,
    },
    {
        "time": "06:30",
        "name": "Auto Deep-Dive (XTrend TOP1)",
        "func": _run_auto_deep_dive,
        "catch_up": True,
    },
    # gws認証ヘルスチェック: 06:30の後・06:45の前。壊れていれば朝の連鎖(Daily Sync等)より
    # 先にDiscordへ気付けるようにする。silent=Trueで正常時は無言 (異常時のみ自前で通知)。
    {
        "time": "06:40",
        "name": "gws Auth Health Check",
        "func": _run_gws_auth_health_check,
        "silent": True,
        "catch_up": True,
    },
    # 自己評価レーダー (週次): 夜間チェーン(03:20〜03:40)の後・朝チェーン(06:00)の前。
    # 日付が変わって cost_guard の日次カウンタがリセットされた直後の枠を使う。
    # catch_up=True: PC が日曜に落ちていた週は次回起動時に繰り越して実行する。
    # silent=True: 正常時は無言 — 閾値割れ/測定不能のときだけ自前で通知する。
    {
        "time": "04:00",
        "name": "Eval Radar (Weekly)",
        "func": _run_eval_radar,
        "weekday": 6,  # 日曜
        "silent": True,
        "catch_up": True,
    },
    # ↑のジョブが止まったこと自体を検知する (API 0回)。これが無いと
    # 「発火しなくなった」「毎回スキップされ続けた」に気付けず、監視が静かに消える。
    {
        "time": "06:35",
        "name": "Eval Radar Staleness",
        "func": _run_eval_staleness,
        "weekday": 0,  # 月曜
        "silent": True,
        "catch_up": True,
    },
    # 論文レーダー: arXiv/HuggingFace/Semantic Scholar の公開APIのみ (課金ゼロ)。
    # 06:45 は XTrend(06:00)/Deep-Dive(06:30) の後、朝ブリーフィング(07:30) の前。
    {
        "time": "06:45",
        "name": "Paper Radar",
        "func": _run_paper_radar,
        "catch_up": True,
    },
    # 週次ダイジェスト: docling キャプチャ + Gemini 2回。土曜の朝に1本だけ。
    # catch_up=True なので PC が落ちていた週も起動時に1回だけ追いかける。
    {
        "time": "09:30",
        "name": "Paper Digest (Radar TOP1)",
        "func": _run_paper_digest,
        "weekday": 5,  # 土曜
        "catch_up": True,
    },
    # Skill Radar: GitHub Search API のみ (課金ゼロ)。上位のみ Gemini LIGHT で一言コメント。
    # Paper Digest (土曜) の翌日、日曜朝に週1回。
    {
        "time": "10:00",
        "name": "Skill Radar",
        "func": _run_skill_radar,
        "weekday": 6,  # 日曜
        "catch_up": True,
    },
    {
        "time": "08:00",
        "name": "Daily News",
        "func": tool_fetch_daily_news,
        "catch_up": True,
    },
    # ------------------------------------------------------------------
    # 暗号資産系ジョブ (11件) は 2026-08-17 に稼働停止。戦略を白紙から
    # 作り直す方針のため、旧ロジックの自動実行を止めてある。
    #   停止したもの: BTC Analysis(08:30) / Crypto Market Scan(09:00) /
    #   Crypto Sweep(01:05,05:05,09:05,13:05,17:05,21:05) /
    #   Crypto Prediction Audit(22:30) / Weekly Crypto Report(日曜22:50) /
    #   Crypto Event Watcher(15分毎)
    #
    # 停止の根拠 (20銘柄×4年のバックテスト + ランダムエントリー対照実験):
    #   - 4年通算の期待値は -0.00R。勝率33.3%はSL2.5ATR/TP5ATRの幾何が
    #     定めるランダムウォークの値そのもので、判定ロジックの寄与ではない
    #   - 同一条件のランダムエントリーとの差は ±2pt で符号が反転する
    #     (2022H2-2023 +2.0pt / 2024 -2.2pt / 2025-2026 +1.7pt) = ノイズ水準
    #   - 1R は価格の約6.8%。往復コスト0.3〜0.5% = 0.05〜0.07R を引くと、
    #     手仕舞い7種すべてが2024年以降マイナス
    #   - 収益が出ていたのは2022H2-2023のみで、その期間はランダムエントリー
    #     でも勝率36.2% (損益分岐33.3%超) = 相場の上昇が配っていた分
    #
    # 再開の条件: 特徴量単体の予測力を先に測り、コストをモデルに織り込み、
    #   ルールを決めてからアウトオブサンプルで一度だけ検証すること。
    #   複数試して最良を選んだ時点でその数字は使えない (今回は11通り試して
    #   最良を選ぶと偶然だけで +0.13R かさ上げされ、計測値 +0.042R を上回った)。
    #
    # Discord の手動コマンド (/crypto, /scan, /crypto-stats 等) と
    # lib/tool_registry.py の登録は残してある — 自動実行ではないため。
    # ------------------------------------------------------------------
    # Engagement Snapshot (Phase 1): .obsidian/workspace.json の lastOpenFiles は
    # 46件ローリングなので、間隔が長いと「開いた」を取りこぼす。ローカルJSONの
    # 読み取りのみで API 課金ゼロ。定型通知はスパムになるため silent。
    {
        "every_minutes": 15,
        "name": "Engagement Snapshot (opened notes)",
        "func": snapshot_open_files,
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
    # Document Conversion (Phase 3-0): Vault内のPDF/DOCX/PPTXをdoclingでMarkdown化し
    # 99_Assets/Converted/ に配置する。03:15 の Vault Semantic Index より前に置くことで、
    # 変換済みノートが同じ夜のインデックス更新に間に合う (順序が逆だと1日遅れて反映される)。
    {
        "time": "03:05",
        "name": "Document Conversion",
        "func": sync_document_sidecars,
    },
    # Vault Semantic Index (ローカルOllama埋め込み、API課金なし。:15分台は他ジョブと衝突しない)
    {
        "time": "03:15",
        "name": "Vault Semantic Index",
        "func": update_vault_index,
    },
    # Vault Insight Watcher: 03:15 Vault Semantic Index の直後に置く。検索が使う
    # 意味インデックスがその夜の変更を含んで更新済みであることを保証するため。
    # silent=True: notable な所見があるときだけ自前で Discord 通知するため
    # (「今日は何もありません」の定型通知はスパムになる — 沈黙バイアス)。
    {
        "time": "03:20",
        "name": "Vault Insight Watcher",
        "func": check_vault_insights,
        "silent": True,
    },
    # Activity Index (エージェントの行動ログを同じインデックスへ。Phase B)
    #
    # 03:25 にしてある理由は2つ:
    #  1. 03:15 の Vault Index が埋め込みモデル/次元の整合を先に確定させる
    #     (食い違うと Activity 側は書かずにエラーを返す設計)
    #  2. 実行条件を再現可能にするため。bge-m3 が GPU に載るか CPU に載るかは
    #     「直前に検索があったか」で変わる (PR #23 が検索時のクエリ埋め込みを
    #     CPU 常駐にしたため、Ollama が既存ランナーを再利用する)。検索が走らない
    #     深夜なら毎回同じ条件で走る。詳細と実測値は tool_activity_index の
    #     docstring にある (当初懸念した VRAM 競合は実測では起きなかった)。
    {
        "time": "03:25",
        "name": "Activity Index (行動ログ)",
        "func": update_activity_index,
    },
    # Vault Health (Feature 1): 03:25 Activity Index の直後。同じ夜の 03:15
    # Vault Semantic Index で links/files が最新化された後に読むため。
    # 週次トレンドとして意味があるのは週1回 (日曜) なので weekday 指定。
    {
        "time": "03:30",
        "name": "Vault Health Check (未使用ノート率)",
        "func": check_vault_health,
        "weekday": 6,  # 日曜
        "catch_up": True,
    },
    # Vault NotebookLM Sync: 03:25 Activity Index 等と同じ夜間メンテナンス帯の末尾。
    # 内容ハッシュで前回との差分だけ判定するため、変更が無い日はほぼ即座に終わる
    # (tools/tool_notebooklm.py の sync_vault_export docstring参照)。同じ帯の日次ジョブ
    # (Document Conversion/Vault Semantic Index/Activity Index) はどれもcatch_upを
    # 付けていない慣習に合わせ、こちらも付けない (1晩逃しても翌晩に自然に追いつく)。
    {
        "time": "03:35",
        "name": "Vault NotebookLM Sync",
        "func": _run_vault_notebooklm_sync,
    },
    # Agent Feedback Weekly Report: 03:35 の直後、同じ夜間帯の末尾。
    # /agent 👍👎 と Planner 低信頼性警告が「今どう注入されているか」を人間が
    # 確認するためのスナップショット (plans記載 Phase 3)。Vault Health と同じ
    # 週1回の性質なので weekday 指定 + catch_up。
    {
        "time": "03:40",
        "name": "Agent Feedback Weekly Report",
        "func": _run_agent_feedback_weekly_report,
        "weekday": 6,  # 日曜
        "catch_up": True,
    },
    # Note Promotion (Progressive Summarization): 03:15 Vault Semantic Index の
    # 埋め込みをそのまま読むため、当夜のインデックス更新後に置く。silent=True:
    # クラスタが無ければ無言 (Inbox Clip Auto-Tagging と同じ「0件でした通知は
    # スパム」判断)。承認ボタン付き通知は自前で送るため定型通知は抑止する。
    {
        "time": "03:45",
        "name": "Note Promotion (fleeting→evergreen クラスタ提案)",
        "func": run_note_promotion_job,
        "silent": True,
    },
    # Orphan Relink: Note Promotionの直後、同じ夜間帯の末尾。週1回の性質なので
    # weekday指定。catch_upは付けない — Note Promotionが週次でもcatch_upを
    # 付けていない前例に倣う。孤児ノートは1週遅れても実害が小さく、catch_up追加は
    # 「起動時に前回分と今回分が両方走りpendingが二重生成される」リスクを増やすだけ。
    {
        "time": "03:50",
        "name": "Orphan Relink (被リンク0件ノートの関連付け提案)",
        "func": run_weekly_orphan_relink_job,
        "weekday": 6,  # 日曜
        "silent": True,
    },
    # Reference Promotion (教訓の自動昇格 Vault版): ノート系夜間ジョブの末尾。検索ログの
    # 集計は当夜のVault Semantic Indexと無関係に独立して動くが、並びとしてここにまとめる。
    # weekday指定・catch_upなしはOrphan Relinkと同じ判断 (1週遅れても実害が小さく、
    # catch_up追加はpending二重生成リスクを増やすだけ)。
    {
        "time": "03:55",
        "name": "Reference Promotion (よく参照されるノートのMOC昇格提案)",
        "func": run_reference_promotion_job,
        "weekday": 6,  # 日曜
        "silent": True,
    },
    # Weekly Archive (daily folders → 50_Archive) は2026-08-22廃止。
    # Discord横断検索(hybrid search)が主な導線になった今、Weekly_Summaryが
    # 「その週の全ノートを生本文のまま連結する」設計のため検索ハブに汚染源と
    # 化していた(孤児ノート一括処理で候補が常にWeekly_Summaryへ収束する実害を確認)。
    # フォルダ閲覧のための整理という当初の目的自体が薄れたため、移動/要約生成を
    # やめる方針にした。関数本体は tools/tool_obsidian.py から削除済み。
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
    now: datetime.datetime, weekday: int | None, hh: int, mm: int
) -> datetime.datetime:
    """now 以前で最も近い「(その曜日の) その時刻」の日時を返す純関数。

    weekday=None は曜日指定なしの日次ジョブを指し、直近の HH:MM を返す。

    例: now=月10:00, weekday=6(日), 23:30 -> 直前の日曜 23:30
    例: now=火12:05, weekday=None,  07:00 -> 同じ火曜 07:00
    例: now=火06:00, weekday=None,  07:00 -> 前日(月) 07:00
    """
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if weekday is None:
        return target if target <= now else target - datetime.timedelta(days=1)
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

            # ツール層は例外を投げずに "Error: ..." を返す規約なので、
            # 「例外が出なかった = 成功」と決めつけると失敗が ✅ で通知される。
            # 定時ジョブは誰も見ていないところで動くため、ここで嘘をつくと
            # 「毎朝 ✅ が届いている = 健全」と誤認したまま壊れ続ける。
            failed = is_failure_result(result)

            if failed:
                logger.error(f"❌ {task_name} returned a failure result: {short_result[:200]}")
            else:
                logger.info(f"✅ Completed {task_name}")

            # catch-up 対象ジョブは成功時刻を記録し、次回起動時の未実行判定の基準にする。
            # 例外/予算超過/失敗文字列のときは記録しない = 未実行扱いのまま残し、
            # 次回 catch-up で拾えるようにする。
            if task.get("catch_up") and not failed:
                _record_job_run(task_name, datetime.datetime.now())

            if not silent:
                if failed:
                    send_discord_notification(
                        f"❌ **Task Failed**: {task_name}\n>>> {short_result}",
                        components=_button_row([("📋 ログ表示", SCHEDULER_LOG_CUSTOM_ID)]),
                    )
                else:
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
    due: list[tuple[datetime.datetime, dict]] = []

    for task in SCHEDULE:
        if not task.get("catch_up"):
            continue
        # catch_up は HH:MM 発火のジョブが対象。weekday 有り=週次 / 無し=日次。
        # every_minutes のジョブは「取りこぼし」の概念が無いので対象外。
        when = task.get("time")
        weekday = task.get("weekday")
        if when is None:
            logger.warning(
                f"catch_up 指定だが time が無い: {task['name']} — スキップ")
            continue
        name = task["name"]
        hh, mm = (int(x) for x in when.split(":"))
        occurrence = _last_scheduled_occurrence(now, weekday, hh, mm)

        # 「今の分」に予定されているものは catch-up の対象にしない。
        # run_scheduler は last_run_minute="" で始まるのでメインループの1周目が
        # 直後に同じジョブを発火させる。catch-up は逐次実行で数分遅れて到達しうるため、
        # そのときには相手のロックが解放済みで **同じジョブが2回走る**
        # (Gemini の二重課金 + tool_create_note の重複名フォールバックでノートが増える)。
        # 定時発火に任せれば取りこぼしは起きない。
        if occurrence >= now.replace(second=0, microsecond=0):
            logger.info(f"⏭️ catch-up 対象外: {name} (この分の定時発火に任せる)")
            continue

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
            due.append((occurrence, task))
        else:
            logger.info(f"✔️ catch-up 不要: {name} (前回 {last_run:%Y-%m-%d %H:%M} は最新)")

    if not due:
        return

    # 予定時刻の早い順に「1本のスレッドで逐次」実行する。
    # 並列に投げてはいけない: 朝の連鎖 (X Trend 06:00 → Auto Deep-Dive 06:30 →
    # Morning Briefing 07:30) は前段が書いた結果を後段が読むため、順序が崩れると
    # 後段が前日のデータを掴む。逐次なら通常発火と同じ順序になる。
    due.sort(key=lambda item: item[0])

    def _run_sequentially():
        # 1本のスレッドで回すので、1ジョブの例外で残り全部が無言で消えないよう
        # ここで隔離する。_execute_task は task["func"] の例外は自分で捕まえるが、
        # その外側 (開始通知の送信・_lock 未初期化など) には except が無い。
        for _, task in due:
            try:
                _execute_task(task)
            except Exception as e:
                logger.error(
                    f"❌ catch-up 実行中に例外 ({task['name']}) — 残りは継続: {e}\n"
                    f"{traceback.format_exc()}")

    threading.Thread(target=_run_sequentially, daemon=True).start()


def run_scheduler():
    setup_logging()  # ハンドラ登録はここだけ (import 時にやらない — setup_logging の docstring 参照)
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
