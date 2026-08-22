# -*- coding: utf-8 -*-
import discord
import asyncio
import datetime
import os
import re
import tempfile

# ツールをインポート
from tools.tool_web import tool_fetch_daily_news
from tools.tool_crypto import tool_analyze_crypto, tool_scan_crypto_market
from tools.tool_briefing_manager import tool_generate_briefing
from tools.tool_gmail_manager import get_gmail_summary as tool_gmail_check
from tools.tool_vault_watcher import check_vault_insights

from lib.bot_errors import log_command_error
from lib.engagement_log import log_event
from lib.discord_response import (
    format_discord_content as _format_discord_content,
    make_discord_file as _make_discord_file,
    send_with_fallback as _send_with_fallback,
)

# 新機能ツール (lazy import で依存関係エラーを bot 起動時に波及させない)
def _import_xtrend():
    from tools.tool_xtrend import run, generate_claude_prompt, _load_feature_by_index
    return run, generate_claude_prompt, _load_feature_by_index

def _import_browser():
    from tools.tool_browser_use import browse_and_extract
    return browse_and_extract

def _import_future_search():
    from tools.tool_future_search import run_future_search
    return run_future_search

# bot_discord から注入されるヘルパー (Discord 応答系は lib.discord_response を直接 import 済み)
_log_to_daily_note = None

def init_helpers(log_func):
    global _log_to_daily_note
    _log_to_daily_note = log_func

async def cmd_news(interaction: discord.Interaction):
    """今日のニュースを収集・保存"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_fetch_daily_news)
    except Exception as e:
        result = log_command_error("/news", e)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/news", result)
    
    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Daily News", emoji="📰")
    else:
        await interaction.followup.send(result[:2000])

async def cmd_briefing(interaction: discord.Interaction):
    """デイリーブリーフィング生成"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_generate_briefing, True)
    except Exception as e:
        result = log_command_error("/briefing", e)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/briefing", result)

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Daily Briefing", emoji="📋")
    else:
        await interaction.followup.send(result[:2000])

    # 深掘りリサーチボタン (永続 View: 本体メッセージとは別送信)
    from commands.bot_views import BriefingDeepDiveView, _load_top_features
    features = _load_top_features(top_n=3)
    if features:
        names = [f.get("feature_name", "?") for f in features]
        await interaction.channel.send("🔍 深掘りリサーチ:", view=BriefingDeepDiveView(names))


async def cmd_vault_watch(interaction: discord.Interaction):
    """Vault Insight Watcher を手動実行（開発中の動作確認用）"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(check_vault_insights)
    except Exception as e:
        result = log_command_error("/vault_watch", e)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/vault_watch", result)

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Vault Insight Watcher", emoji="🔎")
    else:
        await interaction.followup.send(result[:2000])


async def cmd_memory(interaction: discord.Interaction):
    """長期記憶 (User_Memory.md) の現在の内容を表示する (閲覧のみ)。"""
    await interaction.response.defer()
    try:
        from tools.tool_user_memory import get_memory_block
        block = get_memory_block(max_chars=3500)
        if not block:
            result = ("🧠 記憶はまだありません。毎晩02:45にChatlogから自動生成されます。\n"
                      "(会話を重ねると、好み・目標・環境などが自動的に蓄積されていきます)")
        else:
            result = f"🧠 **現在の長期記憶** (編集は Obsidian で直接: `00_System/User_Memory.md`)\n\n{block}"
    except Exception as e:
        result = log_command_error("/memory", e)

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="User Memory", emoji="🧠")
    else:
        await interaction.followup.send(result[:2000])


async def cmd_gmail_check(interaction: discord.Interaction, hours: int = 12):
    """直近の重要メール要約"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_gmail_check)
    except Exception as e:
        result = log_command_error("/gmail_check", e)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", f"/gmail_check {hours}h", result)
    
    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Gmail Check", emoji="📧")
    else:
        await interaction.followup.send(result[:2000])

