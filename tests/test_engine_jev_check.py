# -*- coding: utf-8 -*-
"""
engine_plan_execute._jev_goal_check (Jev/TypeSafe AI クロスチェック) の回帰テスト。

このテストが証明すること:
  Jev クロスチェックは Critic (critique_results) の**追加**であって置き換えではない。
  Jev が成功しても失敗しても、run_plan_and_execute() の最終レポート・制御フローは
  一切変わらないこと (非破壊の回帰保証) と、log_execution() の outcome 計算は
  Jev 追加前と完全に同じロジックのまま (jev_probability/jev_disagreement は
  追加フィールドとして記録されるだけ) であることをこの1ファイルで確認する。

  engine.jev_noul を差し替える (tools.tool_jev.jev_noul ではない) — engine_plan_execute.py
  は `from tools.tool_jev import jev_noul` で値コピー束縛しているため、
  差し替えるべきは import 先の名前空間 (known-bug-patterns.md: from import は
  連動しない)。

実 Gemini / 実 Jev API は一切呼ばない。実 learning_db にも触れない。

実行: py tests/test_engine_jev_check.py
"""
import contextlib
import io as _io
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import engine_plan_execute as engine

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


_RESULTS = [
    {"step": 1, "tool": "tool_web_search", "success": True, "observation": "Success: found it"},
]


# ---------------------------------------------------------
# 1. _jev_goal_check の正常系・異常系
# ---------------------------------------------------------

def test_jev_goal_check_returns_probability():
    print("\n[1] _jev_goal_check: 正常系は probability を返す")
    orig = engine.jev_noul
    engine.jev_noul = lambda state, instructions: {"probability": 0.73}
    try:
        result = engine._jev_goal_check("目標", _RESULTS)
    finally:
        engine.jev_noul = orig
    check("probability がそのまま返る", result == 0.73, str(result))


def test_jev_goal_check_returns_none_on_exception():
    print("\n[2] _jev_goal_check: jev_noul が例外を投げても None (クラッシュしない)")

    def _boom(state, instructions):
        raise engine.JevAPIError("API down")

    orig = engine.jev_noul
    engine.jev_noul = _boom
    try:
        with contextlib.redirect_stdout(_io.StringIO()):
            result = engine._jev_goal_check("目標", _RESULTS)
    finally:
        engine.jev_noul = orig
    check("例外時は None", result is None, str(result))


def test_jev_goal_check_returns_none_when_unavailable():
    print("\n[3] _jev_goal_check: tools.tool_jev 不在 (jev_noul=None) でも None")
    orig = engine.jev_noul
    engine.jev_noul = None
    try:
        result = engine._jev_goal_check("目標", _RESULTS)
    finally:
        engine.jev_noul = orig
    check("未導入時は None", result is None, str(result))


def test_jev_goal_check_state_is_json_with_truncated_observation():
    print("\n[4] _jev_goal_check: state はJSON、observationは200文字で切られる")
    captured = {}

    def _spy(state, instructions):
        captured["state"] = state
        captured["instructions"] = instructions
        return {"probability": 0.5}

    orig = engine.jev_noul
    engine.jev_noul = _spy
    long_obs = "x" * 500
    try:
        engine._jev_goal_check("目標Q", [
            {"step": 1, "tool": "tool_x", "success": False, "observation": long_obs}
        ])
    finally:
        engine.jev_noul = orig

    parsed = json.loads(captured["state"])
    check("state はJSONとしてパースできる", isinstance(parsed, list), captured.get("state"))
    check("observation が200文字に切られる", len(parsed[0]["observation"]) == 200,
          str(len(parsed[0]["observation"])))
    check("instructions にユーザーの目標文字列が含まれる",
          "目標Q" in captured["instructions"], captured["instructions"])


# ---------------------------------------------------------
# 5. log_execution: jev_probability / jev_disagreement の記録
# ---------------------------------------------------------

