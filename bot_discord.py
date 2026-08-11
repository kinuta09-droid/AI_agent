# -*- coding: utf-8 -*-
"""
Antigravity Discord Command Center
Discord Bot を通じて agent_hello.py のツールを操作し、
全ログを Obsidian デイリーノートに集約する。
"""

import os
import sys
import re
import io
import asyncio
from lib.lib_obsidian_utils import get_obsidian_uri, get_obsidian_uri_from_link
from lib.engagement_log import log_event, parse_command_meta
from lib.bot_errors import log_command_error
from lib.pid_guard import acquire as _acquire_pid_lock, release as _release_pid_lock, read_owner as _read_pid_owner
from lib import watchdog as _watchdog
import datetime
import subprocess

try:
    import openai as openai_lib
    OPENAI_AVAILABLE = True
except ImportError:
    openai_lib = None
    OPENAI_AVAILABLE = False

sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------
# PID Guard: 重複起動防止
# 取得ロジックは lib.pid_guard (O_EXCL によるアトミック取得) に委譲する。
# 旧実装の「存在チェック → 書き込み」は非アトミックで、ほぼ同時に起動した
# 2プロセスが両方ロックを取れてしまい Bot の2重起動を招いた (2026-07-17)。
# ---------------------------------------------------------
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_discord.pid")

# ---------------------------------------------------------
# Watchdog Heartbeat: ハング検知 (Tier2-c)
# イベントループ上で 30秒毎に heartbeat ファイルを更新する。更新が止まる =
# イベントループが機能停止しているので、main.py のスーパーバイザが実体を
# 強制再起動する。/agent 等の重い処理は asyncio.to_thread でループ外に
# 逃がしてあるため、長時間タスク中でも heartbeat は止まらない (誤検知しない)。
# ---------------------------------------------------------
HEARTBEAT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "bot_discord.hb")

# ---------------------------------------------------------
# Modular Imports
# ---------------------------------------------------------

from lib.lib_shared import (
    VAULT_ROOT,
    DAILY_NOTE_DIR_NAME,
)

from tools.tool_crypto import (
    tool_audit_predictions,
)

from tools.tool_curator import (
    search_and_save_topic,
    update_user_profile,
)

from tools.tool_obsidian import (
    tool_vault_search,
    tool_create_note,
    tool_read_note,
    tool_update_note,
    tool_merge_weekly_reports,
)

from tools.tool_briefing_manager import (
    tool_generate_briefing,
    add_provider,
)

from tools.tool_gmail_manager import (
    get_gmail_summary as tool_gmail_check,
)

from tools.tool_calendar import (
    tool_add_event,
    tool_create_task,
    tool_sync_to_daily_note,
)

# 新機能: 記事生成 UI
from commands import bot_commands_article
from commands import bot_commands_idea
from commands import bot_commands_curator
from commands import bot_commands_calendar
from commands import bot_commands_tools
from commands import bot_commands_agent
from commands import bot_commands_client
from commands import bot_commands_admin
from commands import bot_commands_chat
from commands import bot_views

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

# ルートを直書きすると、リポジトリを別の場所に置いた環境 (CI の
# D:\a\AI_agent\AI_agent 等) で .env が読まれない。__file__ 相対にする。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID", "")
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


def _parse_allowed_user_ids(raw: str) -> set:
    """カンマ区切りのユーザーID文字列を set[int] にパースする（数字以外は警告して無視）。"""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            ids.add(int(part))
        else:
            print(f"[Discord] ⚠️ ALLOWED_USER_IDS の不正なトークンを無視しました: {part!r}")
    return ids


ALLOWED_USER_IDS = _parse_allowed_user_ids(os.getenv("ALLOWED_USER_IDS", ""))
if not ALLOWED_USER_IDS:
    # フェイルオープン: env 設定ミスで bot が完全停止するのを避けるため、警告のみ出して全許可
    print("[Discord] ⚠️ ALLOWED_USER_IDS が未設定です。全ユーザーがコマンドを実行できます。")


