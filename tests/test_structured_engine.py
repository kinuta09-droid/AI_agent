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
    _sanitize_tool_args,
)


def _dummy_browser_use(url, task="このページの主要な情報を要約して"):
    pass


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
# _sanitize_tool_args — ラッパー剥がし
# ---------------------------------------------------------

def test_sanitize_args_flat_passthrough():
    args = {"url": "https://example.com", "task": "在庫を教えて"}
    assert _sanitize_tool_args("tool_browser_use", _dummy_browser_use, args) == args
    print("✅ sanitize args: フラットな引数はそのまま通る")


def test_sanitize_args_single_key_wrapper():
    args = {"args": {"url": "https://example.com"}}
    assert _sanitize_tool_args("tool_browser_use", _dummy_browser_use, args) == {
        "url": "https://example.com"
    }
    print("✅ sanitize args: 単一キーラッパー ({'args': {...}}) は剥がされる")


def test_sanitize_args_function_calling_style_wrapper():
    """Geminiが {"tool_name": "...", "arguments": {...}} という function-calling
    風の形で応答した場合でも、tool_name をメタデータとして無視し arguments の
    中身を引数として取り出せること (2026-08-28、実運用でtool_browser_useの
    url が丸ごと失われて Step failed になった実例の回帰)。"""
    args = {
        "tool_name": "tool_browser_use",
        "arguments": {"url": "https://www.komeri.com/shop/g/g2164370"},
    }
    result = _sanitize_tool_args("tool_browser_use", _dummy_browser_use, args)
    assert result == {"url": "https://www.komeri.com/shop/g/g2164370"}, result
    print("✅ sanitize args: {tool_name, arguments} 形式でもurlが救済される")


def test_sanitize_args_metadata_key_alone_still_fails():
    """tool_name しか無く実引数が本当に欠けている場合は、従来通り失敗する
    (メタデータ除去が必須引数チェックを無効化しないことの回帰)。"""
    args = {"tool_name": "tool_browser_use"}
    assert _sanitize_tool_args("tool_browser_use", _dummy_browser_use, args) is None
    print("✅ sanitize args: メタデータキーのみ(実引数無し)は従来通り失敗する")


def test_sanitize_args_plan_list_wrapper_rejected():
    """{"plan": [...]} のように中身がlistの場合は、どのステップの引数か判別
    できないため剥がさず拒否する (既存動作の回帰)。"""
    args = {"plan": [{"tool": "tool_browser_use"}]}
    assert _sanitize_tool_args("tool_browser_use", _dummy_browser_use, args) is None
    print("✅ sanitize args: plan配列ラッパーは剥がさず拒否する")


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


def test_json_call_sets_low_temperature_for_deterministic_routing():
    """計画生成/Critic判定は既定温度(~1.0)だと同一クエリでもツール選択が揺れる実測があった
    (「〜についてリサーチして」で tool_notebooklm_research が選ばれる回と選ばれない回が混在)。
    低温度でサンプリング揺らぎを減らしていることを固定する。"""
    original = engine.client
    try:
        engine.client = _FakeClient(_FakeResponse(
            parsed={"plan": [], "expected_outcome": "done"},
            text='{"ignored": true}',
        ))
        engine._gemini_json_call("model-x", "prompt", {"type": "OBJECT"})
        cfg = engine.client.models.last_config
        assert cfg.temperature is not None and cfg.temperature <= 0.3, cfg.temperature
        print("✅ json call: 低temperatureで呼ばれている")
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


# ---------------------------------------------------------
# execute_plan: initial_context が revision をまたいで引き継がれる
# ---------------------------------------------------------

def test_execute_plan_carries_initial_context_into_arg_resolution():
    """execute_plan の2回目の呼び出し (Criticのrevisionによる再実行) が、
    initial_context 経由で1回目の実行結果を _resolve_tool_args に渡せること。

    2026-08-17実測: execute_plan は毎回 accumulated_context=[] から始まっていたため、
    Criticがrevised_planを出してrun_plan_and_executeがexecute_planを再度呼ぶたびに、
    前revisionの実行結果 (例: tool_create_noteが実際に作ったファイル名) が
    _resolve_tool_args から見えなくなっていた。実際に「1回目のrevisionでノート作成
    →Criticが再計画→2回目のrevisionのtool_update_noteが、見えなくなった実ファイル名の
    代わりにタスク説明文中の語句("30_Main"というフォルダ名)を誤ってファイル名として
    拾い、『File 30_Main.md not found』で失敗する」という実害が出た。
    """
    def _probe_tool():
        return "probe ok"

    orig_get_tools = engine._get_tools_config
    orig_resolve = engine._resolve_tool_args
    seen_contexts = []

    def _fake_get_tools_config():
        config = [{"name": "tool_probe", "usage": "tool_probe()", "description": "probe"}]
        return config, {"tool_probe": _probe_tool}

    def _spy_resolve_tool_args(tool_name, args_hint, task, context):
        seen_contexts.append(list(context))
        return {}

    engine._get_tools_config = _fake_get_tools_config
    engine._resolve_tool_args = _spy_resolve_tool_args
    try:
        plan1 = [{"step": 1, "task": "1回目", "tool": "tool_probe", "args_hint": "x"}]
        result1 = engine.execute_plan(plan1)
        assert result1["success"], result1

        plan2 = [{"step": 1, "task": "2回目", "tool": "tool_probe", "args_hint": "x"}]
        engine.execute_plan(plan2, initial_context=result1["results"])

        assert seen_contexts[0] == [], "1回目の呼び出しは前提通り空contextで始まるべき"
        assert len(seen_contexts) == 2, seen_contexts
        assert seen_contexts[1] and seen_contexts[1][0]["task"] == "1回目", (
            f"2回目の呼び出しが1回目の結果を見られていない: {seen_contexts[1]}")
    finally:
        engine._get_tools_config = orig_get_tools
        engine._resolve_tool_args = orig_resolve
    print("✅ execute_plan: initial_context が2回目の呼び出しに引き継がれる")


