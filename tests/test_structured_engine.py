# -*- coding: utf-8 -*-
"""
Phase① Structured Output 移行のユニットテスト
(スキーマビルダー / _try_planned_args / _gemini_json_call のフォールバック順序)

Gemini API は一切呼ばない (client をフェイクに差し替え)。

実行: py tests/test_structured_engine.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import engine_plan_execute as engine
from engine_plan_execute import (
    _build_plan_schema,
    _build_critique_schema,
    _build_step_schema,
    _try_planned_args,
)


# ---------------------------------------------------------
# Schema builders
# ---------------------------------------------------------

def test_plan_schema_tool_enum():
    tools = ["tool_web_search", "tool_create_note"]
    schema = _build_plan_schema(tools)
    step = schema["properties"]["plan"]["items"]
    assert step["properties"]["tool"]["enum"] == tools, "tool enum must match valid_tools"
    assert "expected_outcome" in schema["required"]
    assert "plan" in schema["required"]
    print("✅ plan schema: tool enum & required keys")


def test_critique_schema_status_enum():
    tools = ["tool_web_search"]
    schema = _build_critique_schema(tools)
    assert schema["properties"]["status"]["enum"] == ["ok", "needs_revision", "failed"]
    revised_step = schema["properties"]["revised_plan"]["items"]
    assert revised_step["properties"]["tool"]["enum"] == tools, \
        "revised_plan must also constrain tool names"
    print("✅ critique schema: status enum & revised_plan tool enum")


def test_step_schema_required_fields():
    step = _build_step_schema(["t1"])
    assert set(step["required"]) == {"step", "task", "tool"}
    # args_json / args_hint は任意 (プランナーが確定できない場合は省略可)
    assert "args_json" in step["properties"]
    print("✅ step schema: required fields")


# ---------------------------------------------------------
# _try_planned_args
# ---------------------------------------------------------

def test_planned_args_valid_json():
    item = {"tool": "tool_web_search", "args_json": '{"query": "AI 最新 2026"}'}
    assert _try_planned_args(item) == {"query": "AI 最新 2026"}
    print("✅ planned args: valid JSON -> dict")


def test_planned_args_empty_or_missing():
    assert _try_planned_args({"tool": "t", "args_json": ""}) is None
    assert _try_planned_args({"tool": "t", "args_json": "   "}) is None
    assert _try_planned_args({"tool": "t"}) is None
    print("✅ planned args: empty/missing -> None (resolve へフォールバック)")


def test_planned_args_broken_json():
    assert _try_planned_args({"tool": "t", "args_json": "{query: broken"}) is None
    assert _try_planned_args({"tool": "t", "args_json": "[1, 2, 3]"}) is None, \
        "非dictのJSONは拒否する"
    print("✅ planned args: broken/non-dict JSON -> None")


def test_planned_args_empty_dict_ok():
    # 引数不要ツール: "{}" は有効な事前確定 (resolve をスキップできる)
    assert _try_planned_args({"tool": "tool_fetch_daily_news", "args_json": "{}"}) == {}
    print("✅ planned args: '{}' -> 引数不要ツールとして有効")


def test_planned_args_create_note_guard():
    # tool_create_note は filename + content が揃わない限り resolve に回す
    incomplete = {"tool": "tool_create_note", "args_json": '{"filename": "x.md"}'}
    assert _try_planned_args(incomplete) is None
    complete = {
        "tool": "tool_create_note",
        "args_json": '{"filename": "x.md", "content": "本文", "summary": "s"}',
    }
    assert _try_planned_args(complete) is not None
    print("✅ planned args: tool_create_note 必須キーガード")


# ---------------------------------------------------------
# _gemini_json_call: parsed 優先 → safe_json_loads フォールバック
# ---------------------------------------------------------

class _FakeResponse:
    def __init__(self, parsed=None, text=""):
        self.parsed = parsed
        self.text = text


class _FakeModels:
    def __init__(self, response):
        self._response = response
        self.last_config = None

    def generate_content(self, model=None, contents=None, config=None):
        self.last_config = config
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.models = _FakeModels(response)


def test_json_call_prefers_parsed():
    original = engine.client
    try:
        engine.client = _FakeClient(_FakeResponse(
            parsed={"plan": [], "expected_outcome": "done"},
            text='{"ignored": true}',
        ))
        result = engine._gemini_json_call("model-x", "prompt", {"type": "OBJECT"})
        assert result == {"plan": [], "expected_outcome": "done"}
        # response_schema がconfigに渡っていること
        cfg = engine.client.models.last_config
        assert cfg.response_mime_type == "application/json"
        assert cfg.response_schema == {"type": "OBJECT"}
        print("✅ json call: parsed 優先 & schema がconfigに載る")
    finally:
        engine.client = original


def test_json_call_fallback_to_safe_loads():
    original = engine.client
    try:
        # parsed が取れない場合 (スキーマなしJSON mode / preview モデル保険)
        engine.client = _FakeClient(_FakeResponse(
            parsed=None,
            text='```json\n{"status": "ok", "feedback": "fine"}\n```',
        ))
        result = engine._gemini_json_call("model-x", "prompt")
        assert result == {"status": "ok", "feedback": "fine"}
        cfg = engine.client.models.last_config
        assert cfg.response_mime_type == "application/json"
        assert cfg.response_schema is None
        print("✅ json call: parsed なし -> safe_json_loads フォールバック")
    finally:
        engine.client = original


def test_json_call_raises_on_unparseable():
    # 空dictを黙って返すと下流で critique["status"] KeyError になるため、
    # パース不能時は例外 → @retry に再試行させる仕様
    original = engine.client
    original_sleep = engine.time.sleep
    try:
        engine.time.sleep = lambda *_: None  # retry の待機を無効化
        engine.client = _FakeClient(_FakeResponse(parsed=None, text="not json"))
        try:
            engine._gemini_json_call("model-x", "prompt")
            assert False, "should have raised after retries"
        except ValueError:
            pass
        print("✅ json call: パース不能 -> ValueError (retry対象)")
    finally:
        engine.client = original
        engine.time.sleep = original_sleep


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
