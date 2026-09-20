# -*- coding: utf-8 -*-
"""
体験談記事化スクリプト (Generate Experiential Article)

ユーザーの下書きから要点と質問をAIが生成し、それに回答する形で
対話的にエピソードや感情を引き出します。
最終的に、その「回答の文体・言葉遣い」を模倣し、血の通った体験談記事へリライトします。
"""

import os
import sys
import json
import datetime
import traceback
from pathlib import Path

# ルートディレクトリをsys.pathに追加してlibモジュールをインポート可能にする
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from dotenv import load_dotenv

# 施策③ Phase 4a: 独自 genai.Client + 独自 env MODEL_NAME(既定3.1-preview) を廃止し、
# 共有 client / MODEL_NAME(LIGHT・.envで世代交代可) 経由に統一。
# 効果: Tier2-b 日次コストガード網羅 + モデル切替を .env 1行で追従（直書き排除）。
from lib.lib_shared import client, MODEL_NAME

load_dotenv(os.path.join(root_dir, '.env'))
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GOOGLE_API_KEY:
    print("Error: GOOGLE_API_KEY not found in .env")
    sys.exit(1)

def extract_points_and_questions(draft_text: str) -> list:
    """下書きから深掘りすべき要点とユーザーへの質問を生成する"""
    prompt = f"""
あなたはプロの編集者であり、インタビュアーです。
以下の「ユーザーが書いた記事の下書き・メモ」を読み、読者の共感を呼ぶ「体験談記事」に昇華させるために深掘りすべき重要なポイントを3〜5つ抽出してください。

各ポイントに対して、ユーザーから「生々しい体験、具体的なエピソード、その時どう感じたか（感情）」を引き出すための質問を作成してください。

[下書き・メモ]
{draft_text}

[出力要件]
以下の構造のJSON配列（リスト）のみを出力してください。Markdownバッククォート(```json ... ```)は付けないでください。
[
  {{
    "point": "抽出した要点の短い見出し（例: 技術の導入背景）",
    "question": "ユーザーへの質問（例: 導入しようと決断したとき、どんな焦りや期待がありましたか？）"
  }},
  ...
]
"""
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        # JSON部分だけを抽出する安全な処理
        text = response.text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
            
        return json.loads(text.strip())
    except Exception as e:
        print(f"\n[エラー] 要点の抽出に失敗しました: {e}")
        return []

_PERSONAL_INFO_PROMPT_RULE = (
    "[参考資料]に個人を特定できる情報（住所・電話番号・メールアドレス・家族/友人の実名・"
    "勤務先の詳細・SNSアカウント等）が含まれていても、記事に転記しないこと。"
    "ただし本人がインタビューで語った体験談自体はこの制約の対象外で、主題として自由に使ってよい。"
)


def _build_reference_material_block(reference_material: str) -> str:
    if not reference_material:
        return ""
    return (
        f"\n[参考資料]\n{reference_material}\n\n"
        "[参考資料の扱いに関する絶対条件]\n"
        "- 参考資料に書かれた事実・数値・エピソードのみを情報源として使ってよく、"
        "資料にない体験や数値を本人の体験であるかのように創作しないこと。\n"
        "- 資料から採った記述には文末に出典 `[[ファイル名]]` を付けること。"
        "本人のQ&A回答由来の体験談部分には出典を付けない。\n"
        f"- {_PERSONAL_INFO_PROMPT_RULE}\n"
    )


def _build_audience_block(audience: str) -> str:
    if not audience:
        return ""
    return f"\n[想定読者]\n{audience}\n"


