# -*- coding: utf-8 -*-
"""agent_scheduler._run_auto_deep_dive() の topic/context_hint 抽出ロジックの回帰テスト。

実LLM/実Tavily/実Vault不要 (latest_features.json は一時ディレクトリに用意し、
_future_search は差し替えて呼び出し引数だけ検証する)。

2026-08-07 追加理由: context_hint (tech_stack を検索文脈補強として渡す修正) は
tool_future_search.py 側のユニットテストでしか間接的に守られておらず、
呼び出し元のフィールド抽出 (top.get("tech_stack", ...)) 自体は無検査だった。

実行: py tests/test_agent_scheduler_deep_dive.py
"""
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import agent_scheduler as sched


def _write_features(tmp_dir: str, features: list) -> None:
    xtrend_dir = os.path.join(tmp_dir, "40_Report", "XTrend")
    os.makedirs(xtrend_dir, exist_ok=True)
    with open(os.path.join(xtrend_dir, "latest_features.json"), "w", encoding="utf-8") as fh:
        json.dump({"date": "2026-08-06", "features": features}, fh, ensure_ascii=False)


def _run_with_stubbed_env(features: list):
    """一時Vaultにfeaturesを書き、_future_searchを差し替えて呼び出し引数を捕捉する。"""
    calls = []
    with tempfile.TemporaryDirectory() as tmp:
        _write_features(tmp, features)
        original_dir = sched._WORK_DIR
        original_search = sched._future_search
        sched._WORK_DIR = tmp
        sched._future_search = lambda topic, **kw: (calls.append((topic, kw)), "OK")[1]
        try:
            result = sched._run_auto_deep_dive()
        finally:
            sched._WORK_DIR = original_dir
            sched._future_search = original_search
    return result, calls


def test_tech_stack_forwarded_as_context_hint():
    """arms_affinity最高のfeatureのtech_stackがcontext_hintとしてFutureSearchへ渡ること。"""
    result, calls = _run_with_stubbed_env([
        {"feature_name": "Maple-Preview", "tech_stack": "LLM, Ternary Weights, C++",
         "arms_affinity": 5, "likes": 10},
        {"feature_name": "Other", "tech_stack": "N/A", "arms_affinity": 2, "likes": 999},
    ])
    assert result == "OK"
    assert len(calls) == 1, calls
    topic, kwargs = calls[0]
    assert topic == "Maple-Preview", topic
    assert kwargs.get("context_hint") == "LLM, Ternary Weights, C++", kwargs
    print("✅ arms_affinity最高のfeatureのtech_stackがcontext_hintとして渡る (engagement最高の方は選ばれない)")


def test_missing_tech_stack_field_falls_back_to_empty_hint():
    """旧形式のlatest_features.json (tech_stackキー無し) でもKeyErrorにならずcontext_hint=''で動くこと。"""
    _, calls = _run_with_stubbed_env(
        [{"feature_name": "OldFormat", "arms_affinity": 3, "likes": 1}]
    )
    assert calls[0][1].get("context_hint") == "", calls
    print("✅ tech_stackキーが無い旧形式JSONでもKeyErrorにならずcontext_hint=''で動く")


if __name__ == "__main__":
    test_tech_stack_forwarded_as_context_hint()
    test_missing_tech_stack_field_falls_back_to_empty_hint()
    print("\n🎉 ALL AGENT SCHEDULER DEEP-DIVE TESTS PASSED")
