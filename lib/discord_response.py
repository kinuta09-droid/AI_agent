# -*- coding: utf-8 -*-
"""
Discord 応答共通ヘルパー (bot_discord.py から切り出し)。

commands/bot_commands_{curator,tools,agent,chat}.py がそれぞれ独自の
init_helpers() 経由で受け取っていた5関数をここに集約する。commands/*.py は
bot_discord.py を介さずここを直接 import できるため、新規コマンドファイルが
増えるたびに同じ注入配線を書く必要がなくなる (実装漏れ・ローカル重複定義の再発防止)。

注意: OpenInObsidianView は commands.bot_views.AllowedUserView を継承しており、
lib -> commands の依存が1本生じる。bot_views 側は lib.lib_shared / lib.bot_errors
しか import しないため循環にはならないが、通常の層構造 (lib は commands に依存しない)
には反する。AllowedUserView 自体の移設は bot_views.py 内の他3クラス
(BriefingDeepDiveView 等) を巻き込む別範囲の変更のため、今回は許容する。
"""
import os
import re
import io

import discord

from lib.lib_obsidian_utils import get_obsidian_uri, get_obsidian_uri_from_link
from lib.bot_errors import log_command_error
from commands.bot_views import AllowedUserView

# Obsidian を開くボタンの有効期限 (最後の操作からの idle 秒数)。
# この View は「そのメッセージ専用のリンク集」をコールバックのクロージャに閉じ込めて
# おり、ボタンに固定 custom_id も無い (自動採番) ため、bot.add_view() で永続化できない。
# かつて timeout=None を指定していたが、それは
#   1. Bot 再起動後はどのみち押しても無反応 (所見3と同じ無言の死)
#   2. discord.py の View ストアから永久に消えず、送信するたびにメモリを食う
# という二重の嘘だった。有限期限にして「いつか失効する UI」であることを明示する
# (恒久対応は custom_id に vault 相対パスを埋めた永続 View 化 — 別課題) (2026-07-26)。
_OBSIDIAN_VIEW_TIMEOUT = 3600  # 1時間


class OpenInObsidianView(AllowedUserView):
    def __init__(self, links: dict[str, str]):
        """
        links: {filename: uri} の辞書
        """
        super().__init__(timeout=_OBSIDIAN_VIEW_TIMEOUT)

        # リンク数に応じてボタンを追加 (最大5個まで)
        # 5個を超える場合はドロップダウン (TODO: 実装簡略化のため一旦ボタンのみ、溢れたら無視)
        count = 0
        for name, uri in links.items():
            if count >= 5:
                break
            # ラベルが長すぎる場合は切り詰め
            label = f"📂 {name}"[:80]

            # URLではなく、コールバックでローカルコマンド(os.startfile)を実行する
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)

            # 変数をキャプチャするためにデフォルト引数を使用
            async def callback(interaction: discord.Interaction, uri=uri, name=name):
                try:
                    if uri.startswith("obsidian://"):
                        # Windows only: ローカルでURIを開く
                        os.startfile(uri)
                        await interaction.response.send_message(f"🚀 Opening **{name}** in Obsidian...", ephemeral=True, delete_after=3)
                    else:
                        await interaction.response.send_message("❌ Invalid URI scheme.", ephemeral=True)
                except Exception as e:
                    await interaction.response.send_message(
                        log_command_error(f"Obsidian で開く ({name})", e, "❌ Failed to open"),
                        ephemeral=True,
                    )

            btn.callback = callback
            self.add_item(btn)
            count += 1


