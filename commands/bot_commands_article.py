# -*- coding: utf-8 -*-
import discord
from discord import app_commands
import asyncio
import os
import json
from scripts.generate_experiential_article import extract_points_and_questions, generate_final_article

# ---------------------------------------------------------
# State Management
# ---------------------------------------------------------
# thread_id -> { "user_id": int, "draft": str, "qa_pairs": list, "current_index": int }
_article_sessions = {}

class ArticleDraftModal(discord.ui.Modal, title="体験談記事の作成"):
    draft_title = discord.ui.TextInput(
        label="記事のタイトル（仮）",
        placeholder="例: 未経験からAIエージェントを作った話",
        required=True
    )
    draft_content = discord.ui.TextInput(
        label="内容の下書き・メモ",
        placeholder="箇条書きでも構いません。あなたが伝えたい体験の核心を書いてください。",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # スレッドを作成してインタビューを開始
        thread = await interaction.channel.create_thread(
            name=f"📝記事作成: {self.draft_title.value[:20]}...",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60
        )
        
        await interaction.followup.send(f"スレッドを作成しました: {thread.mention}", ephemeral=True)
        
        await thread.send(f"## 📝 記事作成セッションを開始します\n**仮タイトル**: {self.draft_title.value}\n\n下書きを分析中... 少々お待ちください。")
        
        # 分析実行
        loop = asyncio.get_event_loop()
        points = await loop.run_in_executor(None, extract_points_and_questions, self.draft_content.value)
        
        if not points:
            await thread.send("❌ 下書きの分析に失敗しました。もう一度やり直すか、内容を詳しく書いてみてください。")
            return

        # セッション保存
        _article_sessions[thread.id] = {
            "user_id": interaction.user.id,
            "draft": self.draft_content.value,
            "qa_pairs": points, # ここに answer を追加していく
            "current_index": 0
        }
        
        await thread.send(f"分析が完了しました！{len(points)}つの深掘りポイントについて質問します。\nあなたの**「生の言葉・感情」**で答えてください。その文体が記事に反映されます。")
        
        # 最初の質問
        await send_next_question(thread)

async def send_next_question(thread: discord.Thread):
    session = _article_sessions.get(thread.id)
    if not session: return

    idx = session["current_index"]
    if idx < len(session["qa_pairs"]):
        p = session["qa_pairs"][idx]
        msg = f"**【質問 {idx + 1}/{len(session['qa_pairs'])}】**\n"
        msg += f"### {p['point']}\n"
        msg += f"> {p['question']}"
        await thread.send(msg)
    else:
        # すべての質問が終了
        await thread.send("✨ すべての質問に回答いただきました！まずは初稿を執筆します... (1-2分かかります)")
        
        # 記事の初稿生成
        loop = asyncio.get_event_loop()
        draft_article = await loop.run_in_executor(None, generate_final_article, session["draft"], session["qa_pairs"])
        
        if draft_article:
            await thread.send("📝 初稿の作成が完了しました。続いてプロ編集者（AI）の視点による品質チェックと推敲（リフレクション）を実行します... 🔍")
            
            # リフレクションループの実行
            from scripts.generate_experiential_article import run_article_reflection_loop
            final_article = await loop.run_in_executor(None, run_article_reflection_loop, draft_article, session["draft"], session["qa_pairs"])
            
            # 長い場合は分割
            if len(final_article) > 1900:
                parts = [final_article[i:i+1900] for i in range(0, len(final_article), 1900)]
                for p in parts:
                    await thread.send(p)
            else:
                await thread.send(final_article)
            
            await thread.send("🎉 記事が完成しました！このスレッドはこれにて終了です。")
        else:
            await thread.send("❌ 記事の生成に失敗しました。")
        
        # セッション削除
        del _article_sessions[thread.id]

async def handle_article_reply(message: discord.Message):
    """
    メッセージが記事作成スレッド内での回答かどうかを判定し、処理する。
    bot_discord.py の on_message から呼ばれることを想定。
    """
    if message.author.bot: return
    
    session = _article_sessions.get(message.channel.id)
    if not session: return
    
    # 本人確認
    if message.author.id != session["user_id"]: return

    # 回答を記録
    idx = session["current_index"]
    session["qa_pairs"][idx]["answer"] = message.content
    session["current_index"] += 1

    # 次の質問へ
    await send_next_question(message.channel)

async def cmd_article(interaction: discord.Interaction):
    """
    /article コマンドのエントリポイント
    """
    await interaction.response.send_modal(ArticleDraftModal())

