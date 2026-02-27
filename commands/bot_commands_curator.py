# -*- coding: utf-8 -*-
import discord
from discord import app_commands
import asyncio
import datetime

# tools.tool_curator からツールをインポート
from tools.tool_curator import (
    read_main_context,
    get_curation_suggestions,
    search_and_save_topic,
    update_user_profile,
    fetch_external_trends,
)

# ---------------------------------------------------------
# Constants & Helpers (Originally from bot_discord.py)
# ---------------------------------------------------------

_BRANCH_LABEL = {
    "deepen": "🔬 深化", "expand": "🌐 水平展開", "jump": "🚀 飛躍",
    "consistent": "🔬 安定", "bridge": "🌐 橋渡し", "entropy": "🚀 飛躍",
}
_BRANCH_ICON = {
    "deepen": "🔬", "expand": "🌐", "jump": "🚀",
    "consistent": "🔬", "bridge": "🌐", "entropy": "🚀",
}

# bot_discord から利用するヘルパー（後ほど bot_utils 等に整理検討）
# 実行時に循環参照を避けるため、各関数内で動的に参照するか、引数で渡す形にする
_log_to_daily_note = None
_create_obsidian_view = None
_format_discord_content = None
_send_long_message = None
_send_with_fallback = None

def init_helpers(log_func, view_func, format_func, send_long_func, send_fallback_func):
    global _log_to_daily_note, _create_obsidian_view, _format_discord_content, _send_long_message, _send_with_fallback
    _log_to_daily_note = log_func
    _create_obsidian_view = view_func
    _format_discord_content = format_func
    _send_long_message = send_long_func
    _send_with_fallback = send_fallback_func

def _build_curate_embed(suggestions: dict, reject_count: int = 0) -> discord.Embed:
    """トピック提案の Embed を生成する。"""
    topics = suggestions.get("topics", [])
    ab = suggestions.get("ab_question", {})
    title = suggestions.get("title", "🔭 Antigravity Curator -- 今日の探索トピック")
    
    embed = discord.Embed(
        title=title,
        description="気になるトピックのボタンを押すと、AIが調査してナレッジノートを作成します。",
        color=0x7B68EE,
    )
    for i, topic in enumerate(topics[:3], 1):
        branch = topic.get("branch", "")
        label = _BRANCH_LABEL.get(branch, "📚")
        source = topic.get("source_note", "")
        source_text = f" ← [[{source}]]" if source else ""
        reasoning = topic.get("reasoning", "")
        reasoning_text = f"\n💡 *{reasoning[:100]}*" if (branch == "entropy" and reasoning) else ""
        embed.add_field(
            name=f"{i}. [{label}] {topic['title']}",
            value=f"{topic.get('description', '')}{source_text}{reasoning_text}",
            inline=False,
        )

    if ab:
        embed.add_field(
            name=f"💭 {ab.get('question', 'あなたの好みは？')}",
            value=f"**A**: {ab.get('option_a', '?')}\n**B**: {ab.get('option_b', '?')}",
            inline=False,
        )

    return embed


