# -*- coding: utf-8 -*-
"""
アイデア蓄積 (/idea) + 壁打ち (/brainstorm) コマンド。

/idea: 雑なアイデアを1文でVaultに即保存する低摩擦な入力口。
/brainstorm: AIと1問1答を繰り返しながらアイデアを育て、
             「🏁 まとめる」ボタンで対話全体を1枚のメモに要約してVault保存する。

セッション状態はスレッドID -> dict のメモリ辞書のみで、bot 再起動をまたがない
(article の _article_sessions と同じ耐久性)。

/idea・/brainstorm はユーザーの自発的な入力(顕示選好が最も強いシグナル)だが、
これまで Chatlog に記録されず User_Memory.md の夜間蒸留から完全に見えなかった
(2026-08-26 調査で判明)。init_helpers 経由で _log_to_daily_note を注入し、
他のコマンドモジュール(bot_commands_tools.py 等)と同じ形で記録する。
"""
import discord
from discord import app_commands
import asyncio
import os
import re

from tools.tool_obsidian import (
    tool_create_note, tool_save_idea, tool_list_open_ideas, tool_delete_idea, tool_read_note,
    tool_confirm_related_notes, tool_list_permanent_ideas,
)
from tools.tool_vault_hybrid import tool_find_related_notes
from lib.bot_errors import log_command_error, is_failure_result
from lib.lib_obsidian_utils import extract_note_query, extract_excerpt_citation
from commands.bot_views import AllowedUserView
from scripts.generate_idea_dialogue import generate_next_question, summarize_idea_session

# tool_create_note の成功文字列 "Success: Created [[filename]] with template." からfilenameを拾う。
_CREATED_FILENAME_RE = re.compile(r'\[\[(.+?)\]\]')

# /brainstorm の text 引数がオートコンプリート候補から選ばれたことを示す印。
# 自由入力にはまず出てこない接頭辞なので、通常の新規アイデア開始と衝突しない。
IDEA_REF_PREFIX = "idea:"

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

# bot_discord から注入されるヘルパー (他のコマンドモジュールと同じ init_helpers パターン)
_log_to_daily_note = None


def init_helpers(log_func):
    global _log_to_daily_note
    _log_to_daily_note = log_func


async def cmd_idea(interaction: discord.Interaction, text: str):
    """雑なアイデアを1文のまま00_inboxにFleeting Noteとして即保存する。"""
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(tool_save_idea, text)
    except Exception as e:
        result = log_command_error("/idea", e)
    icon = "❌" if is_failure_result(result) else "✅"
    await interaction.followup.send(f"{icon} {result}")
    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "command", f"/idea {text}", result)


async def idea_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """/brainstorm の text 引数向け。00_inboxのstatus:openなアイデアを候補に出す。"""
    try:
        ideas = await asyncio.to_thread(tool_list_open_ideas)
    except Exception:
        return []
    current_lower = current.lower()
    return [
        app_commands.Choice(name=f"💡 {idea['title']}"[:100], value=f"{IDEA_REF_PREFIX}{idea['title']}")
        for idea in ideas
        if current_lower in idea["title"].lower()
    ][:25]


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
                result = await _summarize_and_save(session, self.thread)
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
            result = await _summarize_and_save(session, self.thread)
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


