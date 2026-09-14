# -*- coding: utf-8 -*-
"""
目標駆動の記事プロジェクト作成 (/article-project) コマンド。

既存の /article (bot_commands_article.py) は「下書きメモがまずある」前提で
インタビュー→執筆→推敲するが、こちらは「目標だけがあり、Vault内の既存知識を検索して
参考資料を採用し、不足情報をインタビューで埋めてから記事化する」逆方向のフロー。
UXの前提が異なるため /article を拡張せず独立コマンドにする（_article_sessions とは
別のセッション辞書 _project_sessions を持つ）。

管理ノート (tools/tool_article_project.py) をObsidian上の正本として、目標・採用資料・
Q&Aログ・完成下書きを都度書き込む。qa_pairs全体(未回答含む)もfrontmatterに永続化する
ため、Bot再起動後は restore_sessions() が管理ノートから _project_sessions を再構築できる
(要件④)。記事完成(status=draft_done)後は自然文の返信を修正依頼として扱い、
🏁ボタンで status=completed に確定する。

個人情報の絶対除外: tools.tool_article_project.sanitize_reference_material が
ファイル単位で「パーソナル情報」セクションを機械的に除去し、見出しの書式が崩れていて
除去しきれなかったファイルは参考資料そのものから除外する (警告して続行はしない)。
本人がQ&Aで直接語った体験談はこの対象外。

手動編集検知: tool_article_project 側で書き込み直前にディスクmtimeを突き合わせ、
ユーザーがObsidianで管理ノート/下書きを直接編集していた場合は上書きせず
{base}_conflict.md / {base}_Draft_conflict.md へ退避する。このモジュールは
save_final_draft の戻り値 (成功したか, 競合時の退避ファイル名) を見て案内するだけ。
競合検出後、tool_article_project側はディスク上の現在値(手動編集版)を新しい比較基準
として採用し直すため、次回の保存試行は競合を再検出しない — このモジュールは
DraftConflictView でAI版採用/手動編集版維持をボタンにより明示選択させ、無関係な
発言をきっかけに手動編集が黙って上書きされることを防ぐ(レビュー指摘AP-102)。

完成確定(ArticleProjectReflectionModal.on_submit)は自然文修正(_handle_revision_reply)と
同じスレッドロックで直列化する(レビュー指摘AP-103)。また promote_and_reflect は
未採用の出典が残っている限り正式ノート化を拒否する(レビュー指摘AP-101)。
"""
import os
import asyncio

import discord

from tools.tool_article_project import (
    create_project, load_project, load_draft_article, update_project_fields,
    append_project_section, sanitize_reference_material, save_final_draft,
    save_adopted_sources, record_qa_answer, list_projects,
    promote_and_reflect, build_dashboard_text,
)
from tools.tool_vault_hybrid import tool_find_related_notes
from scripts.generate_experiential_article import (
    extract_missing_info_questions, generate_final_article, run_article_reflection_loop,
    apply_user_revision,
)
from lib.bot_errors import log_command_error
from lib.discord_response import send_long_message
from commands.bot_views import AllowedUserView

# 個人情報が密集する既知の単一状態ノート。知識検索の候補一覧にはそもそも出さない
# (sanitize_reference_material によるファイル単位の機械的除去・除外が本体の防御)。
_EXCLUDED_CANDIDATE_FILENAMES = {"User_Profile.md", "User_Memory.md"}

# thread_id -> {"user_id", "filename", "goal", "audience", "reference_material",
#               "qa_pairs", "current_index", "generating", "pending_save_article"}
_project_sessions = {}

# thread_id -> filename。status=draft_done/completed のプロジェクトだけを覚える軽量な
# ルーティングキャッシュ。on_message のホットパスで無関係なスレッドへの発言のたびに
# ディレクトリスキャンしないため(_project_sessionsに無ければ即returnできる)。
_completed_projects: dict[int, str] = {}

