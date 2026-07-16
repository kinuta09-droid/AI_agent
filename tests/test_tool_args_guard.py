# -*- coding: utf-8 -*-
"""
ツール引数ガードのユニットテスト
(_tool_param_spec / _sanitize_tool_args / _resolve_step_args)

再現バグ (logs/skill_observations.jsonl 2026-07-17T03:54:36):
    TypeError: tool_read_note() got an unexpected keyword argument 'plan'
  プランナーが args_json に計画全体 {"plan": [{"tool_read_note": {...}}, ...]}
  を吐き、検証なしで **展開されていた。

Gemini API は一切呼ばない (_resolve_tool_args をフェイクに差し替え)。

実行: py tests/test_tool_args_guard.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import engine_plan_execute as engine
from engine_plan_execute import (
    _tool_param_spec,
    _sanitize_tool_args,
    _resolve_step_args,
)


# ---------------------------------------------------------
# 検証対象のダミーツール (本物のシグネチャを模す)
# ---------------------------------------------------------

def fake_read_note(filename: str) -> str:
    """tool_read_note と同じシグネチャ (必須引数1つ)"""
    return f"read:{filename}"


def fake_create_note(filename: str, content: str, summary: str = "") -> str:
    """tool_create_note と同じシグネチャ (必須2 + 任意1)"""
    return f"created:{filename}"


def fake_no_args() -> str:
    """tool_fetch_daily_news のような引数なしツール"""
    return "news"


def fake_var_kwargs(**kwargs) -> str:
    """**kwargs を受けるツールは何でも通す"""
    return "ok"


# ---------------------------------------------------------
# _tool_param_spec
# ---------------------------------------------------------

def test_param_spec_basic():
    accepted, required, has_var_kw = _tool_param_spec(fake_create_note)
    assert accepted == {"filename", "content", "summary"}, accepted
    assert required == {"filename", "content"}, required
    assert has_var_kw is False
    print("✅ param spec: 受理キー/必須キー/**kwargs無し を正しく抽出")


def test_param_spec_var_kwargs():
    accepted, required, has_var_kw = _tool_param_spec(fake_var_kwargs)
    assert has_var_kw is True
    assert required == set()
    print("✅ param spec: **kwargs を検出")


def test_param_spec_unknown_signature():
    # 組込み関数など signature が取れないものは None (検証スキップ)
    assert _tool_param_spec(len) is None or isinstance(_tool_param_spec(len), tuple)
    print("✅ param spec: signature 不明でも例外を出さない")


# ---------------------------------------------------------
# _sanitize_tool_args — 本体バグの再現
# ---------------------------------------------------------

def test_rejects_plan_wrapper_list():
    """本件の再現: {"plan": [...]} は剥がさず拒否 → 再解決に回す"""
    bad = {"plan": [
        {"tool_read_note": {"filename": "Briefing_2026-03-14.md"}},
        {"tool_read_note": {"filename": "Briefing_2026-03-15.md"}},
    ]}
    result = _sanitize_tool_args("tool_read_note", fake_read_note, bad)
    assert result is None, f"plan ラッパーは拒否すべき: {result}"
    print("✅ sanitize: {'plan': [...]} を拒否 (TypeError の直接原因)")


def test_plan_wrapper_never_reaches_tool():
    """ガードを通した引数で実際に呼んでも TypeError にならないこと"""
    bad = {"plan": [{"tool_read_note": {"filename": "a.md"}}]}
    result = _sanitize_tool_args("tool_read_note", fake_read_note, bad)
    if result is not None:
        fake_read_note(**result)  # ここで TypeError が出たらテスト失敗
        assert False, "拒否されるべき引数が通過した"
    print("✅ sanitize: 不正引数がツールに到達しない")


def test_unwraps_single_dict_wrapper():
    """{"args": {"filename": "x.md"}} は1段剥がして使える"""
    result = _sanitize_tool_args(
        "tool_read_note", fake_read_note, {"args": {"filename": "x.md"}}
    )
    assert result == {"filename": "x.md"}, result
    assert fake_read_note(**result) == "read:x.md"
    print("✅ sanitize: ラッパー1枚を剥がして復旧")


def test_drops_unknown_keys():
    """シグネチャに無いキーは捨てて、必須が揃っていれば実行可能にする"""
    result = _sanitize_tool_args(
        "tool_read_note", fake_read_note,
        {"filename": "x.md", "reason": "なぜ読むか", "step": 2},
    )
    assert result == {"filename": "x.md"}, result
    print("✅ sanitize: 未知キーを除去して必須引数のみ通す")


def test_rejects_missing_required():
    result = _sanitize_tool_args(
        "tool_create_note", fake_create_note, {"filename": "x.md"}
    )
    assert result is None, f"必須 content 欠落は拒否すべき: {result}"
    print("✅ sanitize: 必須引数欠落を拒否")


def test_keeps_optional_args():
    result = _sanitize_tool_args(
        "tool_create_note", fake_create_note,
        {"filename": "x.md", "content": "本文", "summary": "要約"},
    )
    assert result == {"filename": "x.md", "content": "本文", "summary": "要約"}
    print("✅ sanitize: 正常な引数はそのまま通す")


def test_no_arg_tool_drops_everything():
    """引数なしツールに引数が来たら全部捨てて {} で呼べるようにする"""
    result = _sanitize_tool_args("tool_fetch_daily_news", fake_no_args,
                                 {"query": "AI"})
    assert result == {}, result
    assert fake_no_args(**result) == "news"
    print("✅ sanitize: 引数なしツールは余剰引数を全除去")


def test_var_kwargs_passes_through():
    args = {"anything": 1, "plan": [1, 2]}
    result = _sanitize_tool_args("x", fake_var_kwargs, args)
    assert result == args, result
    print("✅ sanitize: **kwargs ツールは素通し")


def test_rejects_non_dict():
    for bad in ([{"tool_read_note": {}}], "filename=x.md", None, 42):
        assert _sanitize_tool_args("tool_read_note", fake_read_note, bad) is None
    print("✅ sanitize: dict 以外を拒否")


def test_rejects_non_string_keys():
    result = _sanitize_tool_args("tool_read_note", fake_read_note, {1: "x.md"})
    assert result is None, result
    print("✅ sanitize: 非文字列キー (** 展開不可) を拒否")


# ---------------------------------------------------------
# _resolve_step_args — 経路統合 (Gemini はフェイク)
# ---------------------------------------------------------

def _with_fake_resolver(return_value, calls):
    def _fake(tool_name, args_hint, task, context):
        calls.append(tool_name)
        return return_value
    return _fake


def test_bad_planned_args_fall_back_to_resolver():
    """args_json が不正 → Gemini 再解決へフォールバックして復旧する"""
    original = engine._resolve_tool_args
    calls = []
    try:
        engine._resolve_tool_args = _with_fake_resolver(
            {"filename": "Briefing_2026-03-14.md"}, calls
        )
        item = {
            "tool": "tool_read_note",
            "args_json": '{"plan": [{"tool_read_note": {"filename": "a.md"}}]}',
            "args_hint": "読むノート名",
        }
        args = _resolve_step_args("tool_read_note", fake_read_note, item,
                                  "ノートを読む", [])
        assert args == {"filename": "Briefing_2026-03-14.md"}, args
        assert calls == ["tool_read_note"], "Gemini 再解決が呼ばれるべき"
        print("✅ resolve_step: 不正 args_json → 再解決で復旧")
    finally:
        engine._resolve_tool_args = original


def test_good_planned_args_skip_resolver():
    """正常な args_json は Gemini を呼ばずに使う (Phase① 高速化を壊さない)"""
    original = engine._resolve_tool_args
    calls = []
    try:
        engine._resolve_tool_args = _with_fake_resolver({}, calls)
        item = {
            "tool": "tool_read_note",
            "args_json": '{"filename": "a.md"}',
            "args_hint": "",
        }
        args = _resolve_step_args("tool_read_note", fake_read_note, item,
                                  "ノートを読む", [])
        assert args == {"filename": "a.md"}, args
        assert calls == [], "正常時に Gemini を呼んではいけない"
        print("✅ resolve_step: 正常 args_json は再解決せず高速経路を維持")
    finally:
        engine._resolve_tool_args = original


def test_both_paths_bad_returns_none():
    """計画も再解決も不正 → None (呼び出し側が明示的にステップ失敗させる)"""
    original = engine._resolve_tool_args
    calls = []
    try:
        engine._resolve_tool_args = _with_fake_resolver({"plan": []}, calls)
        item = {
            "tool": "tool_read_note",
            "args_json": '{"plan": [1, 2]}',
            "args_hint": "",
        }
        args = _resolve_step_args("tool_read_note", fake_read_note, item,
                                  "ノートを読む", [])
        assert args is None, args
        print("✅ resolve_step: 両経路NG → None (TypeError ではなく制御された失敗)")
    finally:
        engine._resolve_tool_args = original


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