def _is_allowed_user(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS

_openai_client = (
    openai_lib.OpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_AVAILABLE and OPENAI_API_KEY
    and not OPENAI_API_KEY.startswith("sk-YOUR")
    else None
)

# トークン欠落の判定は run_bot() (起動時) に置く。
# ここで sys.exit(1) すると `import bot_discord` そのものが失敗し、
#   - verify_project.py の [3] import 整合性が「Import Error: bot_discord」で常に落ちる
#   - bot_discord からコマンド定義を読むだけのテストも巻き込まれて落ちる
# ため、.env を持たない環境 (CI) では検査が原理的に成立しなくなる。
# import は副作用でプロセスを殺してはいけない。起動しようとした時に初めて落ちる。

# ---------------------------------------------------------
# Bot Setup
# ---------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)


class GatedCommandTree(app_commands.CommandTree):
    """ALLOWED_USER_IDS に含まれないユーザーの全スラッシュコマンドを一括拒否する。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_allowed_user(interaction.user.id):
            return True
        try:
            await interaction.response.send_message(
                "⛔ このボットのコマンドを実行する権限がありません。", ephemeral=True
            )
        except discord.HTTPException:
            pass
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        # interaction_check の拒否 (CheckFailure) は正常系なのでログに出さない
        if isinstance(error, app_commands.CheckFailure):
            return
        await super().on_error(interaction, error)


tree = GatedCommandTree(bot)

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def _log_to_daily_note(log_type: str, user_input: str, ai_output: str = "",
                       log_engagement: bool = True):
    """Obsidian 50_Archive/Chatlog に Discord ログを保存する。

    log_engagement=True のとき、同じ入力を engagement.jsonl にも query として
    記録する（Phase 1: 自由入力＝最も強い顕示選好シグナル）。Chatlog と同じ
    文字列を使うので、過去分のバックフィルとライブ記録が完全に同形式になる。
    既に click 等で記録済みの呼び出し元だけが False を渡す。
    """
    if log_engagement and log_type in ("chat", "command"):
        log_event(
            "query", user_input, "discord",
            meta=parse_command_meta(user_input, log_type),
        )

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.datetime.now().strftime("%H:%M")
    # Save to 50_Archive/Chatlog
    log_dir = os.path.join(VAULT_ROOT, "50_Archive", "Chatlog")
    daily_path = os.path.join(log_dir, f"{today}.md")

    if log_type == "chat":
        entry = (
            f"\n## 💬 Discord Chat ({time_str})\n"
            f"**User**: {user_input}\n"
            f"**AI**: {ai_output}\n"
            f"#log/mobile\n"
        )
    elif log_type == "action":
        entry = (
            f"\n## 📱 Action Log ({time_str})\n"
            f"{user_input}\n"
            f"#log/action\n"
        )
    elif log_type == "command":
        entry = (
            f"\n## 🤖 Discord Command ({time_str})\n"
            f"**Command**: {user_input}\n"
            f"**Result**: {ai_output[:200]}{'...' if len(ai_output) > 200 else ''}\n"
            f"#log/mobile\n"
        )
    else:
        entry = f"\n## 📝 Log ({time_str})\n{user_input}\n"

    # デイリーノートが存在しない場合は基本ヘッダーで作成
    if not os.path.exists(daily_path):
        os.makedirs(os.path.dirname(daily_path), exist_ok=True)
        with open(daily_path, "w", encoding="utf-8") as f:
            f.write(f"# {today}\n")

    with open(daily_path, "a", encoding="utf-8") as f:
        f.write(entry)


# ---------------------------------------------------------
# Obsidian Helper Views
# ---------------------------------------------------------

# Obsidian を開くボタンの有効期限 (最後の操作からの idle 秒数)。
# この View は「そのメッセージ専用のリンク集」をコールバックのクロージャに閉じ込めて
# おり、ボタンに固定 custom_id も無い (自動採番) ため、bot.add_view() で永続化できない。
# かつて timeout=None を指定していたが、それは
#   1. Bot 再起動後はどのみち押しても無反応 (所見3と同じ無言の死)
#   2. discord.py の View ストアから永久に消えず、送信するたびにメモリを食う
# という二重の嘘だった。有限期限にして「いつか失効する UI」であることを明示する
# (恒久対応は custom_id に vault 相対パスを埋めた永続 View 化 — 別課題) (2026-07-26)。
_OBSIDIAN_VIEW_TIMEOUT = 3600  # 1時間


class OpenInObsidianView(bot_views.AllowedUserView):
    def __init__(self, links: dict[str, str]):
        """
        links: {filename: uri} の辞書
        """
        super().__init__(timeout=_OBSIDIAN_VIEW_TIMEOUT)

        # リンク数に応じてボタンを追加 (最大5個まで)
        # 5個を超える場合はドロップダウン (TODO: 実装簡略化のため一旦ボタンのみ、溢れたら無視)
        count = 0
        for name, uri in links.items():
            if count >= 5:
                break
            # ラベルが長すぎる場合は切り詰め
            label = f"📂 {name}"[:80]
            
            # URLではなく、コールバックでローカルコマンド(os.startfile)を実行する
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
            
            # 変数をキャプチャするためにデフォルト引数を使用
            async def callback(interaction: discord.Interaction, uri=uri, name=name):
                try:
                    if uri.startswith("obsidian://"):
                        # Windows only: ローカルでURIを開く
                        os.startfile(uri)
                        await interaction.response.send_message(f"🚀 Opening **{name}** in Obsidian...", ephemeral=True, delete_after=3)
                    else:
                        await interaction.response.send_message("❌ Invalid URI scheme.", ephemeral=True)
                except Exception as e:
                    await interaction.response.send_message(
                        log_command_error(f"Obsidian で開く ({name})", e, "❌ Failed to open"),
                        ephemeral=True,
                    )
            
            btn.callback = callback
            self.add_item(btn)
            count += 1

def _create_obsidian_view(content: str) -> discord.ui.View | None:
    """コンテンツ内のパス([[...]]含む)を検出し、Obsidianを開くボタンViewを作成する"""
    unique_links = {}

    # 1. (Path: ...) の検出（絶対パス）
    for match in re.finditer(r"\(Path:\s*([A-Z]:\\[^)]+)\)", content):
        path = match.group(1).strip()
        uri = get_obsidian_uri(path)
        if uri:
            name = os.path.basename(path)
            unique_links[name] = uri

    # 2. [[...]] の検出（相対パス/ファイル名）
    for match in re.finditer(r"\[\[(.*?)\]\]", content):
        link_text = match.group(1).strip()
        # パイプ | で表示名が指定されている場合がある [[file|label]] -> fileを採用
        if "|" in link_text:
            link_text = link_text.split("|")[0]
            
        uri = get_obsidian_uri_from_link(link_text)
        if uri:
            # 拡張子がない場合は補完して表示名にする
            name = link_text if "." in link_text else f"{link_text}.md"
            unique_links[name] = uri
            
    # 3. LoRA出力パスの検出
    match = re.search(r"出力:\s*([A-Z]:\\[^\n]+)", content)
    if match:
        path = match.group(1).strip()
        uri = get_obsidian_uri(path)
        if uri:
             name = "LoRA Dataset"
             unique_links[name] = uri

    # 4. Gmail URL の検出 → 外部リンクボタン
    gmail_links = {}
    for match in re.finditer(r"\(Gmail(\d*):\s*(https://[^)]+)\)", content):
        num = match.group(1) or "1"
        url = match.group(2).strip()
        gmail_links[f"Gmail {num}"] = url

    if unique_links or gmail_links:
        view = OpenInObsidianView(unique_links)
        for label, url in gmail_links.items():
            if len(view.children) < 5:
                view.add_item(discord.ui.Button(
                    label=label, url=url, style=discord.ButtonStyle.link
                ))
        return view
    return None


def _format_discord_content(content: str) -> str:
    """Discord表示用にメッセージを整形する（[[...]]削除など）"""
    # Obsidianリンク [[...]] を太字 **...** に置換して見やすくする
    display_content = re.sub(r"\[\[(.*?)\]\]", r"**\1**", content)
    # (Path: ...) も表示上は邪魔なので削除する
    display_content = re.sub(r"\(Path:.*?\)", "", display_content)
    display_content = re.sub(r"\(Gmail\d*:.*?\)", "", display_content).strip()
    return display_content


def _make_discord_file(content: str, filename: str) -> discord.File:
    """テキストコンテンツから discord.File を生成する。
    BytesIO(UTF-8) を使うことで StringIO による TypeError を防ぐ。
    """
    return discord.File(io.BytesIO(content.encode('utf-8')), filename=filename)


async def _send_long_message(interaction: discord.Interaction, content: str,
                              title: str = "Result", color: int = 0x00ff00):
    """2000文字を超える場合は Embed + ファイル添付で送信する。Obsidianボタンも付与。"""
    
    
    display_content = _format_discord_content(content)
    view = _create_obsidian_view(content)
    
    kwargs = {}
    if view:
        kwargs["view"] = view

    if len(display_content) <= 2000:
        await interaction.followup.send(display_content, **kwargs)
    else:
        # サマリー（最初の1500文字）を Embed で、全文を .md ファイルで添付
        summary = display_content[:1500] + "\n\n*... (full report attached)*"
        embed = discord.Embed(title=title, description=summary, color=color)
        file = _make_discord_file(content, f"{title}.md")
        await interaction.followup.send(embed=embed, file=file, **kwargs)


async def _send_with_fallback(interaction: discord.Interaction, result: str,
                              title: str = "Result", emoji: str = "📋",
                              embed: discord.Embed = None, file: discord.File = None):
    """interaction.followup + NotFound 時のチャンネル直接送信フォールバック。"""
    try:
        if embed or file:
            view = _create_obsidian_view(result)
            send_kwargs = {}
            if embed is not None:
                send_kwargs["embed"] = embed
            if file is not None:
                send_kwargs["file"] = file
            if view:
                send_kwargs["view"] = view
            await interaction.followup.send(**send_kwargs)
        else:
            await _send_long_message(interaction, result, title=title)
    except discord.NotFound:
        try:
            mention = f"{interaction.user.mention} {emoji} **{title}** (Interaction Timed Out)"
            if len(result) > 1900:
                fb_file = _make_discord_file(result, f"{title.replace(' ', '_')}.md")
                await interaction.channel.send(content=mention, file=fb_file)
            else:
                await interaction.channel.send(f"{mention}\n\n{result}")
        except Exception as e:
            print(f"[Discord] ❌ Failed to send fallback message: {e}")
    except Exception as e:
        print(f"[Discord] ❌ Failed to send result: {e}")


# ---------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------

@tree.command(name="chat", description="AIチャット (on: セッション開始, off: 終了, またはメッセージ)")
@app_commands.describe(message="on/off またはメッセージを入力")
async def cmd_chat(interaction: discord.Interaction, message: str = "on"):
    await bot_commands_chat.cmd_chat(interaction, message)


@tree.command(name="status", description="常駐スタック（Bot / Scheduler / Launcher）の稼働状況")
async def cmd_status(interaction: discord.Interaction):
    await bot_commands_admin.cmd_status(interaction)


@tree.command(name="restart", description="Bot / Scheduler を再起動する（新しいコードで上がり直す）")
@app_commands.describe(target="再起動する対象")
@app_commands.choices(target=[
    app_commands.Choice(name="Discord Bot", value="bot"),
    app_commands.Choice(name="Scheduler", value="scheduler"),
    app_commands.Choice(name="両方", value="both"),
])
async def cmd_restart(interaction: discord.Interaction, target: str = "bot"):
    await bot_commands_admin.cmd_restart(interaction, target)


@tree.command(name="news", description="今日のニュースを収集・保存")
async def cmd_news(interaction: discord.Interaction):
    await bot_commands_tools.cmd_news(interaction)


@tree.command(name="briefing", description="デイリーブリーフィング生成（Crypto + Vault更新の3項目要約）")
async def cmd_briefing(interaction: discord.Interaction):
    await bot_commands_tools.cmd_briefing(interaction)


@tree.command(name="vault_watch", description="Vault Insight Watcherを手動実行（変更ノートの関連チェック）")
async def cmd_vault_watch(interaction: discord.Interaction):
    await bot_commands_tools.cmd_vault_watch(interaction)


@tree.command(name="memory", description="長期記憶の現在の内容を表示（会話から毎晩自動抽出）")
async def cmd_memory(interaction: discord.Interaction):
    await bot_commands_tools.cmd_memory(interaction)


@tree.command(name="gmail_check", description="直近の重要メール要約")
@app_commands.describe(hours="何時間前まで遡るか（デフォルト: 12）")
async def cmd_gmail_check(interaction: discord.Interaction, hours: int = 12):
    await bot_commands_tools.cmd_gmail_check(interaction, hours)


@tree.command(name="crypto", description="仮想通貨のクオンツ分析")
@app_commands.describe(symbol="通貨ペア（例: BTC/USDT, ETH/USDT）")
async def cmd_crypto(interaction: discord.Interaction, symbol: str = "BTC/USDT"):
    await bot_commands_tools.cmd_crypto(interaction, symbol)


@tree.command(name="scan", description="主要20銘柄の一括スキャン")
async def cmd_scan(interaction: discord.Interaction):
    await bot_commands_tools.cmd_scan(interaction)


@tree.command(name="crypto-stats", description="予測成績ダッシュボード（種別勝率・期待値R・OPEN一覧）")
async def cmd_crypto_stats(interaction: discord.Interaction):
    await bot_commands_tools.cmd_crypto_stats(interaction)


@tree.command(name="watchlist", description="ウォッチャー監視状況（20銘柄変動・クールダウン・SL/TP距離）")
async def cmd_watchlist(interaction: discord.Interaction):
    await bot_commands_tools.cmd_watchlist(interaction)


@tree.command(name="xtrend", description="X(Twitter)のAIトレンドを収集・分析してObsidianに保存")
@app_commands.describe(min_likes="最低いいね数 (デフォルト: 500)")
async def cmd_xtrend(interaction: discord.Interaction, min_likes: int = 500):
    await bot_commands_tools.cmd_xtrend(interaction, min_likes)


@tree.command(name="xtrend-prompt", description="XTrend分析済み機能のClaude Code実装指示書を生成（先に/xtrendを実行）")
@app_commands.describe(index="機能番号 1始まり (デフォルト: 1)")
async def cmd_xtrend_prompt(interaction: discord.Interaction, index: int = 1):
    await bot_commands_tools.cmd_xtrend_prompt(interaction, index)


@tree.command(name="paper-radar", description="指定分野の注目論文を自動判別（arXiv×HF upvote×被引用速度）")
@app_commands.describe(
    field="分野プリセット or arXivクエリ (例: ai_agent, llm, rag / 空欄は.envの既定)",
    top_k="上位何件を出すか (デフォルト: 5)",
    days="何日以内の論文を対象にするか (デフォルト: 7)",
)
async def cmd_paper_radar(interaction: discord.Interaction, field: str = "",
                          top_k: int = 5, days: int = 7):
    await bot_commands_tools.cmd_paper_radar(interaction, field, top_k, days)


@tree.command(name="paper-digest", description="注目論文をdoclingでキャプチャ→要約→SNS/note下書きを生成")
@app_commands.describe(
    index="Paper Radar 候補の番号 1始まり (デフォルト: 1)",
    arxiv_id="arXiv IDを直接指定する場合 (例: 2401.12345)",
    mode="出力: sns / note / both / none",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Xスレッド下書き", value="sns"),
    app_commands.Choice(name="note記事下書き", value="note"),
    app_commands.Choice(name="両方", value="both"),
    app_commands.Choice(name="要約ノートのみ", value="none"),
])
async def cmd_paper_digest(interaction: discord.Interaction, index: int = 1,
                           arxiv_id: str = "", mode: str = "sns"):
    await bot_commands_tools.cmd_paper_digest(interaction, index, arxiv_id, mode)


@tree.command(name="skill-digest", description="Skill Radar候補を選んでSNS/note下書きを生成 (候補が無ければその場でSkill Radarも実行)")
@app_commands.describe(
    index="Skill Radar 候補の番号 1始まり (デフォルト: 1)",
    repo="候補一覧内のリポジトリを owner/name で直接指定する場合",
    mode="出力: sns / note / both / none",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Xティザー投稿下書き", value="sns"),
    app_commands.Choice(name="note記事下書き", value="note"),
    app_commands.Choice(name="両方", value="both"),
    app_commands.Choice(name="候補の中身を確認のみ (何も保存しない)", value="none"),
])
async def cmd_skill_digest(interaction: discord.Interaction, index: int = 1,
                           repo: str = "", mode: str = "sns"):
    await bot_commands_tools.cmd_skill_digest(interaction, index, repo, mode)


@tree.command(name="doc", description="PDF/Office文書を添付してdoclingで変換→Gemini要約→Vault保存 (SNS/note下書きも生成可)")
@app_commands.describe(
    attachment="変換したい文書 (PDF/DOCX/PPTX)",
    folder="保存先フォルダ (Vault相対パス, デフォルト: 30_Main)",
    mode="出力: sns / note / both / none",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Xティザー投稿下書き", value="sns"),
    app_commands.Choice(name="note記事下書き", value="note"),
    app_commands.Choice(name="両方", value="both"),
    app_commands.Choice(name="要約のみ (下書き無し)", value="none"),
])
async def cmd_doc(interaction: discord.Interaction, attachment: discord.Attachment,
                  folder: str = "30_Main", mode: str = "none"):
    await bot_commands_tools.cmd_doc(interaction, attachment, folder, mode)


@tree.command(name="shorts", description="長尺動画を添付してSNS用ショート動画(縦型9:16+字幕+BGM)を自動編集")
@app_commands.describe(
    attachment="編集元の長尺動画ファイル (mp4等)",
    target_seconds="ショート動画の目標尺(秒、デフォルト30)",
    topic_hint="ハイライト選定のヒント(空欄可、例: 'このプロダクトの一番の強みが伝わる部分')",
    bgm="BGM/効果音ファイル (添付、任意。無指定ならBGMなしで進行)",
    vertical_mode="縦型変換方式",
)
@app_commands.choices(vertical_mode=[
    app_commands.Choice(name="ぼかし背景パディング(コンテンツ非破壊)", value="pad"),
    app_commands.Choice(name="中央クロップ", value="crop"),
])
async def cmd_shorts(interaction: discord.Interaction, attachment: discord.Attachment,
                     target_seconds: int = 30, topic_hint: str = "",
                     bgm: discord.Attachment = None, vertical_mode: str = "pad"):
    await bot_commands_tools.cmd_shorts(interaction, attachment, target_seconds, topic_hint, bgm, vertical_mode)


@tree.command(name="browse", description="URLを開いてAIで情報抽出（JS動的ページ・SPAに対応）")
@app_commands.describe(
    url="取得するURL",
    task="AIへの抽出指示 (例: このページの料金プランを教えて)"
)
async def cmd_browse(interaction: discord.Interaction, url: str, task: str = "このページの主要な情報を要約して"):
    await bot_commands_tools.cmd_browse(interaction, url, task)


@tree.command(name="future", description="未来シナリオ分析: Gemini 10視点マルチレンズ推論をObsidianに保存")
@app_commands.describe(
    topic="調査トピック (日本語OK, 例: AIエージェントの未来)",
    depth="詳細度: quick(5ソース/速い) / standard / deep(15ソース/詳細)",
    uncensored="辛口診断を追加する (時間がかかります)",
)
@app_commands.choices(depth=[
    app_commands.Choice(name="quick (速い・5ソース)", value="quick"),
    app_commands.Choice(name="standard (標準・10ソース)", value="standard"),
    app_commands.Choice(name="deep (詳細・15ソース)", value="deep"),
])
async def cmd_future(interaction: discord.Interaction, topic: str,
                     depth: str = "standard", uncensored: bool = False):
    await bot_commands_tools.cmd_future(interaction, topic, depth, uncensored)


# 2026-02-28 の commands/ 分割リファクタで登録が漏れていた (ハンドラ側は無傷)。
# 本体は _dispatch_curate に一本化し、/chat curate と同一経路にしてある。
@tree.command(name="curate", description="AIがあなたの興味に基づいてトピックを提案します")
async def cmd_curate(interaction: discord.Interaction):
    await interaction.response.defer()
    await bot_commands_chat._dispatch_curate(interaction, "")
    await asyncio.to_thread(
        _log_to_daily_note, "command", "/curate", "Suggestions presented"
    )


@tree.command(
    name="setup",
    description="初回セットアップ: 興味のあるトピックを一括選択してナレッジベースを構築",
)
async def cmd_setup(interaction: discord.Interaction):
    await bot_commands_curator.cmd_setup(interaction)


@tree.command(name="topic", description="興味のあるトピックを直接指定して調査・記録します")
@app_commands.describe(query="調べたいキーワード（例: 古代ローマの建築技術）")
async def cmd_topic(interaction: discord.Interaction, query: str):
    await bot_commands_curator.cmd_topic(interaction, query)


# ---------------------------------------------------------
# Calendar / Task Commands
# ---------------------------------------------------------
# これらも 2026-02-28 のリファクタで登録が失われていた。
# 実装は commands/bot_commands_calendar.py に残っていたため委譲するだけでよい。

@tree.command(name="calendar", description="Google Calendarから予定を取得します")
@app_commands.describe(date="日付（today, tomorrow, 2026-02-15 など）")
async def cmd_calendar(interaction: discord.Interaction, date: str = "today"):
    await bot_commands_calendar.cmd_calendar(interaction, date)


@tree.command(name="add-event", description="Google Calendarに予定を追加します")
@app_commands.describe(
    title="予定のタイトル",
    start_time="開始時刻（例: 明日 14:00, 2026-02-15 10:00）",
    end_time="終了時刻（省略時は1時間後）",
)
async def cmd_add_event(interaction: discord.Interaction,
                        title: str, start_time: str, end_time: str = ""):
    await bot_commands_calendar.cmd_add_event(interaction, title, start_time, end_time)


@tree.command(name="tasks", description="Todoistのタスク一覧を表示します")
@app_commands.describe(filter="フィルター（today, overdue, week, all）")
async def cmd_tasks(interaction: discord.Interaction, filter: str = "today"):
    await bot_commands_calendar.cmd_tasks(interaction, filter)


@tree.command(name="remind", description="Todoistにリマインダー（タスク）を追加します")
@app_commands.describe(
    content="リマインド内容",
    due_date="期限（例: 明日, 2026-02-15, 毎日 21:00）",
)
async def cmd_remind(interaction: discord.Interaction,
                     content: str, due_date: str = ""):
    await bot_commands_calendar.cmd_remind(interaction, content, due_date)


@tree.command(name="agent", description="AIエージェントに自律的なタスクを依頼（検索・ファイル操作など）")
@app_commands.describe(goal="エージェントへの指示（例: 〇〇について調べてまとめて）")
async def cmd_agent(interaction: discord.Interaction, goal: str):
    await bot_commands_agent.cmd_agent(interaction, goal)


@tree.command(name="agent-stats", description="AIエージェントの実行統計と学習状況を表示")
async def cmd_agent_stats(interaction: discord.Interaction):
    await bot_commands_agent.cmd_agent_stats(interaction)


@tree.command(name="client-review", description="成果物を指定ペルソナ（顧客視点）で5軸評価する。")
@app_commands.describe(
    deliverable="評価対象の成果物テキスト、または [[ファイル名]] の形式でノートリンクを指定",
    job="当初の依頼内容（何を目的とした成果物か）",
    persona="ペルソナID（未指定時は other）例: writing, client_A"
)
async def cmd_client_review(interaction: discord.Interaction, deliverable: str, job: str, persona: str = "other"):
    await bot_commands_client.cmd_client_review(interaction, deliverable, job, persona)


@tree.command(name="persona-add", description="新しいクライアントペルソナを登録する。")
@app_commands.describe(
    client_id="一意のID（半角英数）", 
    label="表示名",
    industry="ベースとなる業種",
    extra_rules="追加のルール（改行やセミコロン区切り）",
    manual_path="マニュアルファイルのパス（TXT等）"
)
async def cmd_persona_add(interaction: discord.Interaction, client_id: str, label: str, industry: str, extra_rules: str = "", manual_path: str = ""):
    await bot_commands_client.cmd_persona_add(interaction, client_id, label, industry, extra_rules, manual_path)


@tree.command(name="persona-list", description="登録済みのペルソナ一覧を表示する。")
async def cmd_persona_list(interaction: discord.Interaction):
    await bot_commands_client.cmd_persona_list(interaction)


@tree.command(name="article", description="対話形式で自分らしい体験談記事を作成する")
async def cmd_article(interaction: discord.Interaction):
    await bot_commands_article.cmd_article(interaction)


@tree.command(name="idea", description="雑なアイデアを1文のままVaultに即保存する")
@app_commands.describe(text="思いついたことをそのまま（曖昧でOK）")
async def cmd_idea(interaction: discord.Interaction, text: str):
    await bot_commands_idea.cmd_idea(interaction, text)


@tree.command(name="brainstorm", description="AIと壁打ちしながらアイデアを育て、1枚のメモに要約する")
@app_commands.describe(text="育てたいアイデアの種（曖昧でOK。候補から00_inboxの既存アイデアも選べる）")
@app_commands.autocomplete(text=bot_commands_idea.idea_autocomplete)
async def cmd_brainstorm(interaction: discord.Interaction, text: str):
    await bot_commands_idea.cmd_brainstorm(interaction, text)


@tree.command(name="idea-relink", description="既存の完成アイデアノートについて、Vault内の関連ノートを再検索する")
@app_commands.describe(title="対象のアイデアノート（候補から選択）")
@app_commands.autocomplete(title=bot_commands_idea.permanent_idea_autocomplete)
async def cmd_idea_relink(interaction: discord.Interaction, title: str):
    await bot_commands_idea.cmd_idea_relink(interaction, title)
# ---------------------------------------------------------
# Webhook Listener (HTTP Shortcuts → Obsidian)
# ---------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    # 自分自身のメッセージは無視
    if message.author == bot.user:
        return

    # Webhook からのメッセージを検知してデイリーノートに記録
    if message.webhook_id is not None:
        if LOG_CHANNEL_ID and str(message.channel.id) == LOG_CHANNEL_ID:
            await asyncio.to_thread(
                _log_to_daily_note, "action", message.content
            )
            await message.add_reaction("✅")
            print(f"  📱 Webhook log saved: {message.content[:50]}")
        return

    # 許可ユーザー以外のメッセージは無視（webhook 経路は上のチャンネルIDゲートで制御済み）
    if not _is_allowed_user(message.author.id):
        return

    # --- 記事生成スレッドのメッセージ処理 ---
    if isinstance(message.channel, discord.Thread):
        await bot_commands_article.handle_article_reply(message)
        await bot_commands_idea.handle_brainstorm_reply(message)

    # --- Chat Session Handler ---
    await bot_commands_chat.handle_chat_message(message)


# ---------------------------------------------------------
# Events
# ---------------------------------------------------------

@tasks.loop(seconds=_watchdog.BEAT_INTERVAL)
async def _heartbeat_loop():
    _watchdog.beat(HEARTBEAT_FILE)


@bot.event
async def on_ready():
    print(f"[Discord] ✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[Discord] 🏰 Connected Guilds: {[g.id for g in bot.guilds]}")

    # on_ready は再接続でも発火するため多重起動をガードする
    if not _heartbeat_loop.is_running():
        _heartbeat_loop.start()

    # bot_commands_curator のヘルパー初期化
    bot_commands_curator.init_helpers(
        _log_to_daily_note, _create_obsidian_view, _format_discord_content,
        _send_long_message, _send_with_fallback
    )
    # bot_commands_tools のヘルパー初期化
    bot_commands_tools.init_helpers(
        _log_to_daily_note, _format_discord_content, _send_with_fallback
    )
    # bot_commands_agent のヘルパー初期化
    bot_commands_agent.init_helpers(
        _log_to_daily_note, _send_long_message, _send_with_fallback
    )
    # bot_commands_chat のヘルパー初期化
    bot_commands_chat.init_helpers(
        _log_to_daily_note, _send_long_message, _make_discord_file,
        _create_obsidian_view, _openai_client
    )
    # /restart が自分自身を閉じられるよう Client を渡す。
    # 認可リストの設定有無も渡す: 管理系コマンドは fail-open にできない
    # (未設定だと全ユーザーがサービスを停止できてしまう)
    bot_commands_admin.init_helpers(bot, lambda: bool(ALLOWED_USER_IDS))
    # 永続 View: 認可判定関数を注入し、bot 再起動をまたいで動作するよう登録
    bot_views.init_views(_is_allowed_user)
    bot.add_view(bot_views.BriefingDeepDiveView())
    bot.add_view(bot_views.TaskFailureView())
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"[Discord] 🔄 Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
        else:
            synced = await tree.sync()
            print(f"[Discord] 🔄 Synced {len(synced)} slash command(s) globally (may take up to 1h)")
    except Exception as e:
        print(f"[Discord] ❌ Failed to sync commands: {e}")
    print("[Discord] 🚀 Bot is ready!")

    # /restart で自分を閉じていた場合、その報告先へ完了を伝える。
    # (要求時の interaction はプロセスごと消えているのでファイル経由で引き継ぐ)
    await bot_commands_admin.announce_restart_complete(bot)


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def run_bot():
    """main.py から呼び出し可能なエントリーポイント"""
    # トークンの検査はここ (起動時) で行う。import 時ではない — 理由は
    # DISCORD_TOKEN の定義箇所のコメントを参照。
    # spawn 側でも先に見ることで、子プロセスを立ててから落ちる無駄を避ける。
    if not DISCORD_TOKEN:
        print("Error: DISCORD_BOT_TOKEN not found in .env")
        sys.exit(1)

    # Imported from main.py -> Spawn fresh process to avoid Session Closed error (global client reuse)
    if __name__ != "__main__":
        print(f"[Discord] 🚀 Spawning independent bot process via subprocess...")
        try:
            subprocess.run([sys.executable, os.path.abspath(__file__)], check=False)
        except KeyboardInterrupt:
            print("\n[Discord] 🛑 Broker stopped.")
        return

    # Standalone execution (or via subprocess)
    if not _acquire_pid_lock(PID_FILE):
        print(f"[Discord] ⚠️ Another bot instance is already running "
              f"(PID {_read_pid_owner(PID_FILE)}). Exiting.")
        sys.exit(0)  # 正常終了として扱う（ループ防止）
    print("[Discord] 🤖 Starting Antigravity Discord Bot...")
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        _release_pid_lock(PID_FILE)


if __name__ == "__main__":
    run_bot()