class CurateView(discord.ui.View):
    """Interactive view for /curate with Deep Refinement Loop."""

    def __init__(self, suggestions: dict, user_id: int,
                 context: str = "", rejected_topics: list = None,
                 reject_count: int = 0):
        super().__init__(timeout=300)
        self.suggestions = suggestions
        self.user_id = user_id
        self.context = context
        self.rejected_topics = rejected_topics or []
        self.reject_count = reject_count
        self.topics = suggestions.get("topics", [])
        self.ab_question = suggestions.get("ab_question", {})

        # トピックボタン（Row 0）
        for i, topic in enumerate(self.topics[:3]):
            branch = topic.get("branch", "")
            icon = _BRANCH_ICON.get(branch, "📚")
            btn = discord.ui.Button(
                label=f"{icon} {topic['title']}"[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"topic_{i}",
                row=0,
            )
            btn.callback = self._make_topic_callback(i)
            self.add_item(btn)

        # A/B ボタン（Row 1）
        if self.ab_question:
            btn_a = discord.ui.Button(
                label=f"A: {self.ab_question.get('option_a', '?')}"[:80],
                style=discord.ButtonStyle.success,
                custom_id="ab_a",
                row=1,
            )
            btn_a.callback = self._make_ab_callback("option_a")
            self.add_item(btn_a)

            btn_b = discord.ui.Button(
                label=f"B: {self.ab_question.get('option_b', '?')}"[:80],
                style=discord.ButtonStyle.success,
                custom_id="ab_b",
                row=1,
            )
            btn_b.callback = self._make_ab_callback("option_b")
            self.add_item(btn_b)

        # 再提案ボタン（Row 2）
        skip_label = "🔄 別の提案を見る" if reject_count == 0 else f"🔄 もっと別の提案 ({reject_count}回目)"
        btn_skip = discord.ui.Button(
            label=skip_label[:80],
            style=discord.ButtonStyle.secondary,
            custom_id="skip",
            row=2,
        )
        btn_skip.callback = self._refinement_callback
        self.add_item(btn_skip)

        # 終了ボタン（Row 2）
        btn_end = discord.ui.Button(
            label="終了",
            style=discord.ButtonStyle.danger,
            custom_id="end",
            row=2,
        )
        btn_end.callback = self._end_callback
        self.add_item(btn_end)

    def _make_topic_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "このボタンは操作できません。", ephemeral=True
                )
                return

            topic = self.topics[index]
            await interaction.response.defer()

            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(view=self)

            branch = topic.get("branch", "")
            icon = _BRANCH_ICON.get(branch, "🔍")
            await interaction.followup.send(
                f"{icon} **{topic['title']}** を調査中..."
            )
            result = await asyncio.to_thread(
                search_and_save_topic,
                topic["title"],
                topic.get("search_query", ""),
                topic.get("source_note", ""),
                topic.get("branch", ""),
                topic.get("reasoning", ""),
            )
            if _log_to_daily_note:
                await asyncio.to_thread(
                    _log_to_daily_note, "command", f"/curate -> {topic['title']}", result
                )
            
            view = _create_obsidian_view(result) if _create_obsidian_view else None
            display_result = _format_discord_content(result) if _format_discord_content else result
            
            kwargs = {}
            if view:
                kwargs["view"] = view
            
            await interaction.followup.send(f"✅ {display_result}", **kwargs)

        return callback

    def _make_ab_callback(self, option_key: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "このボタンは操作できません。", ephemeral=True
                )
                return

            answer = self.ab_question.get(option_key, "?")
            question = self.ab_question.get("question", "?")

            await interaction.response.defer(ephemeral=True)
            await asyncio.to_thread(update_user_profile, question, answer)
            await interaction.followup.send(
                f"✅ 好みを記録しました: {answer}", ephemeral=True
            )

        return callback

    async def _refinement_callback(self, interaction: discord.Interaction):
        """Deep Refinement Loop: 拒否データを蓄積して再提案する。"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "このボタンは操作できません。", ephemeral=True
            )
            return

        new_rejected = self.rejected_topics.copy()
        for t in self.topics:
            new_rejected.append(t.get("title", ""))
        new_reject_count = self.reject_count + 1

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="🤔 **別の切り口を考え中...**", view=self
        )

        try:
            ctx = self.context
            if not ctx:
                ctx = await asyncio.to_thread(read_main_context)

            trends = await asyncio.to_thread(fetch_external_trends)
            new_suggestions = await asyncio.to_thread(
                get_curation_suggestions, ctx, "normal",
                new_rejected, new_reject_count, trends,
            )

            new_topics = new_suggestions.get("topics", [])
            if not new_topics:
                await interaction.followup.send("これ以上の提案を生成できませんでした。")
                return

            new_embed = _build_curate_embed(new_suggestions, new_reject_count)
            new_view = CurateView(
                new_suggestions, self.user_id,
                context=ctx,
                rejected_topics=new_rejected,
                reject_count=new_reject_count,
            )
            await interaction.followup.send(embed=new_embed, view=new_view)

        except Exception as e:
            await interaction.followup.send(f"再提案の生成に失敗しました: {e}")

    async def _end_callback(self, interaction: discord.Interaction):
        """セッション終了。"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "このボタンは操作できません。", ephemeral=True
            )
            return

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"👋 探索を終了しました（{self.reject_count}回再提案）", view=self
        )


class SetupView(discord.ui.View):
    """Onboarding view for /setup: dropdown with multiple topic selection."""

    def __init__(self, suggestions: dict, user_id: int):
        super().__init__(timeout=600)
        self.suggestions = suggestions
        self.user_id = user_id
        self.topics = suggestions.get("topics", [])

        options = []
        for i, topic in enumerate(self.topics[:25]):
            options.append(discord.SelectOption(
                label=topic["title"][:100],
                description=topic.get("description", "")[:100],
                value=str(i),
            ))

        select = discord.ui.Select(
            placeholder="興味のあるトピックを選んでください...",
            min_values=1,
            max_values=min(10, len(options)),
            options=options,
        )
        select.callback = self._select_callback
        self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "このメニューは操作できません。", ephemeral=True
            )
            return

        selected_indices = [int(v) for v in interaction.data["values"]]
        selected_topics = [
            self.topics[i] for i in selected_indices if i < len(self.topics)
        ]

        await interaction.response.defer()

        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(view=self)

        names = ", ".join([t["title"] for t in selected_topics])
        await interaction.followup.send(
            f"📚 {len(selected_topics)}件のトピックを調査します: {names}\n"
            f"しばらくお待ちください..."
        )

        results = []
        for topic in selected_topics:
            result = await asyncio.to_thread(
                search_and_save_topic,
                topic["title"],
                topic.get("search_query", ""),
                topic.get("source_note", ""),
                topic.get("branch", ""),
            )
            results.append(f"- {topic['title']}: {result}")
            await asyncio.sleep(1)

        summary = "\n".join(results)
        if _log_to_daily_note:
            await asyncio.to_thread(
                _log_to_daily_note, "command", f"/setup -> {names}", summary
            )
        
async def cmd_topic(interaction: discord.Interaction, query: str):
    """特定のトピックについて調査しノートを作成する"""
    await interaction.response.defer()

    try:
        result = await asyncio.to_thread(search_and_save_topic, query)

        if _log_to_daily_note:
            await asyncio.to_thread(
                _log_to_daily_note, "command", f"/topic {query}", result
            )
        view = _create_obsidian_view(result) if _create_obsidian_view else None
        display_result = _format_discord_content(result) if _format_discord_content else result
        
        kwargs = {}
        if view:
            kwargs["view"] = view

        await interaction.followup.send(f"✅ {display_result}", **kwargs)

    except Exception as e:
        await interaction.followup.send(f"Error: {e}")
