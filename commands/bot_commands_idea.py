# -*- coding: utf-8 -*-
"""
アイデア蓄積 (/idea) + 壁打ち (/brainstorm) コマンド。

/idea: 雑なアイデアを1文でVaultに即保存する低摩擦な入力口。
/brainstorm: AIと1問1答を繰り返しながらアイデアを育て、
             「🏁 まとめる」ボタンで対話全体を1枚のメモに要約してVault保存する。

bot_commands_article.py と同じ自己完結スタイル (init_helpers 不要)。
セッション状態はスレッドID -> dict のメモリ辞書のみで、bot 再起動をまたがない
(article の _article_sessions と同じ耐久性)。
"""
import discord
import asyncio

from tools.tool_obsidian import tool_create_note
from lib.bot_errors import log_command_error, is_failure_result
from commands.bot_views import AllowedUserView
from scripts.generate_idea_dialogue import generate_next_question, summarize_idea_session

# View の有効期限。/brainstorm の質問メッセージは thread.send() の素の Message
# (bot トークンで編集できる) であり interaction token 経由ではないため、
# ClientReviewLoopView (followup.send() 由来の WebhookMessage) と違い
# interaction token の15分寿命は関係ない。スレッドの auto_archive_duration=60分
# より短く、かつ壁打ちに十分な余裕を見て30分にする。
VIEW_TIMEOUT_SECONDS = 1800

# ボタンを押し忘れて延々と続いた場合の下限。強制終了はせず一言添えるだけ。
SOFT_TURN_CAP = 12

# thread_id -> {"user_id", "seed", "turns": [{"question","answer"}],
#               "pending_question", "last_view", "busy"}
_brainstorm_sessions: dict[int, dict] = {}


async def cmd_idea(interaction: discord.Interaction, text: str):
    """雑なアイデアを1文のままVaultに保存する。"""
    await interaction.response.defer()
    try:
        # 改行を含むまま24文字切り出すとファイル名の途中に改行が入り、
        # OS側のパスエラーで「Error creating note」になる (原因が分かりにくい失敗)。
        # 先に空白類を1個ずつに畳んでから切り出す。
        filename = " ".join(text.split())[:24] or "idea"
        result = await asyncio.to_thread(tool_create_note, filename, text)
    except Exception as e:
        result = log_command_error("/idea", e)
    icon = "❌" if is_failure_result(result) else "✅"
    await interaction.followup.send(f"{icon} {result}")


class WrapUpView(AllowedUserView):
    """壁打ちセッションを要約・保存して終了させる「🏁 まとめる」ボタン。"""

    def __init__(self, thread: discord.Thread):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.thread = thread
        # on_timeout でボタンを無効化するために、この View を載せた最新メッセージを覚えておく。
        self.message: discord.Message | None = None

    async def on_timeout(self):
        """期限切れ時: 取りこぼし防止で自動的に要約・保存してからボタンを無効化する。"""
        for item in self.children:
            item.disabled = True

        # busy中 (ボタン押下や返信処理が進行中) はここで pop してしまうと、進行中の処理が
        # 参照しているセッションが辞書から消え、_send_question が次の質問を「もう存在しない
        # セッション」として送ってしまう (会話が宙に浮く)。busyなら何もせず見送る —
        # 進行中の処理が完了すれば _send_question が新しい View (新タイマー) に張り替える。
        session = _brainstorm_sessions.get(self.thread.id)
        if session is not None and session.get("busy"):
            return
        _brainstorm_sessions.pop(self.thread.id, None)
        try:
            if session:
                result = await _summarize_and_save(session)
                await self.thread.send(f"⌛ 壁打ちの有効期限が切れたため自動的にまとめました。\n{result}")
            if self.message is not None:
                await self.message.edit(view=self)
        except discord.HTTPException as e:
            log_command_error("WrapUpView.on_timeout", e, "❌ View 期限切れ処理失敗")

    @discord.ui.button(label="🏁 まとめる", style=discord.ButtonStyle.success)
    async def wrap_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = _brainstorm_sessions.get(self.thread.id)
        if session is None:
            await interaction.response.send_message("⚠️ このセッションは既に終了しています。", ephemeral=True)
            return
        if interaction.user.id != session["user_id"]:
            await interaction.response.send_message("⚠️ このセッションの開始者のみ操作できます。", ephemeral=True)
            return
        if session.get("busy"):
            await interaction.response.send_message("⏳ 処理中です。少々お待ちください。", ephemeral=True)
            return

        # busy フラグは defer() より前に立てる。defer() は await を挟むため、後に立てると
        # 連打 (2回目のクリックが1回目の defer 中に割り込む) で busy チェックをすり抜け、
        # summarize_idea_session/tool_create_note が二重に走ってしまう (grill 指摘)。
        # defer() 自体も try の中に入れる — 外に置くと defer() が例外を投げた場合に
        # busy が永久 True のまま残り、セッションが二度と操作できなくなる。
        session["busy"] = True
        try:
            await interaction.response.defer()
            result = await _summarize_and_save(session)
        finally:
            session["busy"] = False

        if is_failure_result(result):
            await interaction.followup.send(f"❌ {result}\nもう一度ボタンを押すか、対話を続けてください。")
            return

        _brainstorm_sessions.pop(self.thread.id, None)
        button.disabled = True
        if self.message is not None:
            await self.message.edit(view=self)
        self.stop()
        await interaction.followup.send(f"✅ {result}")