class RelatedNotesSelect(discord.ui.Select):
    """関連ノート候補のチェック式選択（min_values=0で「全部外して確定」もスキップとして許容）。
    既定で全候補を選択済みにする — 大半は使う想定なので、関係の薄い候補だけ外す方が手間が少ない。
    """

    def __init__(self, candidate_filenames: list[str]):
        options = [
            discord.SelectOption(
                label=os.path.splitext(f)[0][:100], value=f, default=True,
            )
            for f in candidate_filenames
        ]
        super().__init__(
            placeholder="紐づけるノートを選択（関係が薄いものは外してください）",
            min_values=0, max_values=len(options), options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.selected = self.values


class RelatedNotesView(AllowedUserView):
    """関連ノート通知のチェック式選択+確定ボタン。WrapUpViewと違いセッション辞書を持たない
    (押した時点で idea_filename/candidate_filenames は確定済み)。
    """

    def __init__(self, idea_filename: str, candidate_filenames: list[str]):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.idea_filename = idea_filename
        self.candidate_filenames = candidate_filenames
        self.selected = list(candidate_filenames)  # Selectのdefault=Trueと揃える (未操作なら全選択)
        self.message: discord.Message | None = None
        # WrapUpViewと同じ連打対策 (grill指摘): busyはdefer()より前に立てる。
        # 後に立てると2回目のクリックが1回目のdefer中に割り込みチェックをすり抜ける。
        self.busy = False
        self.add_item(RelatedNotesSelect(candidate_filenames))

    async def _disable_and_edit(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()

    @discord.ui.button(label="✅ 確定", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.busy:
            await interaction.response.send_message("⏳ 処理中です。少々お待ちください。", ephemeral=True)
            return
        self.busy = True
        try:
            await interaction.response.defer()
            if not self.selected:
                result = None
            else:
                result = await asyncio.to_thread(
                    tool_confirm_related_notes, self.idea_filename, self.selected
                )
        finally:
            self.busy = False
        await self._disable_and_edit()
        if result is None:
            await interaction.followup.send("⏭️ 選択なしのためスキップしました。")
            return
        icon = "❌" if is_failure_result(result) else "✅"
        await interaction.followup.send(f"{icon} {result}")


async def _notify_related_notes(target, idea_filename: str, query: str):
    """Vault内の関連ノート候補を検索し、あれば target (Thread/Channel) に通知する。

    候補0件なら何も送らない (通知疲れ防止)。target は .send を持てば Thread/Channel どちらでも良い
    — /brainstorm 保存直後は壁打ちスレッド、/idea-relink はコマンド実行チャンネルを渡す。
    """
    candidates = await asyncio.to_thread(tool_find_related_notes, query, idea_filename)
    if not candidates:
        return

    filenames = [c["filename"] for c in candidates]
    lines = "\n".join(f"- [[{os.path.splitext(f)[0]}]]" for f in filenames)
    body = (
        f"🔍 **[[{os.path.splitext(idea_filename)[0]}]]** に関連しそうなノートが見つかりました"
        f"（全て選択済み・関係が薄いものはメニューで外してください）:\n{lines}"
    )

    view = RelatedNotesView(idea_filename, filenames)
    message = await target.send(body, view=view)
    view.message = message


async def _summarize_and_save(session: dict, thread: discord.Thread) -> str:
    """対話を要約し tool_create_note で保存する。tool 層の規約に合わせ Error 文字列を返す。

    session["source_title"] があれば (= /brainstorm が00_inboxのアイデアから始まった)、
    content先頭に `> Source: [[title]]` を足す。tool_create_note の
    _extract_and_integrate_sections がこの1行を自動的に「### 関連ノート」へ移設し、
    元のFleeting Noteへのバックリンクになる。保存成功後は元ノートを削除する
    (削除失敗はログのみ、要約自体の成功には影響させない — 副次的な後処理のため)。

    seed (元ノートの生テキスト) が quickadd の抜粋メモ (Tpl_Fleeting_Excerpt.md) 由来で
    `> from [[引用元]]` を含む場合、その引用元も同じ `> Source:` 形式で追加する。
    _extract_and_integrate_sections は同じ行を複数拾えるので、元アイデアの削除で
    失われないこの引用元も一緒に「### 関連ノート」へ残る。AI要約 (summarize_idea_session)
    に任せると本文の取捨選択で引用元が消えうるため、生テキストから機械的に拾う。

    保存成功後、関連ノートの検索・通知も行う (失敗してもこの関数の戻り値には影響させない —
    削除失敗と同じ「副次的な後処理」の扱い)。
    """
    idea = await asyncio.to_thread(summarize_idea_session, session["seed"], session["turns"])
    if not idea or not idea.get("content"):
        return "Error: 要約の生成に失敗しました。"

    content = idea["content"]
    source_title = session.get("source_title")
    source_lines = []
    if source_title:
        source_lines.append(f"> Source: [[{source_title}]]")
    citation = extract_excerpt_citation(session["seed"])
    if citation and citation != f"[[{source_title}]]":
        source_lines.append(f"> Source: {citation}")
    if source_lines:
        content = "\n".join(source_lines) + f"\n\n{content}"

    filename = (idea.get("title") or session["seed"])[:24]
    result = await asyncio.to_thread(
        tool_create_note, filename, content, idea.get("summary", "")
    )

    if source_title and not is_failure_result(result):
        del_result = await asyncio.to_thread(tool_delete_idea, source_title)
        if is_failure_result(del_result):
            print(f"[bot_commands_idea] ⚠️ Failed to delete source idea [[{source_title}]]: {del_result}")

    if not is_failure_result(result):
        m = _CREATED_FILENAME_RE.search(result)
        if m:
            try:
                await _notify_related_notes(thread, m.group(1), idea.get("summary", ""))
            except Exception as e:
                print(f"[bot_commands_idea] ⚠️ Failed to notify related notes: {e}")

    return result


async def cmd_brainstorm(interaction: discord.Interaction, text: str):
    """AIと1問1答を繰り返す壁打ちセッションを開始する。

    text が "idea:{title}" (オートコンプリート経由) なら00_inboxの該当ノートを読み込んで
    シードにする。それ以外は従来通り text をそのまま新規アイデアの種にする。
    """
    await interaction.response.defer(ephemeral=True)

    source_title = None
    seed = text
    if text.startswith(IDEA_REF_PREFIX):
        source_title = text[len(IDEA_REF_PREFIX):]
        note_content = await asyncio.to_thread(tool_read_note, source_title)
        if is_failure_result(note_content):
            await interaction.followup.send(f"❌ {note_content}", ephemeral=True)
            return
        seed = note_content

        # quickadd抜粋メモ (`> [!INFO] コンテキスト\n> from [[元ファイル]]`) なら、断片だけでなく
        # 元ファイルの内容もseedに含める (壁打ちの質問が断片だけを見て組み立てられていた問題への対処)。
        # 元ファイルが見つからない等は副次的失敗として無視し、抜粋メモ本文のみで続行する。
        citation = extract_excerpt_citation(note_content)
        if citation:
            m = _CREATED_FILENAME_RE.search(citation)
            if m:
                source_content = await asyncio.to_thread(tool_read_note, m.group(1))
                if not is_failure_result(source_content):
                    seed = f"{seed}\n\n---\n## 元ファイルの内容: [[{m.group(1)}]]\n{source_content}"
                else:
                    print(f"[bot_commands_idea] ⚠️ Failed to load excerpt source file [[{m.group(1)}]]: {source_content}")

    display_title = source_title or text
    try:
        thread = await interaction.channel.create_thread(
            name=f"💡壁打ち: {display_title[:20]}",
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
    await thread.send(f"## 💡 壁打ちセッションを開始します\n**お題**: {display_title}\n\n最初の質問を考え中...")

    question = await asyncio.to_thread(generate_next_question, seed, [])
    if not question:
        await thread.send("❌ 質問の生成に失敗しました。もう一度 `/brainstorm` を実行してください。")
        return
    if _log_to_daily_note:
        await asyncio.to_thread(_log_to_daily_note, "chat", f"/brainstorm {seed}", question)

    _brainstorm_sessions[thread.id] = {
        "user_id": interaction.user.id,
        "seed": seed,
        "source_title": source_title,
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
        if _log_to_daily_note:
            await asyncio.to_thread(
                _log_to_daily_note, "chat", message.content, question or "(質問生成失敗)")
        if not question:
            await message.channel.send("❌ 次の質問の生成に失敗しました。🏁まとめてボタンで終了できます。")
            return
        session["pending_question"] = question
        await _send_question(message.channel, question)
    finally:
        session["busy"] = False


async def permanent_idea_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """/idea-relink の title 引数向け。30_Mainの完成アイデアノート一覧を候補に出す。"""
    try:
        ideas = await asyncio.to_thread(tool_list_permanent_ideas)
    except Exception:
        return []
    current_lower = current.lower()
    return [
        app_commands.Choice(name=f"📄 {idea['title']}"[:100], value=idea["title"])
        for idea in ideas
        if current_lower in idea["title"].lower()
    ][:25]


async def cmd_idea_relink(interaction: discord.Interaction, title: str):
    """既存の完成アイデアノートについて、Vault内の関連ノートを能動的に再検索する。"""
    await interaction.response.defer(ephemeral=True)

    note_content = await asyncio.to_thread(tool_read_note, title)
    if is_failure_result(note_content):
        await interaction.followup.send(f"❌ {note_content}", ephemeral=True)
        return

    query = extract_note_query(note_content)

    await interaction.followup.send(f"🔍 [[{title}]] の関連ノートを検索します...", ephemeral=True)
    await _notify_related_notes(interaction.channel, title, query)
