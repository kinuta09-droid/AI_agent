# -*- coding: utf-8 -*-
"""
Discord スラッシュコマンドの認可制御 (ALLOWED_USER_IDS ベースのアクセス制御)。

bot_discord.py から切り出し。GatedCommandTree / is_allowed_user は
discord.Client (bot インスタンス) を必要としない独立した責務のため、
chat サブシステム等への依存なくここに置ける。
"""
import os

import discord
from discord import app_commands
from dotenv import load_dotenv

from lib.paths import ENV_PATH

# 単独 import されても .env を読めるようにする (lib_shared.py と同じ作法)。
load_dotenv(ENV_PATH)


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


def is_allowed_user(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


class GatedCommandTree(app_commands.CommandTree):
    """ALLOWED_USER_IDS に含まれないユーザーの全スラッシュコマンドを一括拒否する。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_allowed_user(interaction.user.id):
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
