# -*- coding: utf-8 -*-
"""
Jev (TypeSafe AI System One model) クライアント

Jev は文章を生成せず、`state` (状況を表す文字列/JSON) と `question` (Choice/Score/Noul の
いずれか一つ) を渡すと、確率付きの型付き判断だけを返す "判断特化AI" (2026-09-15 早期提供開始)。

3つの判断プリミティブ (typesafe-ai/skills SKILL.md より):
  - Choice: 定義済みの選択肢から1つを選ぶ (例: fit = high/medium/low/not_applicable)
  - Score : 説明された尺度上の度合い (確率加重された順序尺度の位置)
  - Noul  : ある条件が真である確率 (0.0〜1.0。boolean ではなく確率値であることに注意 —
            第三者記事の一部は "boolean" と説明しているが、一次情報である公式SKILL.mdは
            明確に「確率」と定義している。閾値判定が要るなら呼び出し側で行う)

既知の制約 (重要):
  このリポジトリの開発・検証環境 (Claude Code on the web のサンドボックス) からは
  docs.typesafe.ai への到達がネットワークポリシーでブロックされており、正式な
  HTTP API 仕様 (エンドポイントパス・リクエスト/レスポンスの正確なフィールド名) を
  実地検証できていない。そのため _jev_request() の配線は SKILL.md の概念的な説明
  (state + instructions + criteria) からの最善の推測であり、**実機で
  ARMS_LIVE_TESTS=1 を使って実APIと疎通確認するまでは未検証**。ズレていても
  直す箇所が _jev_request() の1関数に閉じるように設計してある。

Usage (コードから):
  from tools.tool_jev import jev_choice, jev_score, jev_noul, JevAPIError
  try:
      result = jev_noul(state={"results": [...]}, instructions="ユーザーの目標は達成されたか")
      probability = result["probability"]
  except JevAPIError as e:
      ...  # フォールバック
"""
import os
import sys

# プロジェクトルートを sys.path に追加（tools/ 直接実行時対応）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

from lib.paths import ENV_PATH
from lib import cost_guard

load_dotenv(ENV_PATH)

import requests

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

JEV_API_KEY = os.getenv("JEV_API_KEY", "").strip()
# 既定は空文字。推測したURLへ黙って投げて分かりにくい失敗をするより、
# 未設定時は JevConfigError で明示的に落とす方が安全 (lib/paths.py の
# VAULT_ROOT/BITNET_DIR と違い、Jev のベースURLは「.env未設定なら動作する
# 妥当な既定値」が無い外部サービスの入口そのものなので、フォールバック値は置かない)。
JEV_API_BASE_URL = os.getenv("JEV_API_BASE_URL", "").strip()

HTTP_TIMEOUT = 10


class JevAPIError(Exception):
    """Jev API 呼び出しの失敗 (設定不備・ネットワークエラー・不正なレスポンス)。"""
    pass


class JevConfigError(JevAPIError):
    """JEV_API_KEY / JEV_API_BASE_URL が未設定。"""
    pass


# ---------------------------------------------------------
# 内部: 実際の HTTP 配線 (実APIとの差異が出ても直すのはここだけ)
# ---------------------------------------------------------

def _jev_request(question_type: str, state, instructions: str,
                  criteria=None, timeout: int = HTTP_TIMEOUT) -> dict:
    """Jev API へ1問の判断を問い合わせる。

    TODO: エンドポイントパス・リクエスト/レスポンスのフィールド名は
    docs.typesafe.ai/api.md (このサンドボックスからは到達不可) で要検証。
    現在の配線は SKILL.md の概念説明 (state + instructions + criteria、
    question id はコード側専用でモデルには送られない) からの最善の推測。
    """
    if not JEV_API_BASE_URL or not JEV_API_KEY:
        raise JevConfigError(
            "JEV_API_KEY / JEV_API_BASE_URL が未設定です。.env に設定してください "
            "(ドキュメント: https://docs.typesafe.ai/api.md)"
        )

    question = {"id": "q", "type": question_type, "instructions": instructions}
    if criteria is not None:
        question["criteria"] = criteria

    payload = {"state": state, "questions": [question]}
    headers = {
        "Authorization": f"Bearer {JEV_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            f"{JEV_API_BASE_URL.rstrip('/')}/v1/judgments",
            json=payload, headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise JevAPIError(f"Jev API 呼び出しに失敗しました: {e}") from e
    except ValueError as e:  # JSON デコード失敗
        raise JevAPIError(f"Jev API のレスポンスがJSONとして解釈できません: {e}") from e

    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict) or "q" not in answers:
        raise JevAPIError(f"Jev API のレスポンス形式が想定と異なります: {str(data)[:300]}")
    return answers["q"]


def _call(question_type: str, state, instructions: str, criteria=None) -> dict:
    """コストガード(JEV_COUNTER)を通してから _jev_request を呼ぶ共通経路。"""
    cost_guard.JEV_COUNTER.check_and_increment()
    return _jev_request(question_type, state, instructions, criteria=criteria)


# ---------------------------------------------------------
# 公開関数: 3つの判断プリミティブ
# ---------------------------------------------------------

def jev_choice(state, instructions: str, criteria: list) -> dict:
    """定義済みの選択肢から1つを選ばせる。戻り値: {"choice": str, "probability": float}。"""
    answer = _call("choice", state, instructions, criteria=list(criteria))
    return {
        "choice": answer.get("choice", ""),
        "probability": float(answer.get("probability", 0.0)),
    }


def jev_score(state, instructions: str, criteria: list = None) -> dict:
    """説明された尺度上の度合いを判断させる。戻り値: {"score": float, "confidence": float}。

    criteria は尺度の水準説明 (省略可、Jev の既定尺度に委ねる場合は None のまま)。
    """
    answer = _call("score", state, instructions, criteria=criteria)
    return {
        "score": float(answer.get("score", 0.0)),
        "confidence": float(answer.get("confidence", 0.0)),
    }


def jev_noul(state, instructions: str) -> dict:
    """ある条件が真である確率を判断させる。戻り値: {"probability": float} (0.0〜1.0)。

    Noul は boolean ではなく確率 (SKILL.md: "Probability of yes; no separate
    confidence")。閾値判定 (例: >=0.5 なら真) は呼び出し側の責務。
    """
    answer = _call("noul", state, instructions)
    return {"probability": float(answer.get("probability", 0.0))}
