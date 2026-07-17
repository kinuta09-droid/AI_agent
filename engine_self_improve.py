# -*- coding: utf-8 -*-
"""
Self Improvement Engine
改訂ループの結果（improvement_log.json）を教師データとし、システム全体の
品質を向上させるための「学習パッチ（system_patches.json）」を自動生成・管理する。
"""

import os
import json
import uuid
import datetime
from lib.lib_shared import client, MODEL_NAME, safe_json_loads, WORK_DIR, load_json, save_json

IMPROVEMENT_LOG_PATH = os.path.join(WORK_DIR, "improvement_log.json")
SYSTEM_PATCHES_PATH = os.path.join(WORK_DIR, "system_patches.json")

def analyze_weak_tools() -> str:
    """
    improvement_log.json の最新データから、点数の大幅な上昇・低下の事例を抽出し、
    Gemini で要因を分析して汎用的な学習パッチを生成する。
    """
    logs = load_json(IMPROVEMENT_LOG_PATH, [])
    if not logs:
        return "データ不足: improvement_log.json に学習データがありません。"

    # 直近の20件を学習対象とする
    recent_logs = logs[-20:]
    
    # 差分が大きい（+1.0以上、または -0.5以下など）サンプルに絞る
    training_samples = []
    for log in recent_logs:
        diff = log.get("after_score", 0) - log.get("before_score", 0)
        if diff >= 1.0 or diff < 0:
            training_samples.append({
                "job": log.get("job"),
                "persona": log.get("persona"),
                "before_score": log.get("before_score"),
                "after_score": log.get("after_score"),
                "diff": round(diff, 2),
                "feedback": log.get("feedback_applied", ""),
                "before_snippet": log.get("before_snippet"),
                "after_snippet": log.get("after_snippet")
            })

    if not training_samples:
        return "データ不足: 有効なスコア変動サンプルが見つかりませんでした。"

    prompt = f"""
あなたはAIエージェントの「自己進化調整モジュール」です。
以下の改訂履歴（教師データ）を分析し、エージェントが**最初から高い点数（8.0以上）を出せるようになるための汎用的なルール・思考パッチ**を1つ生成してください。

【改訂履歴（ポジティブな改善とネガティブな失敗のサンプル）】
{json.dumps(training_samples, ensure_ascii=False, indent=2)}

【指示】
1. この履歴から、現在のAIが「どんな指示を見落としがちか」「どう修正すれば点数が上がるか」のパターンを見つけてください。
2. 他のタスクにも適用可能な「システムプロンプトに追加すべき具体的な指示（ルール）」を100文字程度で生成してください。
3. 以下のJSON形式で出力してください。

{{
    "analysis_summary": "全体傾向の分析、なぜ点数が上がったかの考察（2〜3文）",
    "patch_hint": "以降の出力全てに適用すべき強力で具体的なルール指示（1文〜2文）",
    "target_persona": "この弱点が目立つペルソナ（特定の業種、または ALL）"
}}
"""

    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        result = safe_json_loads(response.text)
        
        patch_hint = result.get("patch_hint")
        if not patch_hint:
            return "パッチの生成に失敗しました（空のデータ）。"
            
        # system_patches.json に追記（未承認状態）
        patches = load_json(SYSTEM_PATCHES_PATH, [])
        new_patch = {
            "id": f"patch-{uuid.uuid4().hex[:8]}",
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "approved": False,
            "patch_hint": patch_hint,
            "analysis_summary": result.get("analysis_summary", ""),
            "target_persona": result.get("target_persona", "ALL")
        }
        patches.append(new_patch)
        # 原子的保存 (tmp+os.replace): 書込中クラッシュでの破損防止 — learning_db と同方式
        save_json(SYSTEM_PATCHES_PATH, patches)
        
        return f"✅ 新しい改善パッチを生成し、承認待ち(approved: false)として追加しました。\n- 分析: {new_patch['analysis_summary']}\n- パッチ: {new_patch['patch_hint']}"
    except Exception as e:
        return f"パッチ生成中にエラーが発生しました: {e}"

def get_approved_patch_hints() -> str:
    """
    system_patches.json から承認済み（approved: true）のパッチをすべて連結して返す。
    engine_plan_execute のメインプロンプトに注入するための関数。
    """
    patches = load_json(SYSTEM_PATCHES_PATH, [])
    approved = [p["patch_hint"] for p in patches if p.get("approved") is True]
    
    if not approved:
        return ""
        
    return "【システム自己改善からの教訓（厳守）】\n- " + "\n- ".join(approved)
