# -*- coding: utf-8 -*-
import discord
import os
import asyncio
from lib.lib_persona import add_client_persona, get_all_personas
from tools.tool_client_review import evaluate_deliverable, revise_deliverable, _save_improvement_log
from engine_self_improve import analyze_weak_tools
from lib.lib_shared import VAULT_ROOT
from lib.cost_guard import GeminiBudgetExceeded
from commands.bot_views import AllowedUserView
from lib.bot_errors import log_command_error

# View の有効期限 (最後の操作からの idle 秒数)。
# かつて timeout=None だったが、この View は改訂対象テキスト・評価結果・押下回数を
# すべて instance 変数で持つ (= プロセスのメモリにしか無い) ため、永続 View の要件
# 「ステートレスなコールバック」(commands/bot_views.py の設計) を満たさない。
# bot.add_view() での再登録もできないので、Bot 再起動後はボタンが無言で死んでいた
# (押しても "インタラクションに失敗しました" すら出ないことがある)。
# 嘘の永続をやめ、期限切れをユーザーに見える形で伝える (2026-07-26)。
#
# 15分ではなく10分にしている理由: on_timeout はメッセージを編集して期限切れを
# 伝えるが、interaction.followup.send() が返す WebhookMessage の編集は
# **interaction token 経由**で、このトークンは発行から15分で失効する。
# 期限を15分ちょうどにすると通知の edit がトークン失効と同時になり、
# 「無言の死を直す修正の通知だけが無言で失敗する」ことになる (grill 指摘)。
INTERACTION_TOKEN_LIFETIME = 900    # Discord 仕様: interaction token の寿命
VIEW_TIMEOUT_SECONDS = 600          # 10分 (最後の操作から) — 上記より短くすること
LOOP_GRACE_SECONDS = 1800           # 改訂ループ実行中の期限延長ぶん