def test_execute_plan_defaults_to_empty_context_when_omitted():
    """initial_context省略時は従来通り空から始まること (後方互換性)。"""
    def _probe_tool():
        return "probe ok"

    orig_get_tools = engine._get_tools_config
    orig_resolve = engine._resolve_tool_args
    seen_contexts = []

    def _fake_get_tools_config():
        config = [{"name": "tool_probe", "usage": "tool_probe()", "description": "probe"}]
        return config, {"tool_probe": _probe_tool}

    def _spy_resolve_tool_args(tool_name, args_hint, task, context):
        seen_contexts.append(list(context))
        return {}

    engine._get_tools_config = _fake_get_tools_config
    engine._resolve_tool_args = _spy_resolve_tool_args
    try:
        plan = [{"step": 1, "task": "単発", "tool": "tool_probe", "args_hint": "x"}]
        engine.execute_plan(plan)
        assert seen_contexts == [[]], seen_contexts
    finally:
        engine._get_tools_config = orig_get_tools
        engine._resolve_tool_args = orig_resolve
    print("✅ execute_plan: initial_context省略時は空contextのまま (後方互換)")


# ---------------------------------------------------------
# CriticStatus: EVAL_ERROR が OK に丸め込まれないこと
# ---------------------------------------------------------

def test_critique_status_str_enum_behaves_like_plain_string():
    """CriticStatus は str派生なので、既存コードの `status == "ok"` 型の比較や
    log_execution 経由の json.dump もそのまま通ることを確認する。"""
    assert engine.CriticStatus.OK == "ok"
    assert engine.CriticStatus.EVAL_ERROR == "eval_error"
    assert str(engine.CriticStatus.EVAL_ERROR) == "eval_error", (
        "Enum既定の __str__ だと 'CriticStatus.EVAL_ERROR' になりログが読みにくい")
    assert json.dumps({"status": engine.CriticStatus.EVAL_ERROR}) == '{"status": "eval_error"}'
    print("✅ CriticStatus: str派生としての互換性 (==比較 / str() / JSON化)")


def test_critique_results_distinguishes_eval_error_from_ok():
    """Critic呼び出し自体が例外を投げた場合、status は EVAL_ERROR になり OK とは
    区別できること。2026-08-31以前は例外時に status="ok" を返しており、
    「一度も評価できなかった」が「目標達成と判定された」に化けていた
    (tool_session_archive.archive_chat_session の "Skipped: turns=" 未判定バグと
    同型、grillで検出して修正)。"""
    orig_get_tools = engine._get_tools_config
    orig_json_call = engine._gemini_json_call
    engine._get_tools_config = lambda: (
        [{"name": "tool_probe", "usage": "tool_probe()", "description": "probe"}], {})

    def _boom(*a, **kw):
        raise RuntimeError("Gemini budget exceeded")

    engine._gemini_json_call = _boom
    try:
        results = [{"step": 1, "task": "x", "tool": "tool_probe",
                    "success": True, "observation": "ok"}]
        critique = engine.critique_results("query", [], results)
        assert critique["status"] == engine.CriticStatus.EVAL_ERROR, critique
        assert critique["status"] != engine.CriticStatus.OK, critique
        assert "Gemini budget exceeded" in critique["feedback"], critique
    finally:
        engine._get_tools_config = orig_get_tools
        engine._gemini_json_call = orig_json_call
    print("✅ critique_results: Critic呼び出し失敗時は EVAL_ERROR (OKに丸め込まれない)")


def test_run_plan_and_execute_records_eval_error_not_ok_in_learning_db():
    """critique_results が EVAL_ERROR を返した実行は、learning_db の
    critique_status にも "eval_error" が記録され "ok" に化けないこと。
    (壊れると Agent_Performance.md の成功率が『Criticが一度も評価していない
    実行』込みで水増しされる — Category A の "水増し" バグと同型のリスク。)"""
    import contextlib
    import io as _io

    plan = [{"step": 1, "task": "検索", "tool": "tool_x"}]
    success_step = {"step": 1, "task": "検索", "tool": "tool_x",
                     "observation": "Success: ok", "success": True}

    orig = {name: getattr(engine, name) for name in
            ("generate_plan", "execute_plan", "critique_results",
             "log_execution", "_generate_final_summary")}
    logged_critiques = []
    engine.generate_plan = lambda q, session_context="": {
        "plan": list(plan), "expected_outcome": "x"}
    engine.execute_plan = lambda p, cb=None, initial_context=None: {
        "success": True, "results": [dict(success_step)], "final_output": ""}
    engine.critique_results = lambda *a: {
        "status": engine.CriticStatus.EVAL_ERROR,
        "feedback": "Critic evaluation failed (budget exceeded); results accepted without verification."}
    engine._generate_final_summary = lambda q, r: "## 📋 結論\n取得できました"
    engine.log_execution = lambda query, plan, results, critique, duration, parallel=None: (
        logged_critiques.append(critique["status"]))

    try:
        with contextlib.redirect_stdout(_io.StringIO()):
            result = engine.run_plan_and_execute("何かして")
        assert logged_critiques == [engine.CriticStatus.EVAL_ERROR], logged_critiques
        assert logged_critiques[0] != engine.CriticStatus.OK
        assert "結論" in result, result
    finally:
        for name, func in orig.items():
            setattr(engine, name, func)
    print("✅ run_plan_and_execute: EVAL_ERROR が learning_db に ok として紛れ込まない")


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