async def cmd_crypto(interaction: discord.Interaction, symbol: str = "BTC/USDT"):
    """仮想通貨のクオンツ分析"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_analyze_crypto, symbol)
    except Exception as e:
        result = log_command_error(f"/crypto {symbol}", e)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", f"/crypto {symbol}", result)

    display_content = _format_discord_content(result) if _format_discord_content else result
    embed = discord.Embed(
        title=f"Crypto Analysis: {symbol}",
        description=display_content[:1500] if len(display_content) > 1500 else display_content,
        color=0xf0b90b  # Binance yellow
    )
    clean = symbol.replace('/', '_')
    file = _make_discord_file(result, f"Crypto_{clean}_{datetime.datetime.now().strftime('%Y-%m-%d')}.md") if len(result) > 1500 else None
    
    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Crypto Analysis", emoji="🪙", embed=embed, file=file)
    else:
        send_kwargs = {"embed": embed}
        if file is not None:
            send_kwargs["file"] = file
        await interaction.followup.send(**send_kwargs)

async def cmd_scan(interaction: discord.Interaction):
    """主要20銘柄の一括スキャン"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_scan_crypto_market)
    except Exception as e:
        result = log_command_error("/scan", e)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/scan", result)

    display_content = _format_discord_content(result) if _format_discord_content else result
    embed = discord.Embed(
        title="Market Scan",
        description=display_content[:1500] if len(display_content) > 1500 else display_content,
        color=0x00bfff
    )
    file = _make_discord_file(result, f"Market_Scan_{datetime.datetime.now().strftime('%Y-%m-%d')}.md") if len(result) > 1500 else None

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Market Scan", emoji="🔭", embed=embed, file=file)
    else:
        send_kwargs = {"embed": embed}
        if file is not None:
            send_kwargs["file"] = file
        await interaction.followup.send(**send_kwargs)


async def cmd_crypto_stats(interaction: discord.Interaction):
    """予測成績ダッシュボード (trade_log集計・読み取り専用)"""
    await interaction.response.defer()
    try:
        from tools.tool_crypto_stats import tool_crypto_stats
        result = await asyncio.to_thread(tool_crypto_stats)
    except Exception as e:
        result = log_command_error("/crypto-stats", e)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/crypto-stats", result)

    display_content = _format_discord_content(result) if _format_discord_content else result
    embed = discord.Embed(
        title="Crypto Stats",
        description=display_content[:1500] if len(display_content) > 1500 else display_content,
        color=0xf0b90b  # Binance yellow
    )
    file = _make_discord_file(result, f"Crypto_Stats_{datetime.datetime.now().strftime('%Y-%m-%d')}.md") if len(result) > 1500 else None

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Crypto Stats", emoji="📊", embed=embed, file=file)
    else:
        send_kwargs = {"embed": embed}
        if file is not None:
            send_kwargs["file"] = file
        await interaction.followup.send(**send_kwargs)


async def cmd_watchlist(interaction: discord.Interaction):
    """ウォッチャー監視状況 (20銘柄変動・クールダウン・SL/TP距離)"""
    await interaction.response.defer()
    try:
        from tools.tool_crypto_watcher import tool_watchlist_status
        result = await asyncio.to_thread(tool_watchlist_status)
    except Exception as e:
        result = log_command_error("/watchlist", e)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/watchlist", result)

    display_content = _format_discord_content(result) if _format_discord_content else result
    embed = discord.Embed(
        title="Crypto Watchlist",
        description=display_content[:1500] if len(display_content) > 1500 else display_content,
        color=0x00bfff
    )
    file = _make_discord_file(result, f"Watchlist_{datetime.datetime.now().strftime('%Y-%m-%d')}.md") if len(result) > 1500 else None

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Crypto Watchlist", emoji="👀", embed=embed, file=file)
    else:
        send_kwargs = {"embed": embed}
        if file is not None:
            send_kwargs["file"] = file
        await interaction.followup.send(**send_kwargs)


# ---------------------------------------------------------
# 新機能コマンド (XTrend / BrowserUse / FutureSearch)
# ---------------------------------------------------------