class ClientReviewLoopView(AllowedUserView):
    # Tier2-b コストガード: キャップがないと 1押下=最大6 Gemini呼び出し
    # (改訂+再評価 ×3周) を無制限に繰り返せてしまう。
    # カウントは View instance 内 (in-memory)。
    # 期限切れ時は on_timeout がボタンを無効化するので押下回数も一緒に締まるが、
    # **Bot 再起動時はボタンが有効な見た目のまま残り、押すと Discord 側の
    # 「インタラクションに失敗しました」になる**。これは View を永続化しない限り
    # 解消できない (この View は改訂対象テキストをメモリに持つため永続化不可)。
    MAX_PRESSES = 3

    def __init__(self, deliverable: str, job_description: str, persona_id: str, current_eval: dict):
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.current_text = deliverable
        self.job_description = job_description
        self.persona_id = persona_id
        self.current_eval = current_eval
        self.press_count = 0
        # on_timeout でボタンを無効化するために、この View を載せた最新のメッセージを
        # 覚えておく。discord.py の View は message 属性を自動では持たない。
        self.message: discord.Message | None = None

        # 初期スコアと状態を保持（ログ用）
        self.initial_text = deliverable
        self.initial_eval = current_eval

        # 合格(pass)なら改善ボタンは無効化、不合格(revision_needed/fail)なら有効化
        status = current_eval.get("status", "fail")
        if status == "pass":
            self.btn_loop.disabled = True

    async def on_timeout(self):
        """期限切れ時にボタンを無効化し、理由をメッセージに残す。

        黙って効かなくなるのが所見3の症状そのものなので、必ず表示を更新する。
        """
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=(
                    f"⌛ この顧客レビューは有効期限({VIEW_TIMEOUT_SECONDS // 60}分)を過ぎたため"
                    "ボタンを終了しました。続けるには `/client-review` を再実行してください。"
                ),
                view=self,
            )
        except discord.HTTPException as e:
            # メッセージ削除済み等。ここで例外を上げても誰も受け取れないのでログのみ。
            log_command_error("ClientReviewLoopView.on_timeout", e, "❌ View 期限切れ表示の更新失敗")

    def _create_embed(self, eval_result: dict, iteration: int = 0) -> discord.Embed:
        scores = eval_result.get("scores", {})
        avg = scores.get("average", 0)
        status = eval_result.get("status", "fail")
        
        color = discord.Color.green() if status == "pass" else discord.Color.orange() if status == "revision_needed" else discord.Color.red()
        title_suffix = "✅ 合格 (Pass)" if status == "pass" else "⚠️ 要改訂 (Revision Needed)" if status == "revision_needed" else "❌ 不合格 (Fail)"
        
        embed = discord.Embed(
            title=f"顧客視点レビュー結果 [{title_suffix}]",
            description=f"**平均スコア: {avg} / 10.0**\n(Iteration: {iteration})",
            color=color
        )
        
        embed.add_field(name="要件充足度", value=f"{scores.get('requirements_coverage', 0)}", inline=True)
        embed.add_field(name="品質・完成度", value=f"{scores.get('quality_completeness', 0)}", inline=True)
        embed.add_field(name="明確さ・伝達力", value=f"{scores.get('clarity_communication', 0)}", inline=True)
        embed.add_field(name="独自価値", value=f"{scores.get('unique_value', 0)}", inline=True)
        embed.add_field(name="リスクの低さ", value=f"{scores.get('low_risk', 0)}", inline=True)
        
        embed.add_field(name="📝 クライアントフィードバック", value=eval_result.get('feedback', 'なし'), inline=False)
        embed.add_field(name="🚨 最優先改善点", value=eval_result.get('priority_improvement', 'なし'), inline=False)
        
        return embed

    @discord.ui.button(label="🔄 改善ループ開始 (最大3回)", style=discord.ButtonStyle.primary, custom_id="btn_revise_loop")
    async def btn_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.press_count >= self.MAX_PRESSES:
            await interaction.response.send_message(
                f"⚠️ 改善ループの上限({self.MAX_PRESSES}回)に達しています。", ephemeral=True)
            return
        self.press_count += 1
        await interaction.response.send_message("🔄 自律的改善ループを開始します... (少々お待ちください)", ephemeral=True)
        
        iteration = 0
        max_loops = 3
        
        # ループ処理（UIをブロックしないように進めるが、今回はここでawaitする）
        # 実際にはタスクとして投げて途中の進捗をEditするなど工夫可能
        #
        # 改訂ループは Gemini 最大6回で数分かかる。timeout は「最後の操作から」の
        # idle 秒数で、押下時に一度リセットされるだけなので、実行が長引くと処理中に
        # on_timeout が発火して「終わった瞬間にボタンが無効」という表示になる。
        # timeout setter は実行中のタイマーの期限も更新できる (discord.py 2.x) ので、
        # 作業中だけ延長し、finally で通常の idle 期限に戻す。
        # なおループが長引くと「押下から15分」の interaction token を超えることが
        # あり、その場合 on_timeout の edit は失敗する (ログのみ)。ボタンの無効化
        # 自体は View 内で完結するので効く。
        self.timeout = VIEW_TIMEOUT_SECONDS + LOOP_GRACE_SECONDS
        try:
            for i in range(max_loops):
                iteration += 1
                # 1. 改訂
                self.current_text = revise_deliverable(self.current_text, self.job_description, self.persona_id, self.current_eval)
                # 2. 再評価
                self.current_eval = evaluate_deliverable(self.current_text, self.job_description, self.persona_id)

                status = self.current_eval.get("status", "fail")
                if status == "pass":
                    break
        except GeminiBudgetExceeded as e:
            # ループ途中の予算超過: 黙って死ぬとユーザーには「開始します...」の
            # まま何も起きないため、文言を返して終了する (押下分は消費済み扱い)
            self.message = await interaction.followup.send(content=str(e), view=self)
            return
        finally:
            self.timeout = VIEW_TIMEOUT_SECONDS

        # ログ保存
        _save_improvement_log(
            self.job_description, self.persona_id,
            self.initial_text, self.current_text,
            self.initial_eval, self.current_eval
        )
        
        # UI更新
        status = self.current_eval.get("status", "fail")
        exhausted = self.press_count >= self.MAX_PRESSES
        self.btn_loop.disabled = (status == "pass" or exhausted)
        if exhausted and status != "pass":
            self.btn_loop.label = f"⛔ 改善上限({self.MAX_PRESSES}回)到達"
        embed = self._create_embed(self.current_eval, iteration)
        
        # 合格した場合のみ Obsidian に出力
        file_path_msg = ""
        if status == "pass":
            report_dir = os.path.join(VAULT_ROOT, "40_Report")
            os.makedirs(report_dir, exist_ok=True)
            import datetime
            filename = f"ClientReview_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            filepath = os.path.join(report_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(self.current_text)
            file_path_msg = f"\n✅ 合格基準に達したため、完成版を保存しました: `40_Report/{filename}`"
        elif exhausted:
            file_path_msg = f"\n⛔ {max_loops}回改訂しましたが基準（8.0）に達しませんでした。改善ループの押下上限({self.MAX_PRESSES}回)に達したため、これ以上の自動改訂はできません。"
        else:
            file_path_msg = f"\n⚠️ {max_loops}回改訂しましたが基準（8.0）に達しませんでした。再度ボタンを押すと続きから改訂します。"
            
        # View は新しいメッセージへ載せ替わる (discord.py が message_id を張り替える)
        # ため、on_timeout が編集すべき対象も最新のメッセージに更新する。
        self.message = await interaction.followup.send(content=file_path_msg, embed=embed, view=self)

    @discord.ui.button(label="🔬 システム自己改善分析", style=discord.ButtonStyle.secondary, custom_id="btn_analyze_weakness")
    async def btn_analyze(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔬 過去の改訂履歴からシステムの弱点と改善パッチを分析しています...", ephemeral=True)
        # 時間がかかる可能性があるので別スレッドや非同期にする想定だが簡易的に実行
        result = await asyncio.to_thread(analyze_weak_tools)
        await interaction.followup.send(content=f"**分析結果**\n{result}", ephemeral=True)


async def cmd_client_review(interaction: discord.Interaction, deliverable: str, job: str, persona: str = "other"):
    """成果物を指定ペルソナ（顧客視点）で5軸評価する。"""
    await interaction.response.defer(thinking=True)
    
    # 初回評価
    eval_result = await asyncio.to_thread(evaluate_deliverable, deliverable, job, persona)
    
    if "error" in eval_result:
        await interaction.followup.send(f"❌ 評価中にエラーが発生しました: {eval_result['error']}")
        return
        
    view = ClientReviewLoopView(deliverable, job, persona, eval_result)
    embed = view._create_embed(eval_result, iteration=0)

    # followup.send() は application webhook なので必ず WebhookMessage を返す
    # (discord.py が wait=True を強制)。on_timeout の編集対象として保持する。
    view.message = await interaction.followup.send(embed=embed, view=view)


async def cmd_persona_add(interaction: discord.Interaction, client_id: str, label: str, industry: str, extra_rules: str = "", manual_path: str = ""):
    """新しいクライアントペルソナを登録する。"""
    await interaction.response.defer(thinking=True)
    result = await asyncio.to_thread(add_client_persona, client_id, label, industry, extra_rules, manual_path)
    await interaction.followup.send(result)


async def cmd_persona_list(interaction: discord.Interaction):
    """登録済みのペルソナ一覧を表示する。"""
    data = await asyncio.to_thread(get_all_personas)
    industries = data.get("industries", {})
    clients = data.get("clients", {})
    
    msg = "**🏢 業種ペルソナ (Base)**\n"
    for k, v in industries.items():
        msg += f"- `{k}`: {v.get('label')} (Focus: {v.get('focus')})\n"
        
    msg += "\n**👤 クライアントペルソナ (Custom)**\n"
    if not clients:
        msg += "（登録なし）\n"
    for k, v in clients.items():
        msg += f"- `{k}`: {v.get('label')} (Base: {v.get('industry_parent')})\n"
        
    await interaction.response.send_message(msg, ephemeral=True)
