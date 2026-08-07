# -*- coding: utf-8 -*-
"""
アイデア壁打ち対話生成 (Generate Idea Dialogue)

/brainstorm 用。雑なアイデアの種 (seed) から、直前までのやり取りを踏まえて
「次の1問だけ」を都度生成する適応型インタビュー。事前に質問リストを確定する
generate_experiential_article.py の extract_points_and_questions とは異なり、
毎ターン直前の回答を見てから1問だけ考える。
"""

import os
import sys
import json

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from lib.lib_shared import client, MODEL_NAME


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _format_turns(turns: list) -> str:
    lines = []
    for i, t in enumerate(turns, 1):
        lines.append(f"Q{i}: {t['question']}")
        lines.append(f"A{i}: {t['answer']}")
    return "\n".join(lines)


def generate_next_question(seed: str, turns: list) -> str:
    """直前までの対話を踏まえ、次の深掘り質問を1つだけ生成する。失敗時は空文字。"""
    history = _format_turns(turns) if turns else "(まだ質問していない)"
    prompt = f"""
あなたは優れた壁打ち相手です。ユーザーがまだ言葉になっていない雑なアイデアを話しています。
アイデアを一緒に育てるために、直前のやり取りを踏まえて**次の質問を1つだけ**投げかけてください。

[元のアイデア]
{seed}

[これまでのやり取り]
{history}

[出力要件]
- 質問文だけを出力すること（前置き・挨拶・見出し・箇条書き記号は不要）
- 直前の回答の中で曖昧な部分、具体化できそうな部分、次に考えるべき論点を突く
- 1〜2文の短い質問にすること
"""
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return response.text.strip()
    except Exception as e:
        print(f"\n[エラー] 次の質問の生成に失敗しました: {e}")
        return ""


def summarize_idea_session(seed: str, turns: list) -> dict:
    """対話全体を1枚のアイデアメモに要約する。失敗時は空dict。"""
    history = _format_turns(turns) if turns else "(やり取りなし)"
    prompt = f"""
あなたは優れた編集者です。以下は、あるアイデアについての壁打ち対話の記録です。
これを1枚のアイデアメモとしてまとめてください。

[元のアイデア]
{seed}

[壁打ちのやり取り]
{history}

[出力要件]
以下の構造のJSONオブジェクトのみを出力してください。Markdownバッククォートは付けないでください。
{{
  "title": "ノートのタイトル（20文字以内、ファイル名にも使う）",
  "summary": "このアイデアの要約（1〜3行）",
  "content": "Markdown形式の本文。背景/深掘りで出た要点/次のアクションか未解決の問い、の構成で書く"
}}
"""
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return json.loads(_strip_code_fence(response.text))
    except Exception as e:
        print(f"\n[エラー] アイデアの要約に失敗しました: {e}")
        return {}
