# -*- coding: utf-8 -*-
"""
/agent, /chat agent の回答に対するユーザーフィードバック (👍/👎) を記録する。

lib.engagement_log の汎用追記基盤 (FileLock 付き・例外を外に出さない) をそのまま使い、
専用ログファイル (logs/agent_feedback.jsonl) へ書き込む。パーソナライズ用の
engagement.jsonl とは目的が違う (こちらは質問・回答全文をプロンプト改良の材料として
残す用途) ため、engagement_log 側の 500字トリム・重み付け設計とは馴染まずファイルを分ける。
"""
import os

from lib.engagement_log import log_event

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_FEEDBACK_LOG_PATH = os.path.join(_PROJECT_ROOT, "logs", "agent_feedback.jsonl")


def save_feedback(rating: str, goal: str, result: str, reason: str = "",
                   user_id: int = None, channel_id: int = None,
                   log_path: str = AGENT_FEEDBACK_LOG_PATH) -> bool:
    """フィードバック1件を追記する。rating は "good" か "bad"。

    失敗しても例外は出さず False を返す (log_event と同じ契約)。
    """
    meta = {"result": result, "reason": reason, "user_id": user_id, "channel_id": channel_id}
    return log_event(
        event=rating, item=goal, source="agent_feedback",
        item_type="agent_answer", meta=meta, log_path=log_path,
    )