async def cmd_xtrend(interaction: discord.Interaction, min_likes: int = 500):
    """X(Twitter) AIトレンド収集・分析 → Obsidian保存"""
    await interaction.response.defer()
    try:
        run, _, _ = _import_xtrend()
        features = await asyncio.to_thread(run, None, min_likes)
        if not features:
            result = "⚠️ 投稿が取得できませんでした。クエリまたは閾値を確認してください。"
        else:
            today = datetime.date.today().isoformat()
            lines = [f"**X Trend Analysis — {today}** ({len(features)} features)\n"]
            for i, f in enumerate(features[:5], 1):
                arms = f.get("arms_affinity", 0)
                diff = f.get("difficulty", 0)
                lines.append(
                    f"`{i:02d}` **{f.get('feature_name','?')}** "
                    f"| 難易度{'★'*diff}{'☆'*(5-diff)} "
                    f"| ARMS{'★'*arms}{'☆'*(5-arms)}\n"
                    f"　　{f.get('summary','')}"
                )
            if len(features) > 5:
                lines.append(f"\n...他 {len(features)-5} 件 (Obsidianに全件保存済み)")
            result = "\n".join(lines)
    except Exception as e:
        result = log_command_error("/xtrend", e, "❌ XTrend Error")

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="X Trend", emoji="🔥")
    else:
        await interaction.followup.send(result[:2000])


async def cmd_xtrend_prompt(interaction: discord.Interaction, index: int = 1):
    """XTrend分析済み機能の Claude Code 実装指示書を生成"""
    await interaction.response.defer()
    try:
        _, generate_claude_prompt, _load_feature_by_index = _import_xtrend()
        feature = await asyncio.to_thread(_load_feature_by_index, index)
        if not feature:
            result = f"⚠️ Feature {index} が見つかりません。先に `/xtrend` を実行してください。"
        else:
            # 項目9 (収集クエリの動的チューニング) Phase 0: どのクエリの候補が
            # 実際に深掘りされたかを記録するだけ。自動反映はしない。
            log_event("click", item=feature.get("feature_name", ""), source="xtrend",
                      meta={"source_query": feature.get("source_query", "")})
            prompt_text = await asyncio.to_thread(generate_claude_prompt, feature, True)
            name = feature.get("feature_name", "?")
            result = f"**実装指示書生成完了: {name}**\n\n{prompt_text}"
    except Exception as e:
        result = log_command_error("/xtrend-prompt", e, "❌ XTrend Prompt Error")

    if _send_with_fallback:
        file = _make_discord_file(result, f"xtrend_prompt_{index}_{datetime.date.today().isoformat()}.md") if len(result) > 1800 else None
        await _send_with_fallback(interaction, result, title="実装指示書", emoji="📝", file=file)
    else:
        await interaction.followup.send(result[:2000])


def _import_paper_radar():
    from tools.tool_paper_radar import run_paper_radar
    return run_paper_radar


def _import_paper_digest():
    from tools.tool_paper_digest import run_paper_digest
    return run_paper_digest


async def cmd_paper_radar(interaction: discord.Interaction, field: str = "",
                          top_k: int = 5, days: int = 7):
    """指定分野の注目論文を判別して Obsidian に保存"""
    await interaction.response.defer()
    try:
        run_paper_radar = _import_paper_radar()
        result = await asyncio.to_thread(run_paper_radar, field, top_k, days)
    except Exception as e:
        result = log_command_error("/paper-radar", e, "❌ Paper Radar Error")

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/paper-radar", result)

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Paper Radar", emoji="📡")
    else:
        await interaction.followup.send(result[:2000])


async def cmd_paper_digest(interaction: discord.Interaction, index: int = 1,
                           arxiv_id: str = "", mode: str = "sns"):
    """注目論文をキャプチャ・要約し、SNS/note の下書きまで生成する"""
    # docling の変換 + Gemini 2回で数分かかりうる。defer を先に返さないと
    # interaction token が3秒で切れる (skill discord-bot-patterns)。
    await interaction.response.defer()
    try:
        run_paper_digest = _import_paper_digest()
        result = await asyncio.to_thread(run_paper_digest, index, arxiv_id, "", mode)
    except Exception as e:
        result = log_command_error("/paper-digest", e, "❌ Paper Digest Error")

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/paper-digest", result)

    if _send_with_fallback:
        file = _make_discord_file(
            result, f"paper_digest_{datetime.date.today().isoformat()}.md"
        ) if len(result) > 1800 else None
        await _send_with_fallback(interaction, result, title="Paper Digest", emoji="📄", file=file)
    else:
        await interaction.followup.send(result[:2000])


