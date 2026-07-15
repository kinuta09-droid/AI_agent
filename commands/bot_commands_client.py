# -*- coding: utf-8 -*-
import discord
from discord import app_commands
import os
import asyncio
from lib.lib_persona import add_client_persona, get_all_personas
from tools.tool_client_review import evaluate_deliverable, revise_deliverable, _save_improvement_log
from engine_self_improve import analyze_weak_tools
from lib.lib_shared import WORK_DIR, VAULT_ROOT
from commands.bot_views import AllowedUserView

class ClientReviewLoopView(AllowedUserView):
    def __init__(self, deliverable: str, job_description: str, persona_id: str, current_eval: dict):
        super().__init__(timeout=None)
        self.current_text = deliverable
        self.job_description = job_description
        self.persona_id = persona_id
        self.current_eval = current_eval
        
        # 初期スコアと状態を保持（ログ用）
        self.initial_text = deliverable
        self.initial_eval = current_eval

        # 合格(pass)なら改善ボタンは無効化、不合格(revision_needed/fail)なら有効化
        status = current_eval.get("status", "fail")
        if status == "pass":
            self.btn_loop.disabled = True
            
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
        await interaction.response.send_message("🔄 自律的改善ループを開始します... (少々お待ちください)", ephemeral=True)
        
        iteration = 0
        max_loops = 3
        
        # ループ処理（UIをブロックしないように進めるが、今回はここでawaitする）
        # 実際にはタスクとして投げて途中の進捗をEditするなど工夫可能
        for i in range(max_loops):
            iteration += 1
            # 1. 改訂
            self.current_text = revise_deliverable(self.current_text, self.job_description, self.persona_id, self.current_eval)
            # 2. 再評価
            self.current_eval = evaluate_deliverable(self.current_text, self.job_description, self.persona_id)
            
            status = self.current_eval.get("status", "fail")
            if status == "pass":
                break
                
        # ログ保存
        _save_improvement_log(
            self.job_description, self.persona_id,
            self.initial_text, self.current_text,
            self.initial_eval, self.current_eval
        )
        
        # UI更新
        status = self.current_eval.get("status", "fail")
        self.btn_loop.disabled = (status == "pass")
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
        else:
            file_path_msg = f"\n⚠️ {max_loops}回改訂しましたが基準（8.0）に達しませんでした。再度ボタンを押すと続きから改訂します。"
            
        await interaction.followup.send(content=file_path_msg, embed=embed, view=self)

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
    
    await interaction.followup.send(embed=embed, view=view)


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
