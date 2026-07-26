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
import os
import time

import discord

from lib import service_control

# このプロセスが起動したおおよその時刻 (Bot 起動時に import されるため)。
_IMPORTED_AT = time.time()

# bot_discord から注入される discord.Client (自分自身を閉じるために必要)
_bot = None

# 認可リスト (ALLOWED_USER_IDS) が設定済みかを返す述語。
_allowlist_configured = None


def init_helpers(bot_client, allowlist_configured=None):
    global _bot, _allowlist_configured
    _bot = bot_client
    _allowlist_configured = allowlist_configured


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


async def announce_restart_complete(bot_client):
    """再起動の書き置きがあれば「完了しました」を報告する (on_ready から呼ぶ)。

    Bot は自分を閉じて別プロセスとして上がり直すので、再起動を要求した
    interaction はプロセスごと消えている。完了を伝えられるのは
    「次に起動した自分」だけなので、報告先をファイルに書き置いて引き継ぐ。

    通知の失敗で on_ready を壊さない (Bot 本体の起動が最優先)。
    """
    try:
        notice = await asyncio.to_thread(service_control.pop_restart_notice)
        if not notice:
            return
        channel = bot_client.get_channel(notice["channel_id"])
        if channel is None:
            # 起動直後でキャッシュが埋まっていないことがあるので API で取り直す
            channel = await bot_client.fetch_channel(notice["channel_id"])
        await channel.send(
            f"✅ **問題なく再起動しました** "
            f"(停止 {notice['elapsed']:.1f} 秒 / PID {os.getpid()})"
        )
    except Exception as e:
        print(f"[Admin] 再起動完了の通知に失敗: {e}")


async def _watch_service_recovery(interaction: discord.Interaction, name: str,
                                  label: str, timeout: float = 90.0):
    """kill した子が復帰したかを見届けて報告する。

    request_restart が返すのは「停止させた」ところまで。実際に上がってきたかは
    スーパーバイザ任せなので、heartbeat が動き出すまで見てから完了を伝える。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        st = await asyncio.to_thread(service_control.status, name)
        if st["state"] == "running":
            try:
                await interaction.followup.send(
                    f"✅ **{label} は問題なく再起動しました** (PID {st['pid']})")
            except discord.HTTPException:
                pass
            return
    try:
        await interaction.followup.send(
            f"⚠️ {label} の復帰を {int(timeout)} 秒待ちましたが確認できませんでした。"
            f"`/status` と `logs\\launcher.log` を確認してください。")
    except discord.HTTPException:
        pass


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

    # 管理系だけは fail-closed にする。bot_discord は ALLOWED_USER_IDS 未設定時に
    # 「全ユーザー許可」で起動を続ける (env 設定ミスで Bot が完全停止するのを
    # 避けるための意図的な fail-open)。読み取り系ならそれで許容できるが、
    # 「誰でもサービスを落とせる」は許容できないのでここで止める。
    if _allowlist_configured is not None and not _allowlist_configured():
        await interaction.followup.send(
            "⛔ `ALLOWED_USER_IDS` が未設定のため `/restart` は実行できません。\n"
            "未設定だと全ユーザーがサービスを停止できてしまいます。"
            "`.env` に自分の Discord ユーザー ID を設定してください。")
        return

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
    watch_scheduler = False
    if target in ("scheduler", "both"):
        ok, msg = await asyncio.to_thread(service_control.request_restart, "scheduler")
        messages.append(msg)
        watch_scheduler = ok

    restart_self = target in ("bot", "both")
    if restart_self:
        messages.append("♻️ Discord Bot: 応答後に自分を閉じます。約5秒後に新しいコードで復帰します。")

    if not messages:
        await interaction.followup.send(
            f"❓ 不明な対象です: `{target}` (bot / scheduler / both)")
        return

    await interaction.followup.send("\n".join(messages))

    if restart_self:
        # 印を残すのは応答の送信に成功してから。先に立てると、送信が失敗して
        # 停止に至らなかったときフラグだけが残り、後日の**本物のクラッシュ**を
        # スーパーバイザが「意図的な再起動」と誤読する (鮮度の足切りもあるが、
        # そもそも残さないのが正しい)。
        # 自分は kill せず、フラグを立てて自主的に閉じる。
        await asyncio.to_thread(service_control.set_restart_flag, "discord_bot")
        # 完了報告の宛先を書き置く。この直後にプロセスが消えるので、
        # 「問題なく再起動しました」を投稿できるのは次に起動した自分だけ。
        await asyncio.to_thread(
            service_control.write_restart_notice, interaction.channel_id, target)
        # followup の送信完了を待ってから閉じる (途中で切ると応答が消える)
        asyncio.create_task(_close_after(1.5))
        return   # 自分が落ちるので、これ以降の見届けタスクは走れない

    if watch_scheduler:
        asyncio.create_task(
            _watch_service_recovery(interaction, "scheduler", "Scheduler"))
