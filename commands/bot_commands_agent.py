# -*- coding: utf-8 -*-
import discord
from discord import app_commands
import asyncio
import io
import re
import time

# ツールをインポート
from engine_plan_execute import (
    run_plan_and_execute,
    extract_patterns,
    generate_performance_dashboard,
)

from lib.bot_errors import log_command_error, is_failure_result, FAILURE_PREFIXES

# bot_discord から注入されるヘルパー
_log_to_daily_note = None
_send_long_message = None
_send_with_fallback = None

def init_helpers(log_func, send_long_func, send_fallback_func):
    global _log_to_daily_note, _send_long_message, _send_with_fallback
    _log_to_daily_note = log_func
    _send_long_message = send_long_func
    _send_with_fallback = send_fallback_func

# ---------------------------------------------------------
# 短期セッション記憶 (Phase ③)
# チャンネル毎に直前タスク1件を保持し、次回 /agent の計画プロンプトに注入する。
# in-memory のみ (Bot再起動でリセットされるのは仕様)。
# 読み書きともイベントループ側で行うため lock 不要。
# ---------------------------------------------------------
_SESSION_TTL_SECONDS = 30 * 60
_GOAL_SNIPPET_CHARS = 200
_RESULT_SNIPPET_CHARS = 1000
_MAX_SESSION_FILES = 5
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")

_sessions = {}  # channel_id -> {"goal": str, "summary": str, "files": list, "ts": float}

# run_plan_and_execute が返す失敗文字列の接頭辞。
# 汎用分 ("Error:" / "Error (") は lib.bot_errors に集約し、ここには
# Plan-Execute エンジン固有の文言だけを足す (判定が二重管理になると必ず割れる)。
# この判定は「セッションに保存しない」と「見出しを ❌ にする」の両方で使う。
# 判定そのものは必ず is_failure_result() を通すこと: 汎用側は接頭辞 tuple では
# 表せない揺れ ("[ERROR] ..." / "[xtrend error] ..." / "Error creating note: ...")
# を正規表現で見ており、ここで startswith を書くとその分だけ取りこぼす。
_FAILURE_PREFIXES = FAILURE_PREFIXES + (
    "計画生成に失敗しました",
    "計画を生成できませんでした",
    "タスク失敗:",
)


def _is_failure(result: str) -> bool:
    """エンジン固有の文言を足した失敗判定。この2用途の唯一の窓口。"""
    return is_failure_result(result, _FAILURE_PREFIXES)


def mission_header(result: str) -> str:
    """Plan-Execute の結果見出し。失敗を "✅ Mission Complete" で出さないための単一箇所。

    /agent (このモジュール) と /chat agent (bot_discord.py) は同じエンジンの
    戻り値を表示するため、両方からここを呼ぶ (public にしているのはそのため)。
    """
    if _is_failure(result):
        return "❌ **Mission Failed**"
    return "✅ **Mission Complete**"


def _get_session_context(channel_id) -> str:
    """TTL内のセッションがあれば文脈テキストを返す。期限切れは削除して空文字。"""
    entry = _sessions.get(channel_id)
    if not entry:
        return ""
    if time.time() - entry["ts"] > _SESSION_TTL_SECONDS:
        _sessions.pop(channel_id, None)
        return ""
    files = "、".join(f"[[{f}]]" for f in entry["files"]) if entry["files"] else "なし"
    return (
        f"- 直前の要求: {entry['goal']}\n"
        f"- 直前の結果(先頭): {entry['summary']}\n"
        f"- 関連ファイル: {files}"
    )


def _update_session(channel_id, goal: str, result: str) -> None:
    """直前タスクの要約を保存する。[[wikilink]] は重複除去して最大5件抽出。"""
    files = []
    for name in _WIKILINK_RE.findall(result):
        if name not in files:
            files.append(name)
        if len(files) >= _MAX_SESSION_FILES:
            break
    _sessions[channel_id] = {
        "goal": goal[:_GOAL_SNIPPET_CHARS],
        "summary": result[:_RESULT_SNIPPET_CHARS],
        "files": files,
        "ts": time.time(),
    }

