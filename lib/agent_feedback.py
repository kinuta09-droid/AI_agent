# -*- coding: utf-8 -*-
"""
/agent, /chat agent の回答に対するユーザーフィードバック (👍/👎) を記録・集計する。

lib.engagement_log の汎用追記/読み出し基盤 (FileLock 付き・例外を外に出さない) をそのまま使い、
専用ログファイル (logs/agent_feedback.jsonl) へ書き込む。パーソナライズ用の
engagement.jsonl とは目的が違う (こちらは質問・回答全文をプロンプト改良の材料として
残す用途) ため、engagement_log 側の 500字トリム・重み付け設計とは馴染まずファイルを分ける。

CLI:
  py lib/agent_feedback.py            # 直近30日のサマリを表示
  py lib/agent_feedback.py 90         # 直近90日
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:  # `py lib/agent_feedback.py` 直実行を許すため
    sys.path.insert(0, _PROJECT_ROOT)

from lib.engagement_log import log_event, read_events

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


def summarize_feedback(days: int = 30, log_path: str = AGENT_FEEDBACK_LOG_PATH) -> str:
    """収集状況をテキストで返す (CLI 確認用。engagement_log.summarize() の作法を踏襲)。"""
    events = read_events(days=days, log_path=log_path)
    if not events:
        return f"フィードバックログにエントリがありません ({log_path})"

    good = sum(1 for e in events if e.get("event") == "good")
    bad = sum(1 for e in events if e.get("event") == "bad")
    total = len(events)

    reason_freq = {}
    user_freq = {}
    for e in events:
        meta = e.get("meta") or {}
        if e.get("event") == "bad":
            reason = meta.get("reason") or "(理由未記入)"
            reason_freq[reason] = reason_freq.get(reason, 0) + 1
        uid = meta.get("user_id")
        key = str(uid) if uid is not None else "(不明)"
        user_freq[key] = user_freq.get(key, 0) + 1

    lines = [f"## Agent Feedback Summary (直近{days}日 / {total}件)\n"]

    lines.append("### good/bad")
    lines.append(f"- good: {good} ({good / total * 100:.0f}%)")
    lines.append(f"- bad: {bad} ({bad / total * 100:.0f}%)")

    if reason_freq:
        lines.append("\n### bad理由 TOP10")
        for reason, n in sorted(reason_freq.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"- ({n}) {reason[:80]}")

    lines.append("\n### user_id別件数")
    for uid, n in sorted(user_freq.items(), key=lambda x: -x[1]):
        lines.append(f"- {uid}: {n}")

    return "\n".join(lines)


if __name__ == "__main__":
    _days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(summarize_feedback(days=_days))
