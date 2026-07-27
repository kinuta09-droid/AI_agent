# -*- coding: utf-8 -*-
"""
Client Review System
成果物を5つの顧客視点軸で自動評価し、基準点（平均8.0）を満たさない場合は
フィードバックに基づき自律的に改訂（Revision）を行うエンジン。
"""

import os
import json
import re
from lib.lib_shared import client, MODEL_NAME, safe_json_loads, WORK_DIR, load_json
from lib.lib_persona import build_persona_prompt
from tools.tool_obsidian import tool_read_note
from lib.bot_errors import is_failure_result

IMPROVEMENT_LOG_PATH = os.path.join(WORK_DIR, "improvement_log.json")

def _resolve_deliverable_text(deliverable: str) -> str:
    """入力がObsidianのリンクであれば内容を読み込み、そうでなければそのまま返す"""
    # [[Note Name]] の形式かチェック
    m = re.match(r'^\[\[(.*?)\]\]$', deliverable.strip())
    if m:
        note_name = m.group(1)
        # 拡張子補完
        if not note_name.endswith(".md"):
            note_name += ".md"
        content = tool_read_note(note_name)
        # tool_read_note は "Error: File ... not found." のほかに
        # "Error reading note: {e}" も返す。後者を拾い損ねると、エラー文が
        # そのまま「レビュー対象の成果物」として LLM に渡る。
        if is_failure_result(content):
            # 読み込めない場合は諦めてそのまま返すかエラーにする
            return f"[Error: Obsidian note '{note_name}' could not be read]\n"
        return content
    return deliverable

def evaluate_deliverable(deliverable: str, job_description: str, persona_id: str = "other") -> dict:
    """
    成果物をペルソナ指定で5軸評価する。
    戻り値は以下を含む辞書:
    - scores: 各軸の点数と平均
    - feedback: 定性的なコメント
    - status: pass, revision_needed, fail
    """
    text = _resolve_deliverable_text(deliverable)
    
    persona_prompt = build_persona_prompt(persona_id)
    
    prompt = f"""
{persona_prompt}

以下の成果物が、依頼された仕事の要件を満たし、プロフェッショナルな品質に達しているかを厳格に審査してください。

【依頼内容（Job Description）】
{job_description}

【成果物（Deliverable）】
{text}

以下の5つの軸で、それぞれ 1.0 〜 10.0 の間で採点し、JSON形式で出力してください。
1. requirements_coverage (要件充足度): 依頼内容が全て網羅されているか。
2. quality_completeness (品質・完成度): 正確さ、深さ、全体としての完成度。
3. clarity_communication (明確さ・伝達力): 分かりやすさ、構成、誤解のなさ。
4. unique_value (独自価値): 一般的な回答を超えたプロとしての工夫や付加価値があるか。
5. low_risk (リスクの低さ): そのままクライアントに提出して手戻りやクレームが生じないか。

【出力JSON形式】
{{
    "scores": {{
        "requirements_coverage": 8.5,
        "quality_completeness": 7.0,
        "clarity_communication": 9.0,
        "unique_value": 6.5,
        "low_risk": 8.0
    }},
    "feedback": "全体の構成は良いですが、品質と独自価値の面で少し物足りません。特に〇〇の観点が不足しています。",
    "priority_improvement": "もっとも優先して直すべき具体的なポイント1点"
}}

※ お世辞は不要です。少しでも不十分な点があれば 7.0 以下をつけてください。完璧なプロの仕事にのみ 9.0 以上を与えてください。
"""
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        result = safe_json_loads(response.text)
        
        scores = result.get("scores", {})
        # 不足しているキーがあれば埋める
        for key in ["requirements_coverage", "quality_completeness", "clarity_communication", "unique_value", "low_risk"]:
            if key not in scores:
                scores[key] = 5.0
                
        # 平均計算
        avg = sum(scores.values()) / 5.0
        result["scores"]["average"] = round(avg, 2)
        
        # 判定
        if avg >= 8.0:
            result["status"] = "pass"
        elif avg >= 5.0:
            result["status"] = "revision_needed"
        else:
            result["status"] = "fail"
            
        return result
    except Exception as e:
        return {"error": str(e), "status": "fail"}

