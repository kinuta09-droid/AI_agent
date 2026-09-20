# -*- coding: utf-8 -*-
"""
Jev (TypeSafe AI System One model) クライアント

Jev は文章を生成せず、`state` (状況を表す文字列/JSON) と `questions` (Choice/Score/Noul の
いずれか一つを、キー=質問ID・値=Question のマップとして渡す) を渡すと、確率付きの型付き
判断だけを返す "判断特化AI" (2026-09-15 早期提供開始)。API仕様はユーザーから共有された
公式リファレンス (`docs.typesafe.ai/api.md`) に基づく (2026-09-20時点)。

エンドポイント: POST https://api.typesafe.ai/v1/systemone
  ヘッダ: Authorization: Bearer <API_KEY> / Content-Type: application/json
  リクエスト: {"state":..., "model": "jev-latest", "questions": {"<id>": {"type":..., "instructions":..., "criteria":...}}}
  レスポンス: {"model":..., "answers": {"<id>": {"type":..., ...}}, "usage": {...}}

3つの判断プリミティブ:
  - Noul  : yes/no の確率 (0.0〜1.0)。criteria は任意 ({"true": "...", "false": "..."})。
            レスポンスの値は `noul` フィールドに入る (boolean ではなく確率値であることに注意
            — 第三者記事の一部は "boolean" と説明しているが、公式仕様は明確に確率と定義している)。
  - Choice: 定義済みの選択肢から1つを選ぶ。criteria は必須で {option: 説明 or None} のマップ
            (最大255件)。レスポンスは選ばれた `choice` と `probabilities` (全選択肢の確率)・
            `confidence` (確信度) を含む。
  - Score : 順序付き尺度上の度合い。criteria は必須で尺度水準の配列 (2〜10件)。レスポンスは
            `score` (水準間の値も取りうる確率加重値)・`legend`・`probabilities`・`confidence`。

429 (レート制限) / 529 (過負荷) は公式ドキュメントが指数バックオフでの再試行を推奨しており
(SDKは自動対応)、このクライアントも同じ2つのステータスコードだけを対象に自前でリトライする
(401/422 は再試行しても解決しないため対象外)。

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
import time

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
# 既定値は公式ドキュメント (docs.typesafe.ai/api.md) 記載の実際のエンドポイントホスト。
# .env の JEV_API_BASE_URL で上書き可 (モックサーバ疎通テスト等のため)。
JEV_API_BASE_URL = os.getenv("JEV_API_BASE_URL", "https://api.typesafe.ai").strip()
# URL のパス部分 (ファイルシステムのパスではない)。名前を _PATH で終えると verify_project の絶対パス検査に誤検知される。
JEV_ENDPOINT = "/v1/systemone"
# "jev-latest" は TypeSafe の flagship model エイリアス。.env の JEV_MODEL で固定バージョンに
# 差し替え可 (モデル世代交代時の挙動変化を避けたい場合)。
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest").strip()

HTTP_TIMEOUT = 10
_RETRYABLE_STATUS = {429, 529}  # レート制限 / 過負荷。401/422はリトライしても解決しない
_MAX_ATTEMPTS = 3
_RETRY_INITIAL_DELAY = 1.0


class JevAPIError(Exception):
    """Jev API 呼び出しの失敗 (設定不備・ネットワークエラー・APIエラー応答・不正なレスポンス)。"""
    pass


class JevConfigError(JevAPIError):
    """JEV_API_KEY / JEV_API_BASE_URL が未設定。"""
    pass


# ---------------------------------------------------------
# 内部: 実際の HTTP 配線 (実APIとの差異が出ても直すのはここだけ)
# ---------------------------------------------------------

def _error_body_text(resp) -> str:
    """エラーレスポンスの本文を安全に文字列化する (JSONでなくても落ちない)。"""
    try:
        return str(resp.json())[:300]
    except Exception:
        return (resp.text or "")[:300]


def _jev_request(question_type: str, state, instructions: str,
                  criteria=None, timeout: int = HTTP_TIMEOUT) -> dict:
    """Jev API へ1問の判断を問い合わせる (POST /v1/systemone、質問は1件だけの questions マップ)。

    質問IDには固定で "q" を使う (公式仕様: IDはコード側専用でモデルへは送られず、
    レスポンスの answers も同じキーで返るだけなので、複数質問をまとめて送る
    バッチ機能を使わない今の呼び出し方では固定IDで問題ない)。
    """
    if not JEV_API_KEY:
        raise JevConfigError(
            "JEV_API_KEY が未設定です。.env に設定してください "
            "(ドキュメント: https://docs.typesafe.ai/api.md)"
        )
    if not JEV_API_BASE_URL:
        raise JevConfigError("JEV_API_BASE_URL が空です。.env で上書きした場合は値を確認してください")

    question = {"type": question_type, "instructions": instructions}
    if criteria is not None:
        question["criteria"] = criteria

    payload = {"state": state, "model": JEV_MODEL, "questions": {"q": question}}
    headers = {
        "Authorization": f"Bearer {JEV_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{JEV_API_BASE_URL.rstrip('/')}{JEV_ENDPOINT}"

    delay = _RETRY_INITIAL_DELAY
    resp = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            raise JevAPIError(f"Jev API 呼び出しに失敗しました: {e}") from e

        if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
            print(f"[tool_jev] ⚠️ HTTP {resp.status_code} — {delay}s後にリトライ "
                  f"({attempt + 1}/{_MAX_ATTEMPTS})")
            time.sleep(delay)
            delay *= 2
            continue
        break

    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        raise JevAPIError(
            f"Jev API がエラーを返しました (HTTP {resp.status_code}): {_error_body_text(resp)}"
        ) from e

    try:
        data = resp.json()
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

def jev_choice(state, instructions: str, criteria) -> dict:
    """定義済みの選択肢から1つを選ばせる。

    criteria: 選択肢の list[str] (説明なし) か、{選択肢: 説明 or None} の dict
    (公式仕様どおりの形。最大255件)。list で渡された場合は内部で dict に変換する
    (既存の呼び出し側の大半は説明無しの選択肢リストしか持たないため)。

    戻り値: {"choice": str, "confidence": float}。
    """
    criteria_map = dict(criteria) if isinstance(criteria, dict) else {opt: None for opt in criteria}
    if not criteria_map:
        raise ValueError("jev_choice には最低1つの選択肢 (criteria) が必要です")
    answer = _call("choice", state, instructions, criteria=criteria_map)
    return {
        "choice": answer.get("choice", ""),
        "confidence": float(answer.get("confidence", 0.0)),
    }


def jev_score(state, instructions: str, criteria: list) -> dict:
    """順序付き尺度上の度合いを判断させる。

    criteria: 尺度水準の説明を順序どおりに並べた list (**必須**、2〜10件。公式仕様の制約)。
    戻り値: {"score": float, "confidence": float}。score は水準のインデックス
    (0始まり) を基準にした確率加重値で、水準間の値も取りうる。
    """
    if not criteria or not (2 <= len(criteria) <= 10):
        raise ValueError("jev_score の criteria は2〜10件の順序付き尺度水準が必要です")
    answer = _call("score", state, instructions, criteria=list(criteria))
    return {
        "score": float(answer.get("score", 0.0)),
        "confidence": float(answer.get("confidence", 0.0)),
    }


def jev_noul(state, instructions: str, criteria: dict = None) -> dict:
    """ある条件が真である確率を判断させる。

    criteria: 任意の {"true": "yesの意味", "false": "noの意味"} 説明 (省略可)。
    戻り値: {"probability": float} (0.0〜1.0)。

    Noul は boolean ではなく確率 (公式仕様: "Returns the probability the answer is
    yes")。閾値判定 (例: >=0.5 なら真) は呼び出し側の責務。
    """
    answer = _call("noul", state, instructions, criteria=criteria)
    return {"probability": float(answer.get("noul", 0.0))}