def _import_skill_digest():
    from tools.tool_skill_digest import run_skill_digest
    return run_skill_digest


async def cmd_skill_digest(interaction: discord.Interaction, index: int = 1,
                           repo: str = "", mode: str = "sns"):
    """Skill Radar 候補を選んで SNS/note の下書きまで生成する"""
    # 候補が無ければその場で Skill Radar (GitHub検索+Gemini適合度判定) を回すため
    # 数十秒かかりうる。defer を先に返さないと interaction token が3秒で切れる
    # (skill discord-bot-patterns)。
    await interaction.response.defer()
    try:
        run_skill_digest = _import_skill_digest()
        result = await asyncio.to_thread(run_skill_digest, index, repo, mode)
    except Exception as e:
        result = log_command_error("/skill-digest", e, "❌ Skill Digest Error")

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/skill-digest", result)

    if _send_with_fallback:
        file = _make_discord_file(
            result, f"skill_digest_{datetime.date.today().isoformat()}.md"
        ) if len(result) > 1800 else None
        await _send_with_fallback(interaction, result, title="Skill Digest", emoji="🛰️", file=file)
    else:
        await interaction.followup.send(result[:2000])


def _import_document():
    from tools.tool_document import MAX_DOCUMENT_SIZE_BYTES, tool_import_document
    return tool_import_document, MAX_DOCUMENT_SIZE_BYTES


async def cmd_doc(interaction: discord.Interaction, attachment: discord.Attachment,
                  folder: str = "30_Main", mode: str = "none"):
    """添付PDF/Office文書をdoclingで変換→Gemini要約→Vault保存する (mode指定でSNS/note下書きも生成)"""
    # docling は数十秒〜数分かかりうる。3秒で切れる interaction token より
    # 先に defer() を返す (skill discord-bot-patterns)。
    await interaction.response.defer()

    tool_import_document, max_size = _import_document()

    # ダウンロード前の一次防波堤 (Discordのメタデータのみ・I/Oなし)。
    # 権威あるサイズ/ページ数チェックは tool_import_document 内部の
    # check_document_caps 一箇所だけに置き、ここでは重複させない。
    if attachment.size > max_size:
        size_mb, cap_mb = attachment.size / 1_048_576, max_size / 1_048_576
        result = f"Error: 添付ファイルが上限を超えています ({size_mb:.1f}MB > {cap_mb:.0f}MB)"
        await _send_with_fallback(interaction, result, title="Document Import", emoji="📄")
        return

    tmp_dir = tempfile.mkdtemp(prefix="arms_doc_")
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', attachment.filename) or "document"
    temp_path = os.path.join(tmp_dir, safe_name)
    try:
        await attachment.save(temp_path)
        result = await asyncio.to_thread(tool_import_document, temp_path, folder, mode)
    except Exception as e:
        result = log_command_error("/doc", e, "❌ Document Import Error")
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", f"/doc {attachment.filename}", result)

    if _send_with_fallback:
        file = _make_discord_file(
            result, f"doc_import_{datetime.date.today().isoformat()}.md"
        ) if len(result) > 1800 else None
        await _send_with_fallback(interaction, result, title="Document Import", emoji="📄", file=file)
    else:
        await interaction.followup.send(result[:2000])


DISCORD_ATTACHMENT_LIMIT_BYTES = 25 * 1024 * 1024
MAX_SHORTS_INPUT_SIZE_BYTES = 500 * 1024 * 1024

_SHORTS_VIDEO_PATH_RE = re.compile(r"ショート動画を生成しました: (.+)$", re.MULTILINE)


def _import_shorts_editor():
    from tools.tool_shorts_editor import tool_shorts_from_video
    return tool_shorts_from_video