async def cmd_agent(interaction: discord.Interaction, goal: str):
    """AIエージェントに自律的なタスクを依頼"""
    is_deferred = False
    try:
        await interaction.response.defer()
        is_deferred = True
    except discord.NotFound:
        print("[Agent] Warning: Interaction timed out before defer. Using channel send for fallback.")
        try:
            await interaction.channel.send(
                f"{interaction.user.mention} 🤖 **エージェントを起動します...** (応答が遅延したため、通常メッセージで完了通知を行います)"
            )
        except Exception as e:
            print(f"[Agent] Failed to send startup fallback message: {e}")
    except Exception as e:
        print(f"[Agent] Unexpected error during defer: {e}")

    async def discord_pe_callback(step_data):
        if not is_deferred:
            return
        phase = step_data.get("phase", "")
        try:
            if phase == "plan" and step_data.get("status") == "planning":
                await interaction.edit_original_response(content="🧠 **計画を策定中...**")
            elif phase == "plan" and step_data.get("status") == "ready":
                plan = step_data.get("plan", [])
                lines = [f"{p['step']}. {p['task']}" for p in plan]
                plan_text = "\n".join(lines)
                await interaction.edit_original_response(content=f"📋 **実行計画:**\n{plan_text}\n\n▶️ 実行開始...")
            elif phase == "execute" and step_data.get("status") == "running":
                await interaction.edit_original_response(
                    content=f"▶️ **実行中:** Step {step_data.get('step', '?')}/{step_data.get('total', '?')} - {step_data.get('task', '')}..."
                )
            elif phase == "execute" and step_data.get("status") in ("done", "error"):
                icon = "✅" if step_data["status"] == "done" else "❌"
                await interaction.edit_original_response(
                    content=f"{icon} Step {step_data.get('step', '?')}/{step_data.get('total', '?')} - {step_data.get('task', '')}\n\n⏳ 次のステップへ..."
                )
            elif phase == "critique":
                await interaction.edit_original_response(content="🔍 **結果を評価中...**")
            elif phase == "revision":
                await interaction.edit_original_response(
                    content=f"🔄 **再計画中... (修正 {step_data.get('attempt', 1)}回目)**"
                )
        except Exception as e:
            print(f"[AgentUI] Warning: Failed to update status: {e}")

    session_context = _get_session_context(interaction.channel_id)

    try:
        loop = asyncio.get_running_loop()
        def sync_callback_wrapper(step_data):
            asyncio.run_coroutine_threadsafe(discord_pe_callback(step_data), loop)
        result = await asyncio.to_thread(
            run_plan_and_execute, goal,
            step_callback=sync_callback_wrapper,
            session_context=session_context,
        )
    except Exception as e:
        result = log_command_error(f"/agent {goal}", e)

    # 失敗結果は次回の文脈に混ぜない
    if not _is_failure(result):
        _update_session(interaction.channel_id, goal, result)

    header = mission_header(result)

    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", f"/agent {goal}", result)

    if _send_with_fallback:
        await _send_with_fallback(interaction, f"{header}\n\n{result}", title="Agent Execution", emoji="🤖")
    else:
        await interaction.followup.send(f"{header}\n\n{result[:1900]}")

async def cmd_agent_stats(interaction: discord.Interaction):
    """AIエージェントの実行統計と学習状況を表示"""
    await interaction.response.defer()
    try:
        patterns = await asyncio.to_thread(extract_patterns)
        if patterns.get("message"):
            await interaction.followup.send(f"📊 {patterns['message']}")
            return

        embed = discord.Embed(title="📊 Agent Performance Stats", color=0x3498DB)
        embed.add_field(
            name="全体統計",
            value=f"総実行: **{patterns['total']}**回\n成功率: **{patterns['success_rate']:.0%}**\n平均時間: **{patterns['avg_duration']:.1f}秒**",
            inline=False,
        )
        tool_rel = patterns.get("tool_reliability", {})
        sorted_tools = sorted(tool_rel.items(), key=lambda x: x[1], reverse=True)[:5]
        if sorted_tools:
            tool_lines = "\n".join(f"  {t}: **{r:.0%}**" for t, r in sorted_tools)
            embed.add_field(name="ツール信頼性 TOP5", value=tool_lines, inline=False)
        
        insights = patterns.get("insights", "")
        if insights:
            embed.add_field(name="AI分析", value=insights[:1000], inline=False)

        await interaction.followup.send(embed=embed)
        await asyncio.to_thread(generate_performance_dashboard)
    except Exception as e:
        await interaction.followup.send(log_command_error("/agent-stats", e))
