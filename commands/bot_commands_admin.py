# -*- coding: utf-8 -*-
"""
commands/bot_commands_admin.py — 常駐スタックの状態確認と再起動 (/status, /restart)

なぜ Discord から再起動できる必要があるのか:
  main.py のスーパーバイザは子プロセスの死亡を拾って **新しい python プロセスとして
  再生成する** ため、コード変更の反映に必要なのは「子を終わらせること」だけ。
  これまでその手段がターミナルしか無かったので、変更のたびにターミナルへ戻っていた。

やらないこと:
  スーパーバイザ (main.py) 自身の再起動。殺すと誰も再生成せず
  (タスクスケジューラは onlogon で次のログオンまで走らない)、外から起動する術が
  Discord には無くなる。main.py を書き換えたときだけ scripts/arms.bat を使う。

認可:
  ここでは何もしない。bot_discord.GatedCommandTree.interaction_check が
  ALLOWED_USER_IDS で全スラッシュコマンドを一括ガードしており、自動的に乗る。
"""
import asyncio
import time

import discord

from lib import service_control

# このプロセスが起動したおおよその時刻 (Bot 起動時に import されるため)。
_IMPORTED_AT = time.time()

# bot_discord から注入される discord.Client (自分自身を閉じるために必要)
_bot = None


def init_helpers(bot_client):
    global _bot
    _bot = bot_client


def _format_uptime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}時間{m}分"
    return f"{m}分{s}秒" if m else f"{s}秒"


async def cmd_status(interaction: discord.Interaction):
    """常駐スタックの稼働状況を表示する。"""
    await interaction.response.defer()
    lines = await asyncio.to_thread(service_control.format_status_lines)
    body = "\n".join(lines)
    body += f"\n\n_この Bot プロセスの稼働時間: {_format_uptime(time.time() - _IMPORTED_AT)}_"
    embed = discord.Embed(
        title="🛰️ ARMS スタック状況",
        description=body,
        color=0x2ecc71 if service_control.launcher_alive() else 0xe74c3c,
    )
    embed.set_footer(text="再起動: /restart target:bot | scheduler | both")
    await interaction.followup.send(embed=embed)


async def _close_after(delay: float):
    """応答が Discord に届く猶予を置いてから自分を閉じる。

    bot.close() で bot.run() が戻り、bot_discord.run_bot の finally が PID ロックを
    解放する = 正規の終了経路を通る (os._exit だとロック解放を飛ばしてしまう)。
    プロセスが終われば main.py のスーパーバイザが約5秒後に再生成する。
    """
    await asyncio.sleep(delay)
    if _bot is not None:
        await _bot.close()


async def cmd_restart(interaction: discord.Interaction, target: str = "bot"):
    """Bot / Scheduler を再起動する (再生成はスーパーバイザに任せる)。"""
    await interaction.response.defer()
    target = (target or "bot").lower()

    # スーパーバイザが居ない状態で落とすと、そのまま沈黙して復帰できない。
    # request_restart 側でも弾いているが、自分自身を閉じる経路はそこを通らない
    # ので、ここで先に確認する。
    if not await asyncio.to_thread(service_control.launcher_alive):
        await interaction.followup.send(
            "❌ スーパーバイザ (main.py) が動いていないため中止しました。\n"
            "落としても誰も再起動しません。PC 側で `scripts\\arms.bat start` "
            "(またはデスクトップの ARMS ショートカット) から起動してください。"
        )
        return

    messages = []
    if target in ("scheduler", "both"):
        _ok, msg = await asyncio.to_thread(service_control.request_restart, "scheduler")
        messages.append(msg)

    restart_self = target in ("bot", "both")
    if restart_self:
        # 自分は kill せず、フラグだけ立てて自主的に閉じる。スーパーバイザが
        # 「意図的な再起動」と読んで失敗カウントを増やさない。
        await asyncio.to_thread(service_control.set_restart_flag, "discord_bot")
        messages.append("♻️ Discord Bot: 応答後に自分を閉じます。約5秒後に新しいコードで復帰します。")

    if not messages:
        await interaction.followup.send(
            f"❓ 不明な対象です: `{target}` (bot / scheduler / both)")
        return

    await interaction.followup.send("\n".join(messages))

    if restart_self:
        # followup の送信完了を待ってから閉じる (途中で切ると応答が消える)
        asyncio.create_task(_close_after(1.5))