async def cmd_shorts(interaction: discord.Interaction, attachment: discord.Attachment,
                     target_seconds: int = 30, topic_hint: str = "",
                     bgm: discord.Attachment = None, vertical_mode: str = "pad"):
    """添付動画からSNSショート動画(縦型9:16+字幕+BGM)を自動編集する"""
    # ffmpeg+Whisper+Geminiで数十秒〜数分かかりうる。3秒で切れるinteraction tokenより先にdefer
    await interaction.response.defer()

    if attachment.size > MAX_SHORTS_INPUT_SIZE_BYTES:
        size_mb, cap_mb = attachment.size / 1_048_576, MAX_SHORTS_INPUT_SIZE_BYTES / 1_048_576
        result = f"Error: 添付動画が上限を超えています ({size_mb:.1f}MB > {cap_mb:.0f}MB)"
        await _send_with_fallback(interaction, result, title="Shorts Editor", emoji="🎬")
        return

    tool_shorts_from_video = _import_shorts_editor()

    tmp_dir = tempfile.mkdtemp(prefix="arms_shorts_")
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', attachment.filename) or "video.mp4"
    video_path = os.path.join(tmp_dir, safe_name)
    bgm_path = None
    try:
        await attachment.save(video_path)
        if bgm is not None:
            bgm_safe_name = re.sub(r'[\\/:*?"<>|]', '_', bgm.filename) or "bgm.mp3"
            bgm_path = os.path.join(tmp_dir, bgm_safe_name)
            await bgm.save(bgm_path)

        result = await asyncio.to_thread(
            tool_shorts_from_video, video_path, target_seconds, topic_hint or None, bgm_path, vertical_mode
        )
    except Exception as e:
        result = log_command_error("/shorts", e, "❌ Shorts Editor Error")
    finally:
        for p in (video_path, bgm_path):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", f"/shorts {attachment.filename}", result)

    # 成功時のみ本文に生成済み動画パスが含まれる。25MB超ならVaultパス案内のみで添付は諦める。
    file = None
    m = _SHORTS_VIDEO_PATH_RE.search(result)
    if m:
        out_video_path = m.group(1).strip()
        if os.path.exists(out_video_path) and os.path.getsize(out_video_path) <= DISCORD_ATTACHMENT_LIMIT_BYTES:
            file = discord.File(out_video_path, filename=os.path.basename(out_video_path))
        else:
            result += "\n\n⚠️ 動画がDiscordの添付上限(25MB)を超えているため添付できません。上記Vaultパスを直接開いてください。"

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Shorts Editor", emoji="🎬", file=file)
    else:
        await interaction.followup.send(result[:2000])


async def cmd_browse(interaction: discord.Interaction, url: str, task: str = "このページの主要な情報を要約して"):
    """URLを開いてAIで情報抽出（JS動的ページ対応）"""
    await interaction.response.defer()
    try:
        browse_and_extract = _import_browser()
        result = await asyncio.to_thread(browse_and_extract, url, task)
    except Exception as e:
        result = log_command_error(f"/browse {url}", e, "❌ Browse Error")

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Browse & Extract", emoji="🌐")
    else:
        await interaction.followup.send(result[:2000])


async def cmd_future(interaction: discord.Interaction, topic: str,
                     depth: str = "standard", uncensored: bool = False):
    """未来シナリオ分析: Gemini 10視点マルチレンズ推論 (英訳・個人化・進捗表示付き)"""
    await interaction.response.defer()

    # ライブ進捗メッセージ (to_thread実行中にスレッドから安全に更新)
    progress_msg = await interaction.followup.send(f"🔮 FutureSearch 開始: **{topic}** (depth={depth})")
    loop = asyncio.get_running_loop()

    def progress_cb(text: str):
        # 別スレッドからイベントループへ安全にコルーチンを投げる
        asyncio.run_coroutine_threadsafe(
            progress_msg.edit(content=f"🔮 **{topic}**\n{text}"), loop
        )

    try:
        run_future_search = _import_future_search()
        result = await asyncio.to_thread(
            run_future_search, topic, depth, uncensored, progress_cb
        )
    except Exception as e:
        result = log_command_error(f"/future {topic}", e, "❌ FutureSearch Error")

    if _send_with_fallback:
        file = _make_discord_file(result, f"future_{datetime.date.today().isoformat()}.md") if len(result) > 1800 else None
        await _send_with_fallback(interaction, result, title="Future Search", emoji="🔮", file=file)
    else:
        await interaction.followup.send(result[:2000])
