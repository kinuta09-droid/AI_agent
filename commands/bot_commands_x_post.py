# -*- coding: utf-8 -*-
"""
/x-post コマンド。昇格済み(status=completed)の記事プロジェクトからX投稿文の
下書きを生成し、承認キュー(commands.bot_views.XPostApprovalView)へ流す。

投稿・承認・二重投稿防止の実処理は lib/x_post_store.py (状態管理) と
commands/bot_views.py (ボタンコールバック) が担う。ここは下書き生成と
初回表示だけを担当する薄いコマンド層。
"""
import asyncio

import discord
from discord import app_commands

from tools.tool_article_project import list_projects, generate_x_post_draft
from lib import x_post_store
from commands.bot_views import XPostApprovalView, format_xpost_message


async def x_post_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """昇格済み(status=completed)の記事プロジェクトを候補に出す。"""
    try:
        projects = await asyncio.to_thread(list_projects, ["completed"])
    except Exception:
        return []
    current_lower = current.lower()
    choices = []
    for p in projects:
        goal = p["frontmatter"].get("goal") or p["filename"]
        if current_lower in goal.lower() or current_lower in p["filename"].lower():
            choices.append(app_commands.Choice(name=f"📄 {goal}"[:100], value=p["filename"]))
    return choices[:25]


async def cmd_x_post(interaction: discord.Interaction, note: str, url: str = None):
    await interaction.response.defer()

    draft, error = await asyncio.to_thread(generate_x_post_draft, note, url)
    if error:
        await interaction.followup.send(f"❌ {error}")
        return

    slot, _post_id = await asyncio.to_thread(
        x_post_store.allocate_slot, draft["text"], draft["source_note"]
    )
    if slot is None:
        await interaction.followup.send(
            "⚠️ 進行中の投稿が上限に達しています。先に既存の投稿を承認/却下してください。"
        )
        return

    # allocate_slot は status=draft で登録する。Discordへの表示(下の followup.send)が
    # 成功して初めて pending_approval へ進める — 表示に失敗した場合はdraftのまま残り、
    # 「pending_approvalなのにボタンが存在しない」不整合を避けられる。
    x_post_store.try_transition(slot, "draft", "pending_approval")
    entry = x_post_store.get_slot(slot)
    await interaction.followup.send(
        content=format_xpost_message(slot, entry), view=XPostApprovalView(slots=[slot])
    )
