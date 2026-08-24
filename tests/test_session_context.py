# -*- coding: utf-8 -*-
"""
Phase③ /agent 会話継続 (短期セッション記憶) のユニットテスト
(generate_plan へのsession_context注入 / run_plan_and_execute パススルー /
 チャンネル毎セッションストアの TTL・wikilink抽出・切り詰め)

Gemini API は一切呼ばない (_gemini_json_call / generate_plan をフェイクに差し替え)。

実行: py tests/test_session_context.py
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import engine_plan_execute as engine
from commands import bot_commands_agent as agent_cmd


# ---------------------------------------------------------
# generate_plan: session_context 注入
# ---------------------------------------------------------

def _capture_plan_prompt(user_query, session_context=""):
    """_gemini_json_call をフェイクに差し替えてプロンプトをキャプチャする。"""
    captured = {}
    original = engine._gemini_json_call

    def fake_json_call(model, prompt, schema):
        captured["prompt"] = prompt
        return {"plan": [], "expected_outcome": ""}

    engine._gemini_json_call = fake_json_call
    try:
        engine.generate_plan(user_query, session_context=session_context)
    finally:
        engine._gemini_json_call = original
    return captured["prompt"]


def test_generate_plan_injects_session_block():
    prompt = _capture_plan_prompt("さっきの結果を保存して", session_context="- 直前の要求: AIニュース調査")
    assert "【直前のタスク(参考)】" in prompt, "セッションブロックのヘッダーが注入されること"
    assert "- 直前の要求: AIニュース調査" in prompt, "session_context 本文が注入されること"
    print("✅ generate_plan: session_context あり -> 【直前のタスク(参考)】ブロック注入")


def test_generate_plan_no_session_block_when_empty():
    prompt = _capture_plan_prompt("AIニュースを調べて")
    assert "【直前のタスク(参考)】" not in prompt, "空のsession_contextでは注入しないこと"
    print("✅ generate_plan: session_context なし -> ブロック非注入")


# ---------------------------------------------------------
# run_plan_and_execute: パススルー
# ---------------------------------------------------------

def test_run_plan_and_execute_passes_session_context():
    captured = {}
    original = engine.generate_plan

    def fake_generate_plan(user_query, session_context=""):
        captured["session_context"] = session_context
        return {"plan": [], "expected_outcome": ""}  # 空plan -> 早期return

    engine.generate_plan = fake_generate_plan
    try:
        result = engine.run_plan_and_execute("test", session_context="PREV_CONTEXT")
    finally:
        engine.generate_plan = original

    assert captured["session_context"] == "PREV_CONTEXT", \
        "session_context が generate_plan にパススルーされること"
    assert result == "計画を生成できませんでした。", "空planで早期returnすること"
    print("✅ run_plan_and_execute: session_context パススルー")


def test_run_plan_and_execute_default_empty():
    captured = {}
    original = engine.generate_plan

    def fake_generate_plan(user_query, session_context=""):
        captured["session_context"] = session_context
        return {"plan": [], "expected_outcome": ""}

    engine.generate_plan = fake_generate_plan
    try:
        engine.run_plan_and_execute("test")
    finally:
        engine.generate_plan = original

    assert captured["session_context"] == "", \
        "デフォルトは空文字 (scheduler / main.py 無影響)"
    print("✅ run_plan_and_execute: デフォルト引数は空文字")


# ---------------------------------------------------------
# _goal_references_previous_session (session_context 注入のコード側ゲート)
# ---------------------------------------------------------

def test_goal_references_previous_session_matches_reference_words():
    ref = agent_cmd._goal_references_previous_session
    assert ref("さっきの続きをやって")
    assert ref("それを保存して")
    assert ref("同じテーマでもう1本書いて")
    print("✅ _goal_references_previous_session: 参照語ありは True")


def test_goal_references_previous_session_rejects_unrelated_goal():
    ref = agent_cmd._goal_references_previous_session
    assert not ref("AIニュースを調べて")
    assert not ref("最近のクリプト市況をSNS用にまとめて投稿文を作って")
    print("✅ _goal_references_previous_session: 無関係な要求は False")


def test_cmd_agent_session_context_gated_by_goal_reference():
    """cmd_agent が実際に呼ぶ形 (goal判定 -> _get_session_context) の統合確認。

    無関係な goal では、セッションに直前タスクが残っていても session_context が
    空文字になり Planner へ渡らないこと (2026-08-24 情報汚染インシデントと同型の
    リスクへの予防策)。"""
    agent_cmd._sessions.clear()
    agent_cmd._update_session(777, "ARMSの実行エンジンの設計を調べて", "内部アーキテクチャの詳細...")

    def _resolve_session_context(channel_id, goal):
        return (
            agent_cmd._get_session_context(channel_id)
            if agent_cmd._goal_references_previous_session(goal) else ""
        )

    assert _resolve_session_context(777, "最近の出来事をSNS用にまとめて投稿文を作って") == "", \
        "無関係な要求には直前タスクの内部情報を渡してはいけない"
    assert _resolve_session_context(777, "さっきの内容を保存して") != "", \
        "参照語がある要求には従来通り注入されること"
    print("✅ cmd_agent相当: 無関係なgoalではsession_contextが空になる")


# ---------------------------------------------------------
# セッションストア (_update_session / _get_session_context)
# ---------------------------------------------------------

def test_session_store_roundtrip():
    agent_cmd._sessions.clear()
    agent_cmd._update_session(111, "AIニュースを調べて", "## 結論\n保存済み: [[AI_News_2026]]")
    ctx = agent_cmd._get_session_context(111)
    assert "- 直前の要求: AIニュースを調べて" in ctx
    assert "- 直前の結果(先頭): ## 結論" in ctx
    assert "[[AI_News_2026]]" in ctx
    print("✅ session store: 保存 -> 文脈テキスト取得")


def test_session_store_unknown_channel():
    agent_cmd._sessions.clear()
    assert agent_cmd._get_session_context(999) == "", "未登録チャンネルは空文字"
    print("✅ session store: 未登録チャンネル -> 空文字")


def test_session_store_ttl_expiry():
    agent_cmd._sessions.clear()
    agent_cmd._update_session(222, "goal", "result")
    # ts を直接過去に書き換えて期限切れを再現
    agent_cmd._sessions[222]["ts"] = time.time() - agent_cmd._SESSION_TTL_SECONDS - 1
    assert agent_cmd._get_session_context(222) == "", "TTL超過は空文字"
    assert 222 not in agent_cmd._sessions, "期限切れエントリは削除されること"
    print("✅ session store: TTL 30分超過 -> 破棄")


def test_session_store_wikilink_dedup_and_limit():
    agent_cmd._sessions.clear()
    links = "".join(f"[[note_{i}]]" for i in range(8))
    result = f"[[dup]][[dup]]{links}"
    agent_cmd._update_session(333, "goal", result)
    files = agent_cmd._sessions[333]["files"]
    assert files[0] == "dup" and files.count("dup") == 1, "wikilinkは重複除去されること"
    assert len(files) == agent_cmd._MAX_SESSION_FILES, "最大5件に制限されること"
    print("✅ session store: wikilink 重複除去 + 5件上限")


def test_session_store_truncation():
    agent_cmd._sessions.clear()
    agent_cmd._update_session(444, "G" * 500, "R" * 5000)
    entry = agent_cmd._sessions[444]
    assert len(entry["goal"]) == agent_cmd._GOAL_SNIPPET_CHARS, "goalは200字に切り詰め"
    assert len(entry["summary"]) == agent_cmd._RESULT_SNIPPET_CHARS, "resultは先頭1000字"
    print("✅ session store: goal 200字 / result 1000字 切り詰め")


def test_failure_prefixes_cover_engine_failure_strings():
    """run_plan_and_execute の全失敗パターンが保存フィルタに引っかかること。"""
    failure_results = [
        "Error: division by zero",              # cmd_agent の except 節
        "計画生成に失敗しました: timeout",        # generate_plan 例外
        "計画を生成できませんでした。",            # 空plan早期return
        "タスク失敗: Critic rejected",           # critique status=failed
    ]
    for r in failure_results:
        assert r.startswith(agent_cmd._FAILURE_PREFIXES), \
            f"失敗結果がフィルタを通過してしまう: {r}"
    assert not "## 📋 結論\n成功".startswith(agent_cmd._FAILURE_PREFIXES), \
        "正常結果はフィルタされないこと"
    print("✅ failure filter: 全失敗パターンがセッション保存から除外される")


def test_session_store_no_files():
    agent_cmd._sessions.clear()
    agent_cmd._update_session(555, "goal", "リンクなしの結果")
    ctx = agent_cmd._get_session_context(555)
    assert "- 関連ファイル: なし" in ctx, "wikilinkなしの場合は「なし」と表記"
    print("✅ session store: wikilinkなし -> 関連ファイル「なし」")


if __name__ == "__main__":
    tests = [
        test_goal_references_previous_session_matches_reference_words,
        test_goal_references_previous_session_rejects_unrelated_goal,
        test_cmd_agent_session_context_gated_by_goal_reference,
        test_generate_plan_injects_session_block,
        test_generate_plan_no_session_block_when_empty,
        test_run_plan_and_execute_passes_session_context,
        test_run_plan_and_execute_default_empty,
        test_session_store_roundtrip,
        test_session_store_unknown_channel,
        test_session_store_ttl_expiry,
        test_session_store_wikilink_dedup_and_limit,
        test_session_store_truncation,
        test_failure_prefixes_cover_engine_failure_strings,
        test_session_store_no_files,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    print(f"\n{'=' * 50}")
    if failed:
        print(f"❌ {failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print(f"✅ ALL {len(tests)} tests PASSED")