def generate_final_article(draft_text: str, qa_pairs: list, reference_material: str = "", audience: str = "") -> str:
    """下書きとQ&Aの回答（文体）を元に最終的な記事を生成する。

    reference_material（Vault検索で採用した参考資料、既定は空文字＝従来通りの下書きのみ）を
    渡すと、出典明示・個人情報転記禁止の制約付きで記事にその内容を織り込む。
    audience（想定読者、既定は空文字）を渡すと、その読者に向けた説明の粒度・専門用語の
    かみ砕き方を調整する。
    """

    qa_text = ""
    for idx, qa in enumerate(qa_pairs, 1):
        qa_text += f"\n【ポイント{idx}: {qa['point']}】\n"
        qa_text += f"質問: {qa['question']}\n"
        qa_text += f"ユーザーの回答: {qa['answer']}\n"

    reference_block = _build_reference_material_block(reference_material)
    audience_block = _build_audience_block(audience)

    prompt = f"""
あなたはプロのライターですが、今回は「ゴーストライター」として完全にユーザー自身になりきって記事を執筆してください。

以下の[元の下書き]と、ユーザーが自身の言葉で語った[深掘りインタビューの回答]を統合し、一つの完成された「体験談記事」を作成してください。

[元の下書き]
{draft_text}

[深掘りインタビューの回答]
{qa_text}
{reference_block}
{audience_block}
[執筆の絶対条件]
1. 【文体の完全模倣】: インタビューの回答テキストに含まれる「ユーザー本人の言葉遣い、トーン、語尾（です・ます、だ・である等の判定）、個人的な癖」を分析し、記事全体の文体をそれに完全に一致させてください。AI特有の無機質なまとめ文章や、過剰に劇的な表現は避けてください。
2. 【体験の統合】: 回答で得られた具体的なエピソードや「その時どう感じたか」という感情の動きを、記事のメインコンテンツとして自然に織り交ぜてください。
3. 【構成】: 読者が惹きつけられるタイトル（# ﾀｲﾄﾙ）、導入、本文、まとめの構成をMarkdown形式で出力してください。
4. 【読者への配慮】: [想定読者]が指定されている場合、その読者が理解できる粒度で専門用語をかみ砕いてください。
"""
    try:
        print("\n⏳ 記事を執筆しています... (これには少し時間がかかります)")
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"\n[エラー] 記事の生成に失敗しました: {e}")
        return ""

def extract_missing_info_questions(goal: str, reference_material: str = "", audience: str = "") -> list:
    """目標と採用済み参考資料を踏まえ、記事完成に不足する体験・具体情報を質問として抽出する。

    extract_points_and_questions（下書きを深掘りする質問）とは目的が異なる
    （目標達成に足りない情報の洗い出し）が、出力形式は完全に揃えてあり、
    既存のQ&A送信・回答記録ロジックをそのまま流用できる。
    """
    reference_block = f"\n[採用した参考資料]\n{reference_material}\n" if reference_material else ""
    audience_block = _build_audience_block(audience)
    prompt = f"""
あなたはプロの編集者であり、インタビュアーです。
以下の[目標]の記事を完成させるために、まだ足りていない「本人にしか語れない体験・具体的なエピソード・感情」を
洗い出すための質問を3〜5個作成してください。
{reference_block}
{audience_block}
[目標]
{goal}

[出力要件]
以下の構造のJSON配列（リスト）のみを出力してください。Markdownバッククォート(```json ... ```)は付けないでください。
[
  {{
    "point": "抽出した要点の短い見出し（例: 技術の導入背景）",
    "question": "ユーザーへの質問（例: この目標に取り組もうと決断したとき、どんな焦りや期待がありましたか？）"
  }},
  ...
]
"""
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        text = response.text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]

        return json.loads(text.strip())
    except Exception as e:
        print(f"\n[エラー] 不足情報の質問抽出に失敗しました: {e}")
        return []


from tools.tool_client_review import evaluate_deliverable
from lib.lib_persona import build_persona_prompt