def create_obsidian_view(content: str) -> discord.ui.View | None:
    """コンテンツ内のパス([[...]]含む)を検出し、Obsidianを開くボタンViewを作成する"""
    unique_links = {}

    # 1. (Path: ...) の検出（絶対パス）
    for match in re.finditer(r"\(Path:\s*([A-Z]:\\[^)]+)\)", content):
        path = match.group(1).strip()
        uri = get_obsidian_uri(path)
        if uri:
            name = os.path.basename(path)
            unique_links[name] = uri

    # 2. [[...]] の検出（相対パス/ファイル名）
    for match in re.finditer(r"\[\[(.*?)\]\]", content):
        link_text = match.group(1).strip()
        # パイプ | で表示名が指定されている場合がある [[file|label]] -> fileを採用
        if "|" in link_text:
            link_text = link_text.split("|")[0]

        uri = get_obsidian_uri_from_link(link_text)
        if uri:
            # 拡張子がない場合は補完して表示名にする
            name = link_text if "." in link_text else f"{link_text}.md"
            unique_links[name] = uri

    # 3. LoRA出力パスの検出
    match = re.search(r"出力:\s*([A-Z]:\\[^\n]+)", content)
    if match:
        path = match.group(1).strip()
        uri = get_obsidian_uri(path)
        if uri:
             name = "LoRA Dataset"
             unique_links[name] = uri

    # 4. Gmail URL の検出 → 外部リンクボタン
    gmail_links = {}
    for match in re.finditer(r"\(Gmail(\d*):\s*(https://[^)]+)\)", content):
        num = match.group(1) or "1"
        url = match.group(2).strip()
        gmail_links[f"Gmail {num}"] = url

    if unique_links or gmail_links:
        view = OpenInObsidianView(unique_links)
        for label, url in gmail_links.items():
            if len(view.children) < 5:
                view.add_item(discord.ui.Button(
                    label=label, url=url, style=discord.ButtonStyle.link
                ))
        return view
    return None


def format_discord_content(content: str) -> str:
    """Discord表示用にメッセージを整形する（[[...]]削除など）"""
    # Obsidianリンク [[...]] を太字 **...** に置換して見やすくする
    display_content = re.sub(r"\[\[(.*?)\]\]", r"**\1**", content)
    # (Path: ...) も表示上は邪魔なので削除する
    display_content = re.sub(r"\(Path:.*?\)", "", display_content)
    display_content = re.sub(r"\(Gmail\d*:.*?\)", "", display_content).strip()
    return display_content


def make_discord_file(content: str, filename: str) -> discord.File:
    """テキストコンテンツから discord.File を生成する。
    BytesIO(UTF-8) を使うことで StringIO による TypeError を防ぐ。
    """
    return discord.File(io.BytesIO(content.encode('utf-8')), filename=filename)


async def send_long_message(interaction: discord.Interaction, content: str,
                             title: str = "Result", color: int = 0x00ff00):
    """2000文字を超える場合は Embed + ファイル添付で送信する。Obsidianボタンも付与。"""

    display_content = format_discord_content(content)
    view = create_obsidian_view(content)

    kwargs = {}
    if view:
        kwargs["view"] = view

    if len(display_content) <= 2000:
        await interaction.followup.send(display_content, **kwargs)
    else:
        # サマリー（最初の1500文字）を Embed で、全文を .md ファイルで添付
        summary = display_content[:1500] + "\n\n*... (full report attached)*"
        embed = discord.Embed(title=title, description=summary, color=color)
        file = make_discord_file(content, f"{title}.md")
        await interaction.followup.send(embed=embed, file=file, **kwargs)


async def send_with_fallback(interaction: discord.Interaction, result: str,
                              title: str = "Result", emoji: str = "📋",
                              embed: discord.Embed = None, file: discord.File = None):
    """interaction.followup + NotFound 時のチャンネル直接送信フォールバック。"""
    try:
        if embed or file:
            view = create_obsidian_view(result)
            send_kwargs = {}
            if embed is not None:
                send_kwargs["embed"] = embed
            if file is not None:
                send_kwargs["file"] = file
            if view:
                send_kwargs["view"] = view
            await interaction.followup.send(**send_kwargs)
        else:
            await send_long_message(interaction, result, title=title)
    except discord.NotFound:
        try:
            mention = f"{interaction.user.mention} {emoji} **{title}** (Interaction Timed Out)"
            if len(result) > 1900:
                fb_file = make_discord_file(result, f"{title.replace(' ', '_')}.md")
                await interaction.channel.send(content=mention, file=fb_file)
            else:
                await interaction.channel.send(f"{mention}\n\n{result}")
        except Exception as e:
            print(f"[Discord] ❌ Failed to send fallback message: {e}")
    except Exception as e:
        print(f"[Discord] ❌ Failed to send result: {e}")
