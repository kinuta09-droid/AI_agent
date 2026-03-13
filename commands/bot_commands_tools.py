# -*- coding: utf-8 -*-
import discord
from discord import app_commands
import asyncio
import datetime
import io

# ツールをインポート
from tools.tool_web import tool_fetch_daily_news
from tools.tool_crypto import tool_analyze_crypto, tool_scan_crypto_market
from tools.tool_briefing_manager import tool_generate_briefing
from tools.tool_gmail_manager import get_gmail_summary as tool_gmail_check

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

# bot_discord から注入されるヘルパー
_log_to_daily_note = None
_format_discord_content = None
_send_with_fallback = None

def init_helpers(log_func, format_func, send_fallback_func):
    global _log_to_daily_note, _format_discord_content, _send_with_fallback
    _log_to_daily_note = log_func
    _format_discord_content = format_func
    _send_with_fallback = send_fallback_func

async def cmd_news(interaction: discord.Interaction):
    """今日のニュースを収集・保存"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_fetch_daily_news)
    except Exception as e:
        result = f"Error: {e}"

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
        result = await asyncio.to_thread(tool_generate_briefing)
    except Exception as e:
        result = f"Error: {e}"

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/briefing", result)
    
    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Daily Briefing", emoji="📋")
    else:
        await interaction.followup.send(result[:2000])

async def cmd_gmail_check(interaction: discord.Interaction, hours: int = 12):
    """直近の重要メール要約"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_gmail_check)
    except Exception as e:
        result = f"Error: {e}"

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
        result = f"Error: {e}"

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", f"/crypto {symbol}", result)

    display_content = _format_discord_content(result) if _format_discord_content else result
    embed = discord.Embed(
        title=f"Crypto Analysis: {symbol}",
        description=display_content[:1500] if len(display_content) > 1500 else display_content,
        color=0xf0b90b  # Binance yellow
    )
    clean = symbol.replace('/', '_')
    file = discord.File(
        io.StringIO(result),
        filename=f"Crypto_{clean}_{datetime.datetime.now().strftime('%Y-%m-%d')}.md"
    ) if len(result) > 1500 else None
    
    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Crypto Analysis", emoji="🪙", embed=embed, file=file)
    else:
        await interaction.followup.send(embed=embed, file=file)

async def cmd_scan(interaction: discord.Interaction):
    """主要20銘柄の一括スキャン"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_scan_crypto_market)
    except Exception as e:
        result = f"Error: {e}"

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", "/scan", result)

    display_content = _format_discord_content(result) if _format_discord_content else result
    embed = discord.Embed(
        title="Market Scan",
        description=display_content[:1500] if len(display_content) > 1500 else display_content,
        color=0x00bfff
    )
    file = discord.File(
        io.StringIO(result),
        filename=f"Market_Scan_{datetime.datetime.now().strftime('%Y-%m-%d')}.md"
    ) if len(result) > 1500 else None

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Market Scan", emoji="🔭", embed=embed, file=file)
    else:
        await interaction.followup.send(embed=embed, file=file)


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
        result = f"❌ XTrend Error: {e}"

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
            prompt_text = await asyncio.to_thread(generate_claude_prompt, feature, True)
            name = feature.get("feature_name", "?")
            result = f"**実装指示書生成完了: {name}**\n\n{prompt_text}"
    except Exception as e:
        result = f"❌ XTrend Prompt Error: {e}"

    if _send_with_fallback:
        file = discord.File(
            io.StringIO(result),
            filename=f"xtrend_prompt_{index}_{datetime.date.today().isoformat()}.md"
        ) if len(result) > 1800 else None
        await _send_with_fallback(interaction, result, title="実装指示書", emoji="📝", file=file)
    else:
        await interaction.followup.send(result[:2000])


async def cmd_browse(interaction: discord.Interaction, url: str, task: str = "このページの主要な情報を要約して"):
    """URLを開いてAIで情報抽出（JS動的ページ対応）"""
    await interaction.response.defer()
    try:
        browse_and_extract = _import_browser()
        result = await asyncio.to_thread(browse_and_extract, url, task)
    except Exception as e:
        result = f"❌ Browse Error: {e}"

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Browse & Extract", emoji="🌐")
    else:
        await interaction.followup.send(result[:2000])


async def cmd_future(interaction: discord.Interaction, topic: str):
    """英語圏10エージェント並列リサーチ → 日本語レポート+辛口診断"""
    await interaction.response.defer()
    try:
        run_future_search = _import_future_search()
        result = await asyncio.to_thread(run_future_search, topic)
    except Exception as e:
        result = f"❌ FutureSearch Error: {e}"

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Future Search", emoji="🔮")
    else:
        await interaction.followup.send(result[:2000])