def refine_article(draft_article: str, draft_text: str, qa_pairs: list, evaluation_result: dict, persona_id: str = "writing", reference_material: str = "", audience: str = "") -> str:
    qa_text = ""
    for idx, qa in enumerate(qa_pairs, 1):
        qa_text += f"\n【ポイント{idx}: {qa['point']}】\n"
        qa_text += f"ユーザーの回答: {qa['answer']}\n"

    persona_prompt = build_persona_prompt(persona_id)
    # generate_final_article と同じ参考資料ブロック(本文・出典維持・創作禁止・個人情報禁止の
    # 全条件込み)を渡す。推敲時に資料本文そのものが見えないと、出典を保てているか・
    # 資料にない記述を混入させていないかをLLMが判定しようがない。
    reference_block = _build_reference_material_block(reference_material)
    audience_block = _build_audience_block(audience)

    prompt = f"""
あなたはプロフェッショナルな編集者であり、同時に優れたゴーストライターです。
以下の「初稿記事」に対して、厳しい品質チェック（5軸評価）のフィードバックが入りました。
これを元に記事を推敲（リライト）してください。

{persona_prompt}

【初稿記事】
{draft_article}

【プロ編集者からのフィードバック】
・平均スコア: {evaluation_result.get('scores', {}).get('average', 0)} / 10.0
・フィードバック: {evaluation_result.get('feedback', '')}
・最優先改善点: {evaluation_result.get('priority_improvement', '')}

【推敲の絶対条件】
1. **ユーザーの生々しい文体の完全維持**: ユーザーの生の言葉（以下の「Q&A回答」）の文脈、言葉遣い（です・ます等の語尾、一人称）、感情表現は**絶対に崩さない**でください。フィードバックを受けても「AI臭い美辞麗句（革新的な〜など）」は絶対に追加しないこと。
2. フィードバックで指摘された構成の甘さ、説得力の弱さ、読者のペインポイント解決に関する部分のみを、ユーザーのトーンに合わせた自然な形で補強してください。
3. Markdown形式で出力し、挨拶や「修正しました」などの前置き・後書きは一切不要です。完成した記事本文のみを出力してください。
4. 参考資料に基づく記述の出典 `[[ファイル名]]` を推敲後も維持し、新たに資料にない体験・数値を創作しないこと。
{reference_block}
{audience_block}
【ユーザー自身の生の言葉（Q&A回答） - 文体のベースとして必ず維持すること】
{qa_text}
"""
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"\n[エラー] 記事の推敲に失敗しました: {e}")
        return draft_article

def run_article_reflection_loop(draft_article: str, draft_text: str, qa_pairs: list, max_loops: int = 2, reference_material: str = "", audience: str = "") -> str:
    """初稿に対して自己評価と推敲のループを実行し、最終版を返す"""
    current_article = draft_article
    job_description = f"読者の共感を呼ぶ、ユーザーの等身大の体験に基づいた自分らしい体験談記事の作成。\n元の下書き: {draft_text[:500]}..."
    if reference_material:
        job_description += f"\n\n[審査基準への追加事項] {_PERSONAL_INFO_PROMPT_RULE}"
    if audience:
        job_description += f"\n[想定読者] {audience}"

    for i in range(max_loops):
        print(f"[Reflection] Loop {i+1} starting...")
        eval_result = evaluate_deliverable(current_article, job_description, persona_id="writing")

        status = eval_result.get("status", "fail")
        avg_score = eval_result.get("scores", {}).get("average", 0)
        print(f"[Reflection] Score: {avg_score} ({status})")
        print(f"[Reflection] Feedback: {eval_result.get('feedback', '')}")

        if status == "pass" or avg_score >= 8.5:
            print("[Reflection] Passed! Quality is sufficient.")
            break

        if i < max_loops - 1:
            print("[Reflection] Revising based on feedback...")
            current_article = refine_article(current_article, draft_text, qa_pairs, eval_result, persona_id="writing", reference_material=reference_material, audience=audience)

    return current_article