# thread_id -> asyncio.Lock。同一スレッドの回答処理・保存再試行・自然文修正を直列化する。
# generating フラグだけでは、Q&A保存の await 中に次の返信が届くと両方が
# 「質問終了」を検知して send_next_question (ひいては記事生成) を二重に走らせてしまう
# レースを防げない (レビュー指摘4) — フラグは非同期処理の合間に読み書きされる限り
# チェック直後の割り込みを防げないので、ロックでスレッド単位に直列化する。
_thread_locks: dict[int, asyncio.Lock] = {}


def _get_thread_lock(thread_id: int) -> asyncio.Lock:
    lock = _thread_locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _thread_locks[thread_id] = lock
    return lock


def _end_session(thread_id: int) -> None:
    _project_sessions.pop(thread_id, None)
    _thread_locks.pop(thread_id, None)


class ArticleProjectGoalModal(discord.ui.Modal, title="記事プロジェクトの目標設定"):
    goal = discord.ui.TextInput(
        label="目標",
        placeholder="例: 未経験からAIエージェントを作った経験を、初心者向けに記事化したい",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=10,
    )
    audience = discord.ui.TextInput(
        label="想定読者（任意）",
        placeholder="例: プログラミング未経験の社会人",
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        thread = await interaction.channel.create_thread(
            name=f"🎯記事プロジェクト: {self.goal.value[:20]}...",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60,
        )
        await interaction.followup.send(f"スレッドを作成しました: {thread.mention}", ephemeral=True)

        filename, error = await asyncio.to_thread(
            create_project, self.goal.value, self.audience.value, thread.id, interaction.user.id
        )
        if error:
            await thread.send(f"❌ 管理ノートの作成に失敗しました: {error}")
            return

        await thread.send(
            f"## 🎯 記事プロジェクトを開始します\n**目標**: {self.goal.value}\n\n"
            "関連する知識をVaultから検索中... 少々お待ちください。"
        )

        try:
            candidates = await asyncio.to_thread(tool_find_related_notes, self.goal.value, "", 8)
        except Exception as e:
            log_command_error("/article-project (knowledge search)", e)
            candidates = []
        filenames = [
            c["filename"] for c in candidates
            if c["filename"] not in _EXCLUDED_CANDIDATE_FILENAMES
        ]

        if not filenames:
            saved = await asyncio.to_thread(
                save_adopted_sources, filename, [], "関連ノートは見つかりませんでした。"
            )
            if not saved:
                # このボタン無し経路では再試行の手段がユーザー操作に無いため、
                # 保存が確認できるまでQ&Aフェーズへは進めずやり直しを案内する
                # (レビュー指摘: 採用履歴の保存成功を確実にする)。
                await thread.send(
                    "⚠️ 採用資料の記録に失敗しました。もう一度 /article-project からやり直してください。"
                )
                return
            await _start_qa_phase(thread, filename, self.goal.value, interaction.user.id, "", self.audience.value)
            return

        view = AdoptedSourcesView(filename, self.goal.value, interaction.user.id, filenames, self.audience.value)
        lines = "\n".join(f"- [[{os.path.splitext(f)[0]}]]" for f in filenames)
        message = await thread.send(
            f"🔍 関連しそうなノートが見つかりました（全て選択済み・使わないものはメニューで外してください）:\n{lines}",
            view=view,
        )
        view.message = message


async def append_project_section_async(filename: str, heading: str, text: str) -> bool:
    return await asyncio.to_thread(append_project_section, filename, heading, text)


async def _save_final_article(filename: str, final_article: str) -> tuple[bool, str | None]:
    """記事完成時の保存。tools.tool_article_project.save_final_draft が status 更新と
    本文置換を1回のload/writeでアトミックに行うため、再試行しても本文が重複せず、
    status と本文が食い違う中間状態も作らない (レビュー指摘2)。

    戻り値: (成功したか, 手動編集検知で退避したバックアップファイル名 or None)。
    """
    return await asyncio.to_thread(save_final_draft, filename, final_article)


class AdoptedSourcesSelect(discord.ui.Select):
    """採用資料のチェック式選択（既定全選択、bot_commands_idea.RelatedNotesSelectと同型）。"""

    def __init__(self, candidate_filenames: list[str]):
        options = [
            discord.SelectOption(
                label=os.path.splitext(f)[0][:100], value=f, default=True,
            )
            for f in candidate_filenames
        ]
        super().__init__(
            placeholder="参考資料として採用するノートを選択（不要なものは外してください）",
            min_values=0, max_values=len(options), options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.selected = self.values


class AdoptedSourcesView(AllowedUserView):
    """採用資料の確定ボタン。確定後に不足情報の質問生成・Q&Aループを開始する。"""

    def __init__(self, filename: str, goal: str, user_id: int, candidate_filenames: list[str], audience: str = ""):
        super().__init__(timeout=1800)
        self.filename = filename
        self.goal = goal
        self.user_id = user_id
        self.audience = audience
        self.candidate_filenames = candidate_filenames
        self.selected = list(candidate_filenames)
        self.message: discord.Message | None = None
        self.busy = False
        self.add_item(AdoptedSourcesSelect(candidate_filenames))

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

            reference_material = ""
            excluded: list[str] = []
            if self.selected:
                reference_material, excluded = await asyncio.to_thread(
                    sanitize_reference_material, self.selected
                )

            adopted = [f for f in self.selected if f not in excluded]
            adopted_lines = "\n".join(f"- [[{os.path.splitext(f)[0]}]]" for f in adopted) or "（採用なし）"
            log_text = f"採用した資料:\n{adopted_lines}"
            if excluded:
                excluded_lines = "\n".join(f"- [[{os.path.splitext(f)[0]}]]" for f in excluded)
                log_text += f"\n\n個人情報セクションを機械的に除去できず除外した資料:\n{excluded_lines}"

            saved = await asyncio.to_thread(save_adopted_sources, self.filename, adopted, log_text)
        finally:
            self.busy = False

        thread = interaction.channel
        if not saved:
            # 採用履歴の保存が確認できるまで次のフェーズへは進めない。ボタンは
            # 無効化せず (_disable_and_edit を呼ばない)、同じ選択内容のまま
            # ユーザーがもう一度押せば再試行できるようにする (レビュー指摘)。
            await thread.send("⚠️ 採用資料の記録に失敗しました。もう一度「✅ 確定」ボタンを押してください。")
            return

        await self._disable_and_edit()
        if excluded:
            excluded_names = "、".join(os.path.splitext(f)[0] for f in excluded)
            await thread.send(
                f"⚠️ {excluded_names} は個人情報セクションを機械的に除去できなかったため、参考資料から除外しました。"
            )
        await thread.send(f"📚 参考資料を採用しました（{len(adopted)}件）。不足情報を確認します...")
        await _start_qa_phase(thread, self.filename, self.goal, self.user_id, reference_material, self.audience)


class ArticleProjectReflectionModal(discord.ui.Modal, title="完成の振り返り"):
    """🏁ボタン押下後に表示する振り返り入力Modal。送信すると正式ノート化まで一括で行う
    (promote_and_reflect)。「使った知識」「追加した経験」はGeminiに生成させず、既存の
    adopted_source_files/qa_pairsから機械的に導出できるため、ユーザーには「学び」
    「次に試すこと」の自由記述だけを求める(資料にない体験を捏造しない既存方針と同じ理由)。

    Modalはインタラクション単位で完結するインスタンスであり、永続View
    (ArticleProjectCompleteView)のようにスレッド間で共有される状態を持たないため、
    busyフラグ共有の罠を踏みようがない。
    """
    learning = discord.ui.TextInput(
        label="学んだこと",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=3,
    )
    next_action = discord.ui.TextInput(
        label="次に試したいこと（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
    )

    def __init__(self, filename: str, view: "ArticleProjectCompleteView",
                 button: discord.ui.Button, message: discord.Message):
        super().__init__()
        self.filename = filename
        self.view = view
        self.button = button
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # 自然文修正(_handle_revision_reply)と同じスレッドロックで直列化する。ロックが
        # 無いと、修正保存の途中でstatusをdraft_doneへ戻す処理と完成確定が競合し、
        # 古い記事を正式化した直後にstatusがdraft_doneへ巻き戻る可能性があった
        # (レビュー指摘AP-103)。
        async with _get_thread_lock(interaction.channel.id):
            main_filename, error = await asyncio.to_thread(
                promote_and_reflect, self.filename, self.learning.value, self.next_action.value
            )
            if error:
                await interaction.followup.send(
                    f"⚠️ 完成処理に失敗しました: {error}\nもう一度🏁 完成にするボタンを押してください。"
                )
                return

            self.button.disabled = True
            try:
                await self.message.edit(view=self.view)
            except discord.HTTPException:
                pass

        await interaction.followup.send(f"🏁 正式ノート化しました: [[{os.path.splitext(main_filename)[0]}]]")


class ArticleProjectCompleteView(AllowedUserView):
    """記事完成を明示的に確定する永続ボタン(draft_done -> completed、正式ノート化まで一括)。

    永続View(on_readyでbot.add_view登録、custom_id固定)であり、以後どのメッセージの
    クリックも同じコールバックに配送される。WrapUpView型のインスタンス変数busyフラグを
    そのまま持ち込むと全スレッド共通の1個のフラグになり、Aプロジェクトの完成処理中に
    Bプロジェクトの完成ボタンまで「処理中です」で弾かれる誤動作を生む(設計時に発見)。
    代わりに「クリック時点でstatusを読み直し、既にcompletedなら何もせず案内するだけ」
    という冪等な状態チェックで二重クリックを吸収する。
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🏁 完成にする", style=discord.ButtonStyle.success,
                        custom_id="article_project:complete")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        filename = _completed_projects.get(interaction.channel.id)
        if filename is None:
            await interaction.response.send_message(
                "⚠️ このスレッドに対応する記事プロジェクトが見つかりません。", ephemeral=True
            )
            return

        project = await asyncio.to_thread(load_project, filename)
        if project is None:
            await interaction.response.send_message("⚠️ 管理ノートが見つかりません。", ephemeral=True)
            return

        frontmatter = project["frontmatter"]
        owner_id = frontmatter.get("user_id")
        if owner_id is not None and owner_id != interaction.user.id:
            await interaction.response.send_message(
                "⛔ このプロジェクトの開始者のみ操作できます。", ephemeral=True
            )
            return

        if frontmatter.get("status") == "completed":
            await interaction.response.send_message("✅ 既に完成済みです。", ephemeral=True)
            return

        # 完成確定と正式ノート化を1操作にまとめるため、defer()ではなくModalを最初の
        # レスポンスとして返す(「completedなのに未正式化」という中間状態を作らない)。
        await interaction.response.send_modal(
            ArticleProjectReflectionModal(filename, self, button, interaction.message)
        )


class DraftConflictView(AllowedUserView):
    """手動編集検知で保留中のAI生成記事(pending_save_article)を、AI版で上書きするか
    手動編集版のまま維持するかをユーザーに明示選択させる(レビュー指摘AP-102)。

    tool_article_project側は競合検出時にディスク上の現在値(＝手動編集版)を新しい
    比較基準として採用し直す(_rebase_conflict_baseline)ため、次回の保存試行は
    競合を再検出しない。この設計のまま「次に送ったメッセージで自動的に保留中のAI版を
    上書き保存する」という以前のUXを続けると、無関係な発言をきっかけに手動編集が
    黙って上書きされてしまう。ボタンによる明示選択に置き換えることでこれを防ぐ。
    """

    def __init__(self, thread_id: int, filename: str, backup_filename: str):
        super().__init__(timeout=1800)
        self.thread_id = thread_id
        self.filename = filename
        self.backup_filename = backup_filename
        self.message: discord.Message | None = None
        self.busy = False

    async def _disable_and_edit(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()

    async def _resolve(self, interaction: discord.Interaction, adopt_ai: bool):
        if self.busy:
            await interaction.response.send_message("⏳ 処理中です。少々お待ちください。", ephemeral=True)
            return
        session = _project_sessions.get(self.thread_id)
        self.busy = True
        try:
            await interaction.response.defer()
            saved, backup_filename = True, None
            if adopt_ai:
                if session is None or session.get("pending_save_article") is None:
                    await interaction.followup.send("⚠️ このセッションは既に処理済みです。")
                    return
                saved, backup_filename = await _save_final_article(
                    self.filename, session["pending_save_article"]
                )
        finally:
            self.busy = False

        await self._disable_and_edit()
        if session is not None:
            session["pending_save_article"] = None
            session["pending_conflict"] = False
        _completed_projects[self.thread_id] = self.filename
        _end_session(self.thread_id)

        if saved:
            msg = "🎉 AI版で上書き保存しました。" if adopt_ai else "✅ 手動編集版を維持しました。"
            msg += "準備ができたら下のボタンで完成を確定するか、修正したい点をこのスレッドに送ってください。"
            await interaction.followup.send(msg, view=ArticleProjectCompleteView())
        else:
            msg = "⚠️ 保存に失敗しました。"
            if backup_filename:
                msg += f" 手動編集版は `{backup_filename}` として保存済みです。"
            msg += " 修正したい点があればこのスレッドに送ってください。"
            await interaction.followup.send(msg, view=ArticleProjectCompleteView())

    @discord.ui.button(label="🔄 AI版を採用（手動編集を上書き）", style=discord.ButtonStyle.danger)
    async def adopt_ai(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, adopt_ai=True)

    @discord.ui.button(label="🗑️ 手動編集版を維持（AI版を破棄）", style=discord.ButtonStyle.secondary)
    async def keep_manual(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, adopt_ai=False)


async def _start_qa_phase(
    thread: discord.Thread, filename: str, goal: str, user_id: int,
    reference_material: str, audience: str = "",
):
    """不足情報の質問を生成し、Q&Aセッションを開始する。"""
    points = await asyncio.to_thread(extract_missing_info_questions, goal, reference_material, audience)
    if not points:
        await thread.send("❌ 不足情報の質問生成に失敗しました。もう一度 /article-project からやり直してください。")
        return

    for p in points:
        p.setdefault("answer", None)

    # 質問リスト全体(未回答含む)を先に永続化しておく。1問も回答されないうちに
    # Botが落ちても、次回restore_sessionsが質問文・出題順を復元できるようにするため
    # (要件④: 以前は回答済みログのテキストしかノートに残らず、未回答の質問文が
    # セッションメモリ依存だった)。
    saved = await asyncio.to_thread(update_project_fields, filename, qa_pairs=points)
    if not saved:
        await thread.send("⚠️ 質問リストの保存に失敗しました。もう一度 /article-project からやり直してください。")
        return

    _project_sessions[thread.id] = {
        "user_id": user_id,
        "filename": filename,
        "goal": goal,
        "audience": audience,
        "reference_material": reference_material,
        "qa_pairs": points,
        "current_index": 0,
        "generating": False,
        "pending_save_article": None,
        "pending_conflict": False,
    }

    await thread.send(
        f"{len(points)}つの不足情報について質問します。\nあなたの**「生の言葉・体験」**で答えてください。"
    )
    async with _get_thread_lock(thread.id):
        await send_next_question(thread)


async def send_next_question(thread: discord.Thread):
    session = _project_sessions.get(thread.id)
    if not session:
        return

    idx = session["current_index"]
    if idx < len(session["qa_pairs"]):
        p = session["qa_pairs"][idx]
        msg = f"**【質問 {idx + 1}/{len(session['qa_pairs'])}】**\n"
        msg += f"### {p['point']}\n"
        msg += f"> {p['question']}"
        await thread.send(msg)
        return

    # 全質問終了 → 生成中フラグを立て、以降の返信は handle_project_reply でガードする
    # (記事生成中に追加の返信が来ても質問配列の範囲外アクセスを起こさないため)。
    session["generating"] = True
    await thread.send("✨ すべての質問に回答いただきました！記事の初稿を執筆します... (1-2分かかります)")

    draft_article = await asyncio.to_thread(
        generate_final_article, session["goal"], session["qa_pairs"],
        session["reference_material"], session["audience"],
    )
    if not draft_article:
        await thread.send("❌ 記事の生成に失敗しました。")
        _end_session(thread.id)
        return

    await thread.send("📝 初稿の作成が完了しました。続いてプロ編集者（AI）の視点による品質チェックと推敲を実行します... 🔍")
    final_article = await asyncio.to_thread(
        run_article_reflection_loop,
        draft_article, session["goal"], session["qa_pairs"], 2,
        session["reference_material"], session["audience"],
    )

    if len(final_article) > 1900:
        parts = [final_article[i:i + 1900] for i in range(0, len(final_article), 1900)]
        for part in parts:
            await thread.send(part)
    else:
        await thread.send(final_article)

    saved, backup_filename = await _save_final_article(session["filename"], final_article)
    if saved:
        await thread.send(
            "🎉 下書きが完成し、管理ノートに保存しました！準備ができたら下のボタンで完成を確定するか、"
            "修正したい点をこのスレッドに送ってください。",
            view=ArticleProjectCompleteView(),
        )
        _completed_projects[thread.id] = session["filename"]
        _end_session(thread.id)
    elif backup_filename:
        # 下書きファイルが手動編集されていた。上書きせず退避済みなので、内容を保持し
        # AI版採用/手動編集版維持をボタンで明示選択させる(レビュー指摘AP-102、
        # DraftConflictView参照)。
        session["pending_save_article"] = final_article
        session["pending_conflict"] = True
        session["generating"] = False
        view = DraftConflictView(thread.id, session["filename"], backup_filename)
        message = await thread.send(
            f"⚠️ 記事ファイルが手動編集されていたため上書きを避け、あなたの版を `{backup_filename}` として保存しました。"
            "AI版を採用するか、手動編集版を維持するか選んでください。",
            view=view,
        )
        view.message = message
    else:
        # 保存失敗を無視してセッションを消すと、記事が完成しているのに管理ノートには
        # 何も残らず、しかも二度と再保存できなくなる。内容を保持し再試行を待つ。
        session["pending_save_article"] = final_article
        session["generating"] = False
        await thread.send(
            "⚠️ 記事は完成しましたが、管理ノートへの保存に失敗しました。"
            "このスレッドに何か送信すると保存を再試行します。"
        )


async def _handle_qa_reply(message: discord.Message):
    """Q&A進行中セッションへの返信を処理する(ロックの中から呼ばれる)。"""
    session = _project_sessions.get(message.channel.id)
    if not session:
        return

    if session.get("pending_save_article") is not None:
        if session.get("pending_conflict"):
            # 手動編集競合が未解決。以前は「何か送信すると自動的にAI版で上書き」だったが、
            # rebase後は次の保存試行が競合を再検出しないため、無関係な発言で手動編集が
            # 黙って上書きされてしまう(レビュー指摘AP-102)。ボタンでの明示選択のみを促す。
            await message.channel.send(
                "⏳ 記事ファイルの競合が未解決です。上のメッセージのボタンでAI版採用/手動編集版維持を選んでください。"
            )
            return
        saved, backup_filename = await _save_final_article(
            session["filename"], session["pending_save_article"]
        )
        if saved:
            await message.channel.send(
                "🎉 保存に成功しました。準備ができたら下のボタンで完成を確定するか、"
                "修正したい点をこのスレッドに送ってください。",
                view=ArticleProjectCompleteView(),
            )
            _completed_projects[message.channel.id] = session["filename"]
            _end_session(message.channel.id)
        elif backup_filename:
            session["pending_conflict"] = True
            view = DraftConflictView(message.channel.id, session["filename"], backup_filename)
            m = await message.channel.send(
                f"⚠️ 記事ファイルが手動編集されていたため上書きを避け、あなたの版を `{backup_filename}` として保存しました。"
                "AI版を採用するか、手動編集版を維持するか選んでください。",
                view=view,
            )
            view.message = m
        else:
            await message.channel.send(
                "⚠️ まだ管理ノートへの保存に失敗しています。もう一度送信するか、時間をおいてお試しください。"
            )
        return

    if session.get("generating"):
        await message.channel.send("⏳ 記事を生成中です。しばらくお待ちください。")
        return

    idx = session["current_index"]
    if idx >= len(session["qa_pairs"]):
        # 全質問終了後、generating が立つ直前の隙間で届いた返信。範囲外アクセスを避ける。
        await message.channel.send("⏳ 処理中です。しばらくお待ちください。")
        return

    qa = session["qa_pairs"][idx]
    qa["answer"] = message.content
    # 保存成功を確認してから current_index を進める。保存に失敗したら回答を元に戻し、
    # ユーザーが同じ内容を再送すれば同じ回答で保存を再試行できるようにする
    # (レビュー指摘3: 以前は戻り値を無視して無条件に進めていた)。
    saved = await asyncio.to_thread(
        record_qa_answer, session["filename"], session["qa_pairs"], qa["question"], message.content
    )
    if not saved:
        qa["answer"] = None
        await message.channel.send("⚠️ 回答の保存に失敗しました。もう一度同じ内容を送信してください。")
        return

    session["current_index"] += 1
    await send_next_question(message.channel)


async def _handle_revision_reply(message: discord.Message, filename: str):
    """完成済み(draft_done/completed)プロジェクトのスレッドへの自然文返信を処理する
    (ロックの中から呼ばれる)。draft_done のときだけ修正依頼として反映する。"""
    project = await asyncio.to_thread(load_project, filename)
    if project is None:
        return
    frontmatter = project["frontmatter"]

    owner_id = frontmatter.get("user_id")
    if owner_id is not None and owner_id != message.author.id:
        return

    status = frontmatter.get("status")
    if status == "completed":
        promoted = frontmatter.get("promoted_filename")
        # _Draft.md側を直しても正式ノートには反映されないため、修正すべき正しいファイルを案内する
        # (promoted_filenameが無い旧データ・想定外の状態のみ汎用の案内にフォールバック)。
        if promoted:
            await message.channel.send(
                f"✅ このプロジェクトは完成済みです。修正が必要な場合は"
                f" [[{os.path.splitext(promoted)[0]}]] をObsidianで直接編集してください。"
            )
        else:
            await message.channel.send(
                "✅ このプロジェクトは完成済みです。修正が必要な場合は Obsidian で直接編集してください。"
            )
        return
    if status != "draft_done":
        return

    article = await asyncio.to_thread(load_draft_article, filename)
    if article is None:
        return

    await message.channel.send("✏️ 修正を反映しています...")

    reference_material, _ = await asyncio.to_thread(
        sanitize_reference_material, frontmatter.get("adopted_source_files") or []
    )
    audience = frontmatter.get("audience", "")

    revised = await asyncio.to_thread(
        apply_user_revision, article, message.content, reference_material, audience
    )
    if not revised:
        await message.channel.send("❌ 修正に失敗しました。もう一度お試しください。")
        return

    if len(revised) > 1900:
        parts = [revised[i:i + 1900] for i in range(0, len(revised), 1900)]
        for part in parts:
            await message.channel.send(part)
    else:
        await message.channel.send(revised)

    saved, backup_filename = await _save_final_article(filename, revised)
    if saved:
        await message.channel.send("修正版を保存しました。", view=ArticleProjectCompleteView())
    elif backup_filename:
        await message.channel.send(
            f"⚠️ 記事ファイルが手動編集されていたため上書きを避け、あなたの版を `{backup_filename}` として保存しました。"
            "修正結果は上に表示した通りですが保存はされていません。もう一度修正内容を送ってください。"
        )
    else:
        await message.channel.send("⚠️ 修正内容の保存に失敗しました。もう一度送信してください。")


async def handle_project_reply(message: discord.Message):
    """メッセージが記事プロジェクトスレッド内での返信かどうかを判定し、処理する。

    bot_discord.py の on_message から呼ばれることを想定。スレッド単位のロックで
    回答処理・保存再試行・自然文修正を直列化する — ロックが無いと、保存の await 中に
    届いた次の返信が同じ状態を読んでしまい、両方が「質問終了」を検知して記事生成を
    二重に走らせることがある (レビュー指摘4)。
    """
    if message.author.bot:
        return

    session = _project_sessions.get(message.channel.id)
    if session:
        if message.author.id != session["user_id"]:
            return
        async with _get_thread_lock(message.channel.id):
            await _handle_qa_reply(message)
        return

    filename = _completed_projects.get(message.channel.id)
    if filename is None:
        return  # 無関係なスレッドへの発言は無視、on_messageのホットパスを汚さない

    async with _get_thread_lock(message.channel.id):
        await _handle_revision_reply(message, filename)


async def _restore_one_project(bot_client, project: dict) -> None:
    filename = project["filename"]
    frontmatter = project["frontmatter"]
    thread_id = frontmatter.get("thread_id")
    status = frontmatter.get("status")
    if not thread_id:
        return

    thread = bot_client.get_channel(thread_id)
    if thread is None:
        thread = await bot_client.fetch_channel(thread_id)

    if status == "qa":
        qa_pairs = frontmatter.get("qa_pairs") or []
        current_index = next(
            (i for i, qa in enumerate(qa_pairs) if qa.get("answer") is None), len(qa_pairs)
        )
        reference_material, _ = await asyncio.to_thread(
            sanitize_reference_material, frontmatter.get("adopted_source_files") or []
        )
        _project_sessions[thread_id] = {
            "user_id": frontmatter.get("user_id"),
            "filename": filename,
            "goal": frontmatter.get("goal", ""),
            "audience": frontmatter.get("audience", ""),
            "reference_material": reference_material,
            "qa_pairs": qa_pairs,
            "current_index": current_index,
            "generating": False,
            "pending_save_article": None,
            "pending_conflict": False,
        }
        await thread.send("🔄 Bot再起動のため再開しました。続きから進めます。")
        async with _get_thread_lock(thread_id):
            await send_next_question(thread)
    elif status == "goal_set":
        # 資料採用候補一覧がどこにも永続化されておらず完全復元は不可能。
        # ユーザー入力がまだ無い段階なのでやり直しコストが低く、無理に再開しない。
        await thread.send(
            "⚠️ このプロジェクトは資料採用の確認前にBotが再起動しました。"
            "お手数ですが `/article-project` からやり直してください。"
        )
    elif status in ("draft_done", "completed"):
        # 過去の完成済みプロジェクトが再起動のたびにスレッド通知でスパムにならないよう、
        # ルーティングキャッシュへの登録のみ行い通知は送らない。
        _completed_projects[thread_id] = filename


async def restore_sessions(bot_client) -> None:
    """on_ready から呼ぶ。管理ノートから進行中プロジェクトを列挙し、スレッドを取得できた
    ものだけ状態を再構築する。1件の失敗が他プロジェクトの復元を止めないよう、
    プロジェクトごとに例外を個別catchする(Bot起動自体も壊さない)。"""
    projects = await asyncio.to_thread(list_projects, ["goal_set", "qa", "draft_done", "completed"])
    for project in projects:
        try:
            await _restore_one_project(bot_client, project)
        except Exception as e:
            log_command_error(f"/article-project restore ({project['filename']})", e)


async def cmd_article_project(interaction: discord.Interaction):
    """/article-project コマンドのエントリポイント。"""
    await interaction.response.send_modal(ArticleProjectGoalModal())


async def cmd_article_project_list(interaction: discord.Interaction):
    """/article-project-list コマンドのエントリポイント。常駐ジョブ・永続ファイルは
    作らず、都度list_projects()から build_dashboard_text() で組み立てて表示するだけ。"""
    await interaction.response.defer(ephemeral=True)
    projects = await asyncio.to_thread(list_projects)
    await send_long_message(interaction, build_dashboard_text(projects), title="記事プロジェクト一覧")