async def _summarize_and_save(session: dict) -> str:
    """対話を要約し tool_create_note で保存する。tool 層の規約に合わせ Error 文字列を返す。"""
    idea = await asyncio.to_thread(summarize_idea_session, session["seed"], session["turns"])
    if not idea or not idea.get("content"):
        return "Error: 要約の生成に失敗しました。"
    filename = (idea.get("title") or session["seed"])[:24]
    return await asyncio.to_thread(
        tool_create_note, filename, idea["content"], idea.get("summary", "")
    )


async def cmd_brainstorm(interaction: discord.Interaction, text: str):
    """AIと1問1答を繰り返す壁打ちセッションを開始する。"""
    await interaction.response.defer(ephemeral=True)

    try:
        thread = await interaction.channel.create_thread(
            name=f"💡壁打ち: {text[:20]}",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60,
        )
    except (discord.HTTPException, AttributeError) as e:
        # AttributeError: DM/スレッド内など create_thread を持たないチャンネルから呼ばれた場合
        await interaction.followup.send(
            log_command_error("/brainstorm (create_thread)", e, "❌ スレッドを作成できませんでした"),
            ephemeral=True,
        )
        return

    await interaction.followup.send(f"スレッドを作成しました: {thread.mention}", ephemeral=True)
    await thread.send(f"## 💡 壁打ちセッションを開始します\n**お題**: {text}\n\n最初の質問を考え中...")

    question = await asyncio.to_thread(generate_next_question, text, [])
    if not question:
        await thread.send("❌ 質問の生成に失敗しました。もう一度 `/brainstorm` を実行してください。")
        return

    _brainstorm_sessions[thread.id] = {
        "user_id": interaction.user.id,
        "seed": text,
        "turns": [],
        "pending_question": question,
        "busy": False,
    }
    await _send_question(thread, question)


async def _send_question(thread: discord.Thread, question: str):
    session = _brainstorm_sessions.get(thread.id)
    body = f"**{question}**"
    if session and len(session["turns"]) >= SOFT_TURN_CAP:
        body += "\n\n(そろそろ🏁まとめてボタンでどうぞ)"

    if session is not None:
        # 直前の質問のボタンは無効化し、最新メッセージだけが操作可能な状態にする。
        # stop() を呼ばないと古い View のタイマーが生き続け、対話が30分を超えたときに
        # 古い View の on_timeout が今も進行中のセッションを勝手に要約・終了させてしまう。
        prev_view = session.get("last_view")
        if prev_view is not None:
            prev_view.stop()
            if prev_view.message is not None:
                try:
                    await prev_view.message.edit(view=None)
                except discord.HTTPException:
                    pass

    view = WrapUpView(thread)
    message = await thread.send(body, view=view)
    view.message = message

    if session is not None:
        session["last_view"] = view


async def handle_brainstorm_reply(message: discord.Message):
    """壁打ちスレッド内でのユーザー返信を処理し、次の質問を生成する。"""
    if message.author.bot:
        return

    session = _brainstorm_sessions.get(message.channel.id)
    if not session:
        return
    if message.author.id != session["user_id"]:
        return
    if session.get("busy"):
        return

    session["busy"] = True
    try:
        session["turns"].append({"question": session["pending_question"], "answer": message.content})
        question = await asyncio.to_thread(generate_next_question, session["seed"], session["turns"])
        if not question:
            await message.channel.send("❌ 次の質問の生成に失敗しました。🏁まとめてボタンで終了できます。")
            return
        session["pending_question"] = question
        await _send_question(message.channel, question)
    finally:
        session["busy"] = False