def apply_user_revision(article: str, revision_request: str, reference_material: str = "", audience: str = "") -> str:
    """完成記事へのユーザーの自由文による修正依頼を反映する単発修正。

    refine_article(AIの自己評価フィードバックに基づく全面推敲)とは異なり、ユーザーが
    直接指示した変更点だけを反映する。評価・推敲ループ(evaluate_deliverable/
    run_article_reflection_loop)は呼ばない — ユーザーの直接指示を汎用品質フィードバックで
    上書きしてしまうと、せっかく本人の言葉で確定した記事が意図せず書き換わるため。
    """
    reference_block = _build_reference_material_block(reference_material)
    audience_block = _build_audience_block(audience)

    prompt = f"""
あなたはプロの編集者です。以下の[完成記事]に対して、ユーザーから[修正依頼]が届きました。
指示された変更点だけを反映し、それ以外の文体・構成・出典表記は一切変更しないでください。

[完成記事]
{article}

[修正依頼]
{revision_request}
{reference_block}
{audience_block}
[修正の絶対条件]
1. [修正依頼]で指示された箇所のみを変更し、指示されていない部分の文章・語順・見出し構成は元のまま維持すること。
2. ユーザー本人の言葉遣い・文体は変更しないこと。
3. 資料由来の記述に付いている出典 `[[ファイル名]]` は、その記述を残す限り維持すること。
4. Markdown形式で出力し、挨拶や「修正しました」などの前置き・後書きは一切不要です。完成した記事本文のみを出力してください。
"""
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"\n[エラー] 記事の修正に失敗しました: {e}")
        return article


def main():
    print("="*60)
    print(" 体験談記事化 ジェネレーター (Generate Experiential Article) ")
    print("="*60)
    print("記事の下書きや箇条書きのメモを入力してください。")
    print("複数行入力できます。入力を終了する場合は、空行で Enter を2回押してください。")
    print("-" * 60)
    
    draft_lines = []
    empty_count = 0
    while True:
        try:
            line = input()
            if not line.strip():
                empty_count += 1
                if empty_count >= 2:
                    break
            else:
                empty_count = 0
            
            # 空行以外、または空行1つめは保持
            if empty_count < 2:
                 draft_lines.append(line)
        except EOFError:
            break
            
    draft_text = "\n".join(draft_lines).strip()
    
    if not draft_text:
        print("下書きが入力されていません。終了します。")
        return

    print("\n⏳ 下書きを分析し、深掘りするポイントを抽出しています...")
    points = extract_points_and_questions(draft_text)
    
    if not points:
        print("抽出に失敗したため処理を終了します。")
        return
        
    print(f"\n🎉 {len(points)}つの深掘りポイントが見つかりました！")
    print("それぞれの質問に、あなたの**「生の言葉・感情」**で答えてください。")
    print("（この言葉遣いやトーンがそのまま記事の文体に反映されます）\n")
    
    qa_pairs = []
    for idx, p in enumerate(points, 1):
        print(f"■ ポイント {idx}: {p.get('point', '未定義')}")
        print(f"Q. {p.get('question', '未定義')}")
        
        # 複数行の回答を受け付ける
        print("(回答を入力してください。終了する場合は空行で Enter を2回押してください)")
        answer_lines = []
        ans_empty = 0
        while True:
            try:
                line = input("> ")
                if not line.strip():
                    ans_empty += 1
                    if ans_empty >= 2:
                        break
                else:
                    ans_empty = 0
                
                if ans_empty < 2:
                    answer_lines.append(line)
            except EOFError:
                break
                
        answer = "\n".join(answer_lines).strip()
        if not answer:
            answer = "（特になし）"
            
        p["answer"] = answer
        qa_pairs.append(p)
        print("-" * 40)
        
    final_article = generate_final_article(draft_text, qa_pairs)
    
    if not final_article:
        return
        
    # 保存処理
    exports_dir = os.path.join(root_dir, "exports")
    os.makedirs(exports_dir, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"experiential_article_{timestamp}.md"
    filepath = os.path.join(exports_dir, filename)
    
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(final_article)
        
        print("\n" + "="*60)
        print(" ✨ 完成した体験談記事 ✨ ")
        print("="*60)
        print(final_article)
        print("="*60)
        print(f"📁 保存先: {filepath}")
    except Exception as e:
        print(f"\n[エラー] ファイルの保存に失敗しました: {e}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n処理を中断しました。")
