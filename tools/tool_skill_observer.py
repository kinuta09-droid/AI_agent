# -*- coding: utf-8 -*-
"""
Skill Observer — Phase 1: Observe
ツール実行の成否・エラー種別を構造化ログ（JSONL）に記録する。
Self-Improving Skills ループの起点。

公開 API:
  observe(tool_name, func, caller, **kwargs) → func の戻り値
  get_failure_summary(top_n=10)             → 失敗率集計テキスト
"""

import os
import json
import time
import datetime
import threading

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "skill_observations.jsonl")

# Phase C0: fan-out で observe() が同一プロセスの複数スレッドから同時に呼ばれる。
# open(append) → write → close の間に別スレッドが割り込むと行が混ざりうるので、
# 書き込みを直列化する。
#
# **プロセスをまたぐ重複は別の話**: 過去に同ファイルで起きた「同一秒の重複エントリ」は
# Discord Bot が二重起動していた (= 別プロセスが2つ) のが原因で、lib.pid_guard の
# 担当。このロックはそれを直さないし、直すつもりもない。
# 今回**新たに**持ち込むプロセス内並列の分だけを塞ぐ。
_WRITE_LOCK = threading.Lock()


def _task_summary(kwargs: dict, max_len: int = 80) -> str:
    """kwargs から人間が読みやすいタスク要約を作る。"""
    if not kwargs:
        return ""
    # 最初の値を文字列化して truncate
    first_val = str(next(iter(kwargs.values())))
    return first_val[:max_len] if len(first_val) > max_len else first_val


def _write_log(tool_name: str, kwargs: dict, success: bool,
               error_type, error_msg, duration_sec: float, caller: str) -> None:
    """1エントリを JSONL に追記する（失敗しても例外を出さない）。"""
    entry = {
        "ts":           datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "tool":         tool_name,
        "task":         _task_summary(kwargs),
        "success":      success,
        "error_type":   error_type,
        "error_msg":    error_msg,
        "duration_sec": round(duration_sec, 2),
        "caller":       caller,
    }
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _WRITE_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass  # ログ失敗でエージェントを止めない


def observe(tool_name: str, func, caller: str, **kwargs):
    """
    ツールを実行し、結果をログに記録して返す。
    例外は re-raise するので呼び出し元のエラーハンドリングをそのまま使える。
    """
    start = time.time()
    try:
        result = func(**kwargs)
        _write_log(tool_name, kwargs, True, None, None, time.time() - start, caller)
        return result
    except Exception as e:
        _write_log(
            tool_name, kwargs, False,
            type(e).__name__,
            str(e)[:200],
            time.time() - start,
            caller,
        )
        raise


def get_failure_summary(top_n: int = 10) -> str:
    """
    skill_observations.jsonl を集計し、失敗率の高いツールを返す。
    /skill-inspect コマンドから呼び出す想定。
    """
    if not os.path.exists(LOG_PATH):
        return "ログファイルが存在しません: " + LOG_PATH

    counts: dict = {}   # tool → {"total": int, "fail": int, "errors": list}
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tool = entry.get("tool", "unknown")
                if tool not in counts:
                    counts[tool] = {"total": 0, "fail": 0, "errors": []}
                counts[tool]["total"] += 1
                if not entry.get("success", True):
                    counts[tool]["fail"] += 1
                    et = entry.get("error_type") or "unknown"
                    if et not in counts[tool]["errors"]:
                        counts[tool]["errors"].append(et)
    except Exception as e:
        return f"ログ読み込みエラー: {e}"

    if not counts:
        return "ログエントリが0件です。"

    # 失敗率でソート
    ranked = sorted(
        counts.items(),
        key=lambda x: x[1]["fail"] / max(x[1]["total"], 1),
        reverse=True,
    )[:top_n]

    lines = ["## Skill Failure Summary\n"]
    for tool, stat in ranked:
        total = stat["total"]
        fail  = stat["fail"]
        rate  = fail / total * 100 if total else 0
        errors = ", ".join(stat["errors"]) if stat["errors"] else "-"
        lines.append(f"- **{tool}**: {fail}/{total} 失敗 ({rate:.0f}%)  errors: {errors}")

    return "\n".join(lines)