def revise_deliverable(deliverable: str, job_description: str, persona_id: str, evaluation_result: dict) -> str:
    """
    評価フィードバックを元に成果物を改訂（修正）する。
    """
    text = _resolve_deliverable_text(deliverable)
    persona_prompt = build_persona_prompt(persona_id)
    
    prompt = f"""
あなたはプロフェッショナルなクリエイターです。
クライアント（レビュアー）から以下のフィードバックを受け取りました。指摘事項を真摯に受け止め、成果物を完全に改訂してください。

{persona_prompt}

【当初の依頼内容（Job Description）】
{job_description}

【現在の成果物（Deliverable）】
{text}

【クライアントの評価・フィードバック】
・平均スコア: {evaluation_result.get('scores', {}).get('average', 0)} / 10.0
・フィードバック: {evaluation_result.get('feedback', '')}
・最優先改善点: {evaluation_result.get('priority_improvement', '')}

【指示】
フィードバックにある問題点を全て修正し、評価が 9.0 以上になるように改訂された「完成版の成果物のみ」を出力してください。
挨拶や「修正しました」といった前置き・後書きは一切不要です。成果物の本文のみを出力してください。
"""
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        revised_text = response.text.strip()
        return revised_text
    except Exception as e:
        return f"Error during revision: {e}"

def run_client_review_loop(deliverable: str, job_description: str, persona_id: str = "other", max_loops: int = 3) -> dict:
    """
    自律的な改訂ループを実行するバックエンド関数。
    ※Discord側からは非同期で段階的に呼ぶ設計のため、これはUIを介さないエージェント自律時等に使用される想定。
    Discordからは状態を維持して evaluate -> revise を個別に呼ぶ設計とする。
    """
    current_text = deliverable
    logs = []
    
    for i in range(max_loops):
        print(f"[ClientReview] Loop {i+1} starting...")
        eval_result = evaluate_deliverable(current_text, job_description, persona_id)
        
        log_entry = {
            "loop": i+1,
            "text": current_text,
            "evaluation": eval_result
        }
        logs.append(log_entry)
        
        status = eval_result.get("status", "fail")
        if status == "pass":
            print("[ClientReview] Passed!")
            break
            
        if i < max_loops - 1:
            print("[ClientReview] Revising based on feedback...")
            current_text = revise_deliverable(current_text, job_description, persona_id, eval_result)
            
    # 学習用にログを保存（最初と最後のスコア差分など）
    if len(logs) > 1:
        _save_improvement_log(
            job_description, persona_id,
            logs[0]["text"], logs[-1]["text"],
            logs[0]["evaluation"], logs[-1]["evaluation"]
        )
        
    return {
        "final_text": current_text,
        "logs": logs,
        "final_status": logs[-1]["evaluation"].get("status") if logs else "fail"
    }

def _save_improvement_log(job: str, persona: str, before_text: str, after_text: str, before_eval: dict, after_eval: dict):
    """改訂のビフォーアフターを保存し、今後のシステム改善学習パッチに活かす"""
    try:
        os.makedirs(os.path.dirname(IMPROVEMENT_LOG_PATH), exist_ok=True)
        history = load_json(IMPROVEMENT_LOG_PATH, default=[])

        entry = {
            "timestamp": __import__("datetime").datetime.now().isoformat(),
            "job": job,
            "persona": persona,
            "before_score": before_eval.get("scores", {}).get("average", 0),
            "after_score": after_eval.get("scores", {}).get("average", 0),
            "feedback_applied": before_eval.get("feedback", ""),
            "before_snippet": before_text[:200] + "...",
            "after_snippet": after_text[:200] + "...",
            "full_before": before_text,
            "full_after": after_text
        }
        history.append(entry)
        
        with open(IMPROVEMENT_LOG_PATH, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ClientReview] Failed to save improvement log: {e}")
