# -*- coding: utf-8 -*-
import discord
from discord import app_commands
import asyncio

# tools.tool_calendar からツールをインポート
from tools.tool_calendar import (
    tool_get_calendar,
    tool_add_event,
    tool_create_task,
    tool_get_tasks,
    tool_sync_to_daily_note,
)

from lib.bot_errors import log_command_error

async def cmd_calendar(interaction: discord.Interaction, date: str = "today"):
    """Google Calendarから予定を取得します"""
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_get_calendar, date)
    except Exception as e:
        result = log_command_error(f"/calendar {date}", e)

    embed = discord.Embed(
        title="📅 Calendar",
        description=result[:2000],
        color=0x4285F4,
    )
    await interaction.followup.send(embed=embed)


async def cmd_add_event(interaction: discord.Interaction,
                        title: str, start_time: str, end_time: str = ""):
    """Google Calendarに予定を追加します"""
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_add_event, title, start_time, end_time)
    except Exception as e:
        result = log_command_error("/add-event", e)

    await interaction.followup.send(result)


async def cmd_tasks(interaction: discord.Interaction, filter: str = "today"):
    """Todoistのタスク一覧を表示します"""
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_get_tasks, filter)
    except Exception as e:
        result = log_command_error(f"/tasks {filter}", e)

    embed = discord.Embed(
        title="✅ Tasks",
        description=result[:2000],
        color=0xE44332,
    )
    await interaction.followup.send(embed=embed)


async def cmd_remind(interaction: discord.Interaction,
                     content: str, due_date: str = ""):
    """Todoistにリマインダー（タスク）を追加します"""
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(tool_create_task, content, due_date, 1)
    except Exception as e:
        result = log_command_error("/remind", e)

    await interaction.followup.send(result)
