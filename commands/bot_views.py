# -*- coding: utf-8 -*-
"""
永続 Discord View (timeout=None + 固定 custom_id + on_ready で bot.add_view 登録)。

スケジューラプロセスは discord.py の gateway 接続を持たず、コンポーネント
(ボタン) の click を受信できない。そのためスケジューラは REST 経由で
custom_id 付きボタンを「表示」するだけにし、実際のコールバックはここに
登録された永続 View (bot プロセス側) が処理する。

コールバックはインスタンス状態を持たず、都度ファイルを読み直す
(ステートレス設計) ため、bot 再起動をまたいでも動作する。
"""
import os
import io
import datetime
import asyncio

import discord

from lib.lib_shared import WORK_DIR

# ---------------------------------------------------------
# 認可付き基底 View
# ---------------------------------------------------------

# init_views() で注入される許可判定関数。未注入時はフェイルオープン。
_is_allowed_user = None


def init_views(is_allowed_fn):
    """bot_discord.py の on_ready から呼び出し、許可判定関数を注入する。"""
    global _is_allowed_user
    _is_allowed_user = is_allowed_fn


class AllowedUserView(discord.ui.View):
    """ALLOWED_USER_IDS に含まれないユーザーの操作を一括拒否する基底 View。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_allowed_user is None or _is_allowed_user(interaction.user.id):
            return True
        await interaction.response.send_message(
            "⛔ このボタンを操作する権限がありません。", ephemeral=True
        )
        return False


# ---------------------------------------------------------
# 深掘りリサーチボタン (朝ブリーフィング / /briefing 共通)
# ---------------------------------------------------------

_XTREND_JSON = os.path.join(WORK_DIR, "40_Report", "XTrend", "latest_features.json")
DIVE_CUSTOM_IDS = ["brief_dive:1", "brief_dive:2", "brief_dive:3"]

# 多重起動防止 (feature_name をキーにした実行中セット)
_dive_running: set[str] = set()


def _load_top_features(top_n: int = 3, max_age_days: int = 2) -> list[dict]:
    """latest_features.json から鮮度チェック済みの上位 N 件を返す（stale/不在時は空リスト）。"""
    if not os.path.exists(_XTREND_JSON):
        return []
    try:
        import json
        with open(_XTREND_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []

    date_str = data.get("date", "")
    try:
        data_date = datetime.date.fromisoformat(date_str)
        if (datetime.date.today() - data_date).days > max_age_days:
            return []
    except ValueError:
        return []

    return data.get("features", [])[:top_n]


class BriefingDeepDiveView(AllowedUserView):
    """朝ブリーフィング TOP3 の深掘りリサーチを起動する永続ボタン群。

    Args:
        feature_names: 表示する feature 名のリスト。省略時は3個の汎用ラベルで
            全 custom_id を登録する (on_ready でのグローバル永続登録専用 —
            bot 起動時点のデータ鮮度に関わらず全 custom_id を確実に紐付けるため、
            latest_features.json は読まない)。実際にユーザーへ送信する View
            (/briefing・朝ブリーフィング) は実際の feature 件数に合わせて
            明示的に feature_names を渡すこと。
    """

    def __init__(self, feature_names: list[str] | None = None):
        super().__init__(timeout=None)
        if feature_names is None:
            feature_names = ["深掘り"] * len(DIVE_CUSTOM_IDS)
        for i, custom_id in enumerate(DIVE_CUSTOM_IDS[:len(feature_names)]):
            label = f"🔍 {feature_names[i][:70]}"
            btn = discord.ui.Button(
                label=label, style=discord.ButtonStyle.primary, custom_id=custom_id
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            features = _load_top_features(top_n=3)
            if index >= len(features):
                await interaction.response.send_message(
                    "⚠️ このデータは古くなっているため、深掘りできません。"
                    "最新の /briefing または /xtrend を実行してください。",
                    ephemeral=True,
                )
                return

            topic = features[index].get("feature_name", "").strip()
            if not topic:
                await interaction.response.send_message("❌ トピック名が空です。", ephemeral=True)
                return

            if topic in _dive_running:
                await interaction.response.send_message(
                    f"⏳ 「{topic}」は既に深掘り実行中です。", ephemeral=True
                )
                return

            await interaction.response.send_message(
                f"🔍 深掘り開始: **{topic}** (数分かかります。完了したらこのチャンネルに投稿します)",
                ephemeral=True,
            )

            _dive_running.add(topic)
            try:
                from tools.tool_future_search import run_future_search
                result = await asyncio.to_thread(
                    run_future_search, topic, "standard", False
                )
            except Exception as e:
                result = f"❌ 深掘りリサーチでエラーが発生しました: {e}"
            finally:
                _dive_running.discard(topic)

            channel = interaction.channel
            if channel is None:
                return
            if len(result) > 1900:
                file_obj = discord.File(
                    io.BytesIO(result.encode("utf-8")), filename=f"deepdive_{topic[:30]}.md"
                )
                await channel.send(content=f"✅ 深掘り完了: **{topic}**", file=file_obj)
            else:
                await channel.send(content=f"✅ 深掘り完了: **{topic}**\n\n{result}")

        return callback


# ---------------------------------------------------------
# タスク失敗時のログ表示ボタン (スケジューラ通知用)
# ---------------------------------------------------------

SCHEDULER_LOG_CUSTOM_ID = "sched_show_log"
_SCHEDULER_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler.log")


class TaskFailureView(AllowedUserView):
    """スケジューラのタスク失敗通知に添付する「ログ表示」永続ボタン。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📋 ログ表示", style=discord.ButtonStyle.secondary, custom_id=SCHEDULER_LOG_CUSTOM_ID)
    async def show_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not os.path.exists(_SCHEDULER_LOG_PATH):
            await interaction.response.send_message("❌ scheduler.log が見つかりません。", ephemeral=True)
            return
        try:
            # テキストモードの seek は tell() が返した値以外は未定義動作
            # (マルチバイト文字境界で壊れる) なのでバイナリで読んでからデコードする
            with open(_SCHEDULER_LOG_PATH, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8000))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            await interaction.response.send_message(f"❌ ログ読み込みエラー: {e}", ephemeral=True)
            return

        file_obj = discord.File(io.BytesIO(tail.encode("utf-8")), filename="scheduler_log_tail.txt")
        await interaction.response.send_message(
            "📋 scheduler.log の末尾 (最大8KB):", file=file_obj, ephemeral=True
        )
