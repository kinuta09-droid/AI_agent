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
from tools.tool_gmail_manager import tool_gmail_check

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
        result = await asyncio.to_thread(tool_gmail_check, hours)
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
