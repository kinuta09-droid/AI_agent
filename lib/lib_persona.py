# -*- coding: utf-8 -*-
"""
Persona Management System
業種別・クライアント別のペルソナ（評価ルールや文体指定）を管理する。
設定は Obsidian の 00_System ディレクトリ内の personas.json に保存される。
"""

import os
import json
from lib.lib_shared import VAULT_ROOT, client, MODEL_NAME

PERSONA_DIR = os.path.join(VAULT_ROOT, "00_System")
PERSONA_FILE = os.path.join(PERSONA_DIR, "personas.json")

# 初回自動生成されるデフォルトの業種ペルソナ
DEFAULT_INDUSTRIES = {
    "writing": {"label": "ライティング", "focus": "文体・正確さ・読みやすさ", "rules": ["簡潔で論理的な文章を心がける", "ターゲット読者に合わせたトーンにする"]},
    "design": {"label": "デザイン", "focus": "視覚インパクト・ブランド整合", "rules": ["デザイン原則（近接、整列、反復、コントラスト）を守る"]},
    "coding": {"label": "プログラミング", "focus": "動作・可読性・セキュリティ", "rules": ["クリーンコードを徹底する", "エッジケースを考慮する", "適切なコメントを残す"]},
    "data": {"label": "データ入力/加工", "focus": "精度・フォーマット準拠", "rules": ["指定されたフォーマットを厳守する", "欠損値・異常値の処理を明確にする"]},
    "seo": {"label": "SEOライティング", "focus": "KW配置・見出し・E-E-A-T", "rules": ["検索意図を満たす", "見出し構造を論理的にする", "E-E-A-T（経験、専門性、権威性、信頼性）を意識する"]},
    "translation": {"label": "翻訳", "focus": "忠実度・自然さ・専門用語", "rules": ["直訳を避け、自然な日本語にする", "コンテキストに合った単語を選ぶ"]},
    "video": {"label": "動画・編集", "focus": "テンポ・字幕・視聴維持率", "rules": ["テンポ良く編集する", "視覚的・聴覚的な飽きを防ぐ工夫をする"]},
    "other": {"label": "その他", "focus": "汎用（デフォルト）", "rules": ["依頼者の意図を正確に汲み取る", "最高品質のアウトプットを出す"]}
}

def _init_personas():
    """初期化：ファイルがなければデフォルトを生成"""
    os.makedirs(PERSONA_DIR, exist_ok=True)
    if not os.path.exists(PERSONA_FILE):
        data = {
            "industries": DEFAULT_INDUSTRIES,
            "clients": {}
        }
        with open(PERSONA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return data
    else:
        try:
            with open(PERSONA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {"industries": DEFAULT_INDUSTRIES, "clients": {}}

def get_all_personas():
    """全ペルソナデータを取得"""
    return _init_personas()

def get_persona(persona_id: str):
    """ID（業種またはクライアント）からペルソナ情報を取得"""
    data = _init_personas()
    # 先にクライアントを探す
    if persona_id in data.get("clients", {}):
        return data["clients"][persona_id]
    # なければ業種を探す
    if persona_id in data.get("industries", {}):
        return data["industries"][persona_id]
    
    # 存在しない場合はデフォルトを返す
    return data["industries"]["other"]

def add_client_persona(client_id: str, label: str, industry: str, extra_rules: str, manual_path: str = "") -> str:
    """
    新しいクライアントペルソナを登録する。
    マニュアル(TXT/PDF等)がある場合は、Geminiで解析してルールを抽出する。
    """
    data = _init_personas()
    
    if industry not in data["industries"]:
        industry = "other"
        
    extracted_rules = []
    
    # マニュアルファイルの解析（今回はモックとしてテキスト抽出までを想定、実装は拡張可能）
    if manual_path and os.path.exists(manual_path):
        try:
            with open(manual_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()[:10000] # 上限を設けて読み込む
                
            prompt = f"""
            以下のクライアント作成のマニュアル・指示書から、AIが成果物を評価・作成する際に守るべき「抽出ルール」を箇条書きで5〜10個抽出してください。
            【マニュアル内容】
            {content}
            """
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            # 抽出されたルールをリスト化
            rules = [line.strip("- *").strip() for line in response.text.split("\n") if line.strip().startswith(("-", "*"))]
            extracted_rules.extend(rules)
        except Exception as e:
            return f"Error reading manual file: {e}"
            
    # 追加ルールのパース
    if extra_rules:
        # セミコロンまたは改行区切り
        split_rules = [r.strip() for r in extra_rules.replace(";", "\n").split("\n") if r.strip()]
        extracted_rules.extend(split_rules)
        
    client_data = {
        "label": label,
        "industry_parent": industry,
        "custom_rules": extracted_rules
    }
    
    data["clients"][client_id] = client_data
    
    try:
        with open(PERSONA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return f"✅ クライアントペルソナ '{client_id} ({label})' を登録しました。抽出ルール: {len(extracted_rules)}件"
    except Exception as e:
        return f"Error saving persona: {e}"

def build_persona_prompt(persona_id: str) -> str:
    """評価AIに渡すためのペルソナプロンプト文字列を構築する"""
    persona = get_persona(persona_id)
    
    prompt = f"あなたはプロの「{persona.get('label', '発注者')}」として、納品物の品質を厳格に評価します。\n"
    
    if "industry_parent" in persona:
        # クライアントペルソナの場合、親の業種特性も引き継ぐ
        parent = get_persona(persona["industry_parent"])
        prompt += f"【業種の評価重点】\n{parent.get('focus', '')}\n"
        prompt += f"【業種の基本ルール】\n"
        for rule in parent.get("rules", []):
            prompt += f"- {rule}\n"
            
        prompt += f"\n【クライアント固有の絶対ルール】\n"
        for rule in persona.get("custom_rules", []):
            prompt += f"- {rule}\n"
    else:
        # 業種ペルソナの場合
        prompt += f"【評価の重点】\n{persona.get('focus', '')}\n"
        prompt += f"【基本ルール】\n"
        for rule in persona.get("rules", []):
            prompt += f"- {rule}\n"
            
    return prompt