def test_log_execution_records_jev_fields_without_changing_outcome():
    print("\n[5] log_execution: jev_probability/jev_disagreement を記録し、outcome計算は不変")

    saved = {}
    orig_load, orig_save = engine._load_learning_db, engine._save_learning_db
    engine._load_learning_db = lambda: {"executions": [], "seq": 0}
    engine._save_learning_db = lambda db: saved.update(db)

    def _log(results, critique):
        saved.clear()
        with contextlib.redirect_stdout(_io.StringIO()):
            engine.log_execution("q", [{"step": 1}], results, critique, 1.0)
        return saved["executions"][-1]

    try:
        success_results = [{"step": 1, "task": "t", "tool": "tool_ok",
                            "observation": "Success: ok", "success": True}]

        # critique に _jev_probability が無い (Jev未導入・失敗時と同じ形) → 従来どおり
        entry = _log(success_results, {"status": "ok"})
        check("critiqueにJevキーが無くてもoutcomeは従来通りsuccess",
              entry["outcome"] == "success", str(entry))
        check("jev_probability は None として記録される",
              entry["jev_probability"] is None, str(entry))
        check("jev_disagreement は False", entry["jev_disagreement"] is False, str(entry))

        # Critic は ok、Jev は強く disagree (低確率) → 記録はされるが outcome は変わらない
        entry = _log(success_results, {"status": "ok", "_jev_probability": 0.1})
        check("Jevが低確率でも outcome は success のまま (診断専用、判定に使わない)",
              entry["outcome"] == "success", str(entry))
        check("jev_probability が記録される", entry["jev_probability"] == 0.1, str(entry))
        check("jev_disagreement が True になる (status=ok かつ probability<0.4)",
              entry["jev_disagreement"] is True, str(entry))

        # Critic は ok、Jev も同意 (高確率) → disagreement は False
        entry = _log(success_results, {"status": "ok", "_jev_probability": 0.9})
        check("Jevが同意していれば jev_disagreement は False",
              entry["jev_disagreement"] is False, str(entry))

        # 失敗ケースの outcome 計算が Jev 追加前と同じであることも確認
        fail_results = [{"step": 1, "task": "t", "tool": "tool_x",
                         "observation": "Error: boom", "success": False}]
        entry = _log(fail_results, {"status": "ok", "_jev_probability": 0.05})
        check("失敗ステップは success の判定ロジック不変 (outcome=failed)",
              entry["outcome"] == "failed", str(entry))
    finally:
        engine._load_learning_db, engine._save_learning_db = orig_load, orig_save


# ---------------------------------------------------------
# 6. run_plan_and_execute: Jev の成功/失敗で最終出力が完全に不変
# ---------------------------------------------------------

def test_run_plan_and_execute_output_unchanged_regardless_of_jev():
    print("\n[6] run_plan_and_execute: 最終出力は Jev の成否に関わらず完全に同一")

    plan = [{"step": 1, "task": "検索", "tool": "tool_x"}]
    ok_result = {"step": 1, "task": "検索", "tool": "tool_x",
                "observation": "Success: found", "success": True}

    orig = {name: getattr(engine, name) for name in
            ("generate_plan", "execute_plan", "critique_results",
             "log_execution", "jev_noul", "_generate_final_summary")}
    try:
        engine.generate_plan = lambda q, session_context="": {
            "plan": list(plan), "expected_outcome": "x"}
        engine.execute_plan = lambda p, cb=None, initial_context=None: {
            "success": True, "results": [dict(ok_result)], "final_output": ""}
        engine.critique_results = lambda *a: {"status": "ok", "feedback": "良い"}
        # _final_report は成功時に _generate_final_summary (実Gemini呼び出し) を
        # 使う。ここを差し替えないと本テストが実APIへ到達してしまう
        # (known-bug-patterns.md Category C: 実行系を回した結果として間接的に
        # 呼ぶものは見落としやすい — test_result_icons.py と同じ注意点)。
        engine._generate_final_summary = lambda q, r: "## 📋 結論\n見つかりました"
        logged = []
        engine.log_execution = lambda *a, **kw: logged.append(dict(a[3]))

        # ケースA: Jev が正常に高確率を返す
        engine.jev_noul = lambda state, instructions: {"probability": 0.95}
        with contextlib.redirect_stdout(_io.StringIO()):
            result_with_jev = engine.run_plan_and_execute("何かを検索して")

        # ケースB: Jev がエラーで落ちる
        engine.jev_noul = lambda state, instructions: (_ for _ in ()).throw(
            engine.JevAPIError("down"))
        with contextlib.redirect_stdout(_io.StringIO()):
            result_jev_down = engine.run_plan_and_execute("何かを検索して")

        # ケースC: Jev 自体が未導入
        engine.jev_noul = None
        with contextlib.redirect_stdout(_io.StringIO()):
            result_no_jev = engine.run_plan_and_execute("何かを検索して")

        check("Jev成功/失敗/未導入で最終レポート文字列が完全に同一",
              result_with_jev == result_jev_down == result_no_jev,
              f"A={result_with_jev[:80]!r} B={result_jev_down[:80]!r} C={result_no_jev[:80]!r}")
        check("log_execution は3回とも呼ばれる (Jevの成否でスキップされない)",
              len(logged) == 3, str(len(logged)))
        check("critique dict に _jev_probability キーが (診断目的で) 追加されている",
              "_jev_probability" in logged[0], str(logged[0]))
    finally:
        for name, func in orig.items():
            setattr(engine, name, func)


if __name__ == "__main__":
    print("=== test_engine_jev_check ===")
    test_jev_goal_check_returns_probability()
    test_jev_goal_check_returns_none_on_exception()
    test_jev_goal_check_returns_none_when_unavailable()
    test_jev_goal_check_state_is_json_with_truncated_observation()
    test_log_execution_records_jev_fields_without_changing_outcome()
    test_run_plan_and_execute_output_unchanged_regardless_of_jev()

    print(f"\n=== 結果: {len(_PASS)} passed / {len(_FAIL)} failed ===")
    if _FAIL:
        for f in _FAIL:
            print(f"  - {f}")
        sys.exit(1)
