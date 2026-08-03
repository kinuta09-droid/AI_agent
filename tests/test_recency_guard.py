# -*- coding: utf-8 -*-
"""直近性キーワード検知によるコード側ガードの回帰テスト (plans/recency-detection-reliability.md)

背景: 「今日/急に recency_days を付けて」というプロンプト指示はLLMの確率的な
判断に依存し、同じ言い回しの質問でも実行のたびに適用されたりされなかったりする
ことが2026-08-03に4回連続の実測で確認された。この回帰を防ぐため、
engine_plan_execute._run_step にキーワード検知の決定的ガードを追加した
(LLMを一切経由しない)。ここではその判定ロジックとステップ実行時の
強制付与の両方を Gemini を1回も叩かずに検証する。

実行: py tests/test_recency_guard.py
"""
import os
import sys
import json
import datetime

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import engine_plan_execute as engine
from engine_plan_execute import _has_recency_intent, _current_year_month

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _fake_web_search(query: str, recency_days: int = None) -> str:
    """本物の tool_web_search と同じシグネチャのスタブ (_sanitize_tool_args 通過用)。"""
    return "Success: dummy"


def _run_step_with(task: str, args: dict):
    """observe をパススルーに差し替えた状態で _run_step を1回実行する。

    args_json で引数を計画時確定にし、_resolve_tool_args (Gemini) を経由しない。
    """
    orig_observe = engine.observe
    engine.observe = lambda name, func, caller, **kw: func(**kw)
    try:
        item = {
            "step": 1, "task": task, "tool": "tool_web_search",
            "args_json": json.dumps(args, ensure_ascii=False),
        }
        tools_map = {"tool_web_search": _fake_web_search}
        return engine._run_step(item, tools_map, context=[])
    finally:
        engine.observe = orig_observe


# ---------------------------------------------------------
# 1. _has_recency_intent (純関数)
# ---------------------------------------------------------

def test_has_recency_intent_keywords():
    print("\n[1] _has_recency_intent: キーワードあり/なし")
    for kw in ("今日", "本日", "急に", "直近", "たった今", "さっき", "さきほど"):
        check(f"『{kw}』を含む文で True", _has_recency_intent(f"{kw}のニュースを調べて"))
    check("キーワードを含まない文で False",
          not _has_recency_intent("猫の飼い方を教えて"))
    check("空文字で False", not _has_recency_intent(""))


def test_current_year_month_matches_now():
    print("\n[2] _current_year_month: 実際の年月と一致")
    now = datetime.datetime.now()
    expected = f"{now.year}年{now.month}月"
    check("フォーマットが一致", _current_year_month() == expected,
          f"{_current_year_month()!r} != {expected!r}")


# ---------------------------------------------------------
# 2. _run_step 内の強制付与ロジック (結合)
# ---------------------------------------------------------

def test_forces_recency_days_when_keyword_present():
    print("\n[3] キーワードあり + recency_days未設定 → 強制付与される")
    result = _run_step_with("今日のガソリン価格が急に下がった理由を調べる",
                            {"query": "ガソリン価格"})
    check("ステップは成功扱い", result["success"], str(result))
    check("recency_days が10で強制付与される", result["args"].get("recency_days") == 10,
          str(result["args"]))
    check("クエリに今日の年月が追記される",
          _current_year_month() in result["args"].get("query", ""),
          str(result["args"]))


def test_does_not_add_recency_days_without_keyword():
    print("\n[4] キーワードなし + recency_days未設定 → 何もしない")
    result = _run_step_with("猫の飼い方を教えて", {"query": "猫 飼い方"})
    check("recency_days は付かない", "recency_days" not in result["args"]
          or result["args"].get("recency_days") is None, str(result["args"]))
    check("クエリに年月も追記されない",
          _current_year_month() not in result["args"].get("query", ""),
          str(result["args"]))


def test_does_not_override_existing_recency_days():
    print("\n[5] 既に recency_days が設定済み → 上書きしない (LLMの判断を優先)")
    result = _run_step_with("今日のガソリン価格を調べる",
                            {"query": "ガソリン価格", "recency_days": 3})
    check("既存の recency_days (3) を維持する", result["args"].get("recency_days") == 3,
          str(result["args"]))


def test_does_not_double_append_year_month():
    print("\n[6] クエリに既に年月が入っている → 二重付与しない")
    year_month = _current_year_month()
    result = _run_step_with("今日のガソリン価格を調べる",
                            {"query": f"ガソリン価格 {year_month}", "recency_days": 10})
    query = result["args"].get("query", "")
    check("年月が1回しか出現しない", query.count(year_month) == 1, repr(query))


if __name__ == "__main__":
    print("=" * 55)
    print(" 直近性キーワード検知ガード 回帰テスト")
    print("=" * 55)

    test_has_recency_intent_keywords()
    test_current_year_month_matches_now()
    test_forces_recency_days_when_keyword_present()
    test_does_not_add_recency_days_without_keyword()
    test_does_not_override_existing_recency_days()
    test_does_not_double_append_year_month()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
