# -*- coding: utf-8 -*-
"""
Phase C1: fan-out (依存の無い連続ステップの同時実行) のユニットテスト

実行: py tests/test_parallel_execute.py

Gemini も Ollama も呼ばない: ツールはスタブに差し替え、引数は args_json で
計画時に確定させて _resolve_tool_args (Gemini) の経路に入らないようにしている。
"""
import os
import sys
import ast
import json
import time
import threading
import contextlib
import io as _io

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import engine_plan_execute as engine

_FAILED = []


def check(label: str, ok: bool, detail: str = ""):
    if ok:
        print(f"  ✅ {label}")
    else:
        _FAILED.append(label)
        print(f"  ❌ {label} — {detail}")


# ---------------------------------------------------------
# スタブ
# ---------------------------------------------------------

def _sleepy(seconds: float, ret: str = "Success: done", intervals: list = None):
    """指定秒だけ眠るツールのスタブ。

    intervals を渡すと (start, end) を追記する。**同時実行の判定に壁時計の
    しきい値を使わないため** — 詳細は test_wave_actually_runs_in_parallel。
    """
    def _tool(query: str = "") -> str:
        started = time.monotonic()
        time.sleep(seconds)
        if intervals is not None:
            intervals.append((started, time.monotonic()))
        return ret
    return _tool


def _peak_concurrency(intervals):
    """重なった区間の最大本数。逐次実行なら必ず 1 になる。"""
    events = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals])
    peak = cur = 0
    for _t, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


def _step(n: int, tool: str, args_json: str = '{"query": "x"}', hint: str = ""):
    item = {"step": n, "task": f"task{n}", "tool": tool, "args_json": args_json}
    if hint:
        item["args_hint"] = hint
    return item


# 「並列可」と宣言されたスタブ群 (実際のレジストリとは切り離してテストする)
_SAFE = frozenset({"tool_a", "tool_b", "tool_c"})


def _fake_map(**overrides):
    base = {
        "tool_a": _sleepy(0),
        "tool_b": _sleepy(0),
        "tool_c": _sleepy(0),
        "tool_write": _sleepy(0),   # parallel_safe ではない
    }
    base.update(overrides)
    return base


@contextlib.contextmanager
def _patched(tools_map, parallel_safe=_SAFE):
    """engine のツール解決・観測ログ・並列可否宣言を差し替える。

    parallel_safe まで差し替えるのは、スタブのツール名 (tool_a 等) が実際の
    レジストリに存在しないため。ここを差し替えないと全ステップが単独ウェーブに
    なり、「並列になっていない」ことに気付かないままテストが緑になる。
    実レジストリとの結線は [6] が別途見る。
    """
    orig_config, orig_observe = engine._get_tools_config, engine.observe
    orig_safe = engine.get_parallel_safe_tools
    engine._get_tools_config = lambda: ([], dict(tools_map))
    engine.observe = lambda name, func, caller, **kw: func(**kw)
    engine.get_parallel_safe_tools = lambda: parallel_safe
    try:
        yield
    finally:
        engine._get_tools_config = orig_config
        engine.observe = orig_observe
        engine.get_parallel_safe_tools = orig_safe


# ---------------------------------------------------------
# [1] ウェーブ分割 (純関数)
# ---------------------------------------------------------

def test_waves_group_consecutive_ready_steps():
    print("\n[1] _plan_waves: 連続する fan-out 可能ステップを束ねる")
    tools = _fake_map()
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]
    waves = engine._plan_waves(plan, tools, parallel_safe=_SAFE, enabled=True)
    check("3連続 → 1ウェーブ3件", len(waves) == 1 and len(waves[0]) == 3,
          str([[i["tool"] for i in w] for w in waves]))


def test_waves_split_on_write_tool():
    print("\n[2] _plan_waves: 副作用ツールでウェーブが割れる")
    tools = _fake_map()
    plan = [_step(1, "tool_a"), _step(2, "tool_write"), _step(3, "tool_b")]
    waves = engine._plan_waves(plan, tools, parallel_safe=_SAFE, enabled=True)
    shapes = [[i["tool"] for i in w] for w in waves]
    check("3ウェーブに割れる", shapes == [["tool_a"], ["tool_write"], ["tool_b"]],
          str(shapes))


def test_waves_split_on_context_dependent_step():
    print("\n[3] _plan_waves: args_json が無いステップは単独ウェーブ")
    tools = _fake_map()
    plan = [
        _step(1, "tool_a"),
        # args_json 空 = 「前ステップの結果に依存する」の宣言 (generate_plan の契約)
        {"step": 2, "task": "t2", "tool": "tool_b", "args_json": "",
         "args_hint": "前の結果を使う"},
        _step(3, "tool_c"),
    ]
    waves = engine._plan_waves(plan, tools, parallel_safe=_SAFE, enabled=True)
    shapes = [[i["tool"] for i in w] for w in waves]
    check("依存ステップで割れる", shapes == [["tool_a"], ["tool_b"], ["tool_c"]],
          str(shapes))


def test_waves_reject_unknown_tool_and_bad_args():
    print("\n[4] _plan_waves: 未知ツール / シグネチャに合わない引数は並列化しない")
    tools = _fake_map()
    plan = [_step(1, "tool_missing"), _step(2, "tool_a")]
    with contextlib.redirect_stdout(_io.StringIO()):
        waves = engine._plan_waves(plan, tools, parallel_safe=_SAFE | {"tool_missing"},
                                   enabled=True)
    check("未知ツールは束ねない", [len(w) for w in waves] == [1, 1], str(waves))

    # 必須引数が欠けている → 実行時に Gemini 再解決へ落ちる = 依存が発生しうる
    def _needs_two(query, extra):
        return "ok"

    tools2 = _fake_map(tool_a=_needs_two)
    plan2 = [_step(1, "tool_a"), _step(2, "tool_b")]
    with contextlib.redirect_stdout(_io.StringIO()):
        waves2 = engine._plan_waves(plan2, tools2, parallel_safe=_SAFE, enabled=True)
    check("必須引数が欠けたステップは束ねない",
          [len(w) for w in waves2] == [1, 1], str(waves2))


def test_kill_switch_makes_every_wave_singleton():
    print("\n[5] AGENT_PARALLEL=0 で全ステップが単独ウェーブ")
    tools = _fake_map()
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]
    waves = engine._plan_waves(plan, tools, parallel_safe=_SAFE, enabled=False)
    check("全て1要素", [len(w) for w in waves] == [1, 1, 1], str(waves))
    check("順序は計画順", [w[0]["step"] for w in waves] == [1, 2, 3], str(waves))


def test_waves_use_registry_by_default():
    """既定引数で呼んだとき、実際のレジストリの parallel_safe を見ていること。

    テスト用の集合を注入したケースしか無いと、本番の結線が外れても気付けない。
    """
    print("\n[6] 既定ではレジストリの parallel_safe を使う")
    from lib.tool_registry import get_parallel_safe_tools
    safe = get_parallel_safe_tools()
    check("tool_web_search が並列可", "tool_web_search" in safe, str(sorted(safe)))
    check("tool_create_note は並列不可", "tool_create_note" not in safe, str(sorted(safe)))

    _, real_map = engine._get_tools_config()
    plan = [_step(1, "tool_web_search"), _step(2, "tool_vault_hybrid_search")]
    with contextlib.redirect_stdout(_io.StringIO()):
        waves = engine._plan_waves(plan, real_map)   # parallel_safe を渡さない
    check("実レジストリで web_search + vault_search が1ウェーブ",
          len(waves) == 1 and len(waves[0]) == 2,
          str([[i["tool"] for i in w] for w in waves]))


# ---------------------------------------------------------
# [7] 実行: 順序と並列性
# ---------------------------------------------------------

def test_results_keep_plan_order_regardless_of_completion():
    print("\n[7] 完了順が逆でも results は計画順")
    tools = _fake_map(
        tool_a=_sleepy(0.30, "Success: a"),
        tool_b=_sleepy(0.15, "Success: b"),
        tool_c=_sleepy(0.01, "Success: c"),
    )
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]
    with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
        res = engine.execute_plan(plan)
    steps = [r["step"] for r in res["results"]]
    obs = [str(r["observation"]) for r in res["results"]]
    check("step が 1,2,3 の順", steps == [1, 2, 3], str(steps))
    check("observation が計画順に対応", obs == ["Success: a", "Success: b", "Success: c"],
          str(obs))


def test_wave_actually_runs_in_parallel():
    """同時実行を **壁時計のしきい値ではなく実測の重なり** で判定する。

    初版は `wall < 1.0` (0.5秒 x 3本 = 逐次なら1.5秒) だった。開発機では
    0.5秒台なので余裕があるが、2026-07-31 にスイープ自体が8並列になってから
    **CI で 1.42秒** を記録して赤くなった (master 30621803361)。
    ツール3本が同時に走っていても、8プロセスがコアを取り合う runner では
    スレッドの起動と復帰が遅れて壁時計が伸びる。
    「開発機の速さ」を暗黙の前提にした判定はこのリポジトリが繰り返し潰してきた
    下駄そのもので、実際 tests/test_sweep_runner.py も同じ形で flaky だった。

    ツール側で (start, end) を記録し、**重なった本数 (peak concurrency)** で見る。
    逐次に戻れば peak は必ず 1 になるので、3本の fan-out が壊れれば確実に赤くなる。
    壁時計は参考値としてメッセージに残す。
    """
    print("\n[8] 本当に同時実行されている (区間の重なり)")
    seen = []
    tools = _fake_map(
        tool_a=_sleepy(0.5, intervals=seen),
        tool_b=_sleepy(0.5, intervals=seen),
        tool_c=_sleepy(0.5, intervals=seen),
    )
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]
    with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
        t0 = time.time()
        res = engine.execute_plan(plan)
        wall = time.time() - t0

    peak = _peak_concurrency(seen)
    check(f"3本が同時に走っている (peak={peak} / 壁時計 {wall:.2f}秒)", peak == 3,
          f"peak={peak} — 逐次なら1。fan-out が効いていない")
    check("全ステップ成功", res["success"], str(res["results"]))
    check("parallel.waves が1", res["parallel"]["waves"] == 1, str(res["parallel"]))
    # 「正」と名乗っている以上、しきい値も正であること。0.5 を要求すると
    # これも壁時計依存になる (saved_seconds は所要時間の総和 - 実測 wall)。
    check("saved_seconds が正", res["parallel"]["saved_seconds"] > 0,
          str(res["parallel"]))


def test_kill_switch_serializes_execution():
    print("\n[9] AGENT_PARALLEL=0 なら実行も逐次に戻る")
    saved = os.environ.get("AGENT_PARALLEL")
    os.environ["AGENT_PARALLEL"] = "0"
    try:
        seen = []
        tools = _fake_map(
            tool_a=_sleepy(0.2, intervals=seen),
            tool_b=_sleepy(0.2, intervals=seen),
            tool_c=_sleepy(0.2, intervals=seen),
        )
        plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]
        with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
            t0 = time.time()
            res = engine.execute_plan(plan)
            wall = time.time() - t0
        # `wall >= 0.55` だけでは「逐次である」証明にならない。混雑した runner では
        # **並列実行でも** 0.55秒を軽く超えるため (実測: 0.5秒 x 3本の並列が 1.42秒)、
        # fan-out が誤って有効なままでもこの assert は緑になりうる。
        # 区間の重なりで見れば逐次は必ず peak==1 になり、方向が確定する。
        # これは [8] の peak==3 の逆方向の証明でもある。
        peak = _peak_concurrency(seen)
        check(f"1本ずつ順に走っている (peak={peak} / 壁時計 {wall:.2f}秒)", peak == 1,
              f"peak={peak} — AGENT_PARALLEL=0 なのに同時実行されている")
        check("parallel.waves が0", res["parallel"]["waves"] == 0, str(res["parallel"]))
        check("結果は同じ", [r["step"] for r in res["results"]] == [1, 2, 3],
              str(res["results"]))
    finally:
        if saved is None:
            os.environ.pop("AGENT_PARALLEL", None)
        else:
            os.environ["AGENT_PARALLEL"] = saved


# ---------------------------------------------------------
# [10] 失敗の扱い
# ---------------------------------------------------------

def test_failure_in_wave_keeps_sibling_results():
    print("\n[10] ウェーブ内の失敗で同ウェーブの成功を捨てない")
    tools = _fake_map(
        tool_a=_sleepy(0.02, "Success: a"),
        tool_b=_sleepy(0.02, "Error: 意図的な失敗"),
        tool_c=_sleepy(0.02, "Success: c"),
    )
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c"),
            _step(4, "tool_write")]
    with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
        res = engine.execute_plan(plan)

    steps = [r["step"] for r in res["results"]]
    check("同ウェーブの3件が全て残る", steps == [1, 2, 3], str(steps))
    check("失敗ステップは success=False",
          res["results"][1]["success"] is False, str(res["results"][1]))
    check("成功ステップは残る",
          res["results"][0]["success"] and res["results"][2]["success"],
          str(res["results"]))
    check("ウェーブ境界で止まる (Step 4 は実行されない)", 4 not in steps, str(steps))
    check("execute_plan 全体は失敗", not res["success"], str(res["success"]))

    # 部分失敗として記録されること (⚠️ Mission Partial の前提)
    from engine_plan_execute import log_execution
    outcome_all = all(r.get("success") for r in res["results"])
    outcome_any = any(r.get("success") for r in res["results"])
    check("outcome は partial 相当 (成功と失敗が混在)",
          (not outcome_all) and outcome_any, str([r["success"] for r in res["results"]]))


def test_worker_exception_does_not_kill_the_wave():
    print("\n[11] ワーカーの例外がウェーブごと落とさない")

    def _boom(query: str = ""):
        raise RuntimeError("スタブが例外を投げた")

    tools = _fake_map(tool_b=_boom)
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]
    with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
        res = engine.execute_plan(plan)
    steps = [r["step"] for r in res["results"]]
    check("3件とも結果が残る", steps == [1, 2, 3], str(steps))
    check("例外ステップが failed", not res["results"][1]["success"],
          str(res["results"][1]))
    check("例外本文が observation に残る",
          "スタブが例外を投げた" in str(res["results"][1]["observation"]),
          str(res["results"][1]["observation"]))


# ---------------------------------------------------------
# [12] 進捗コールバック
# ---------------------------------------------------------

def test_callback_emitted_once_per_wave():
    print("\n[12] コールバックはウェーブ1本につき running/done 各1回")
    tools = _fake_map(
        tool_a=_sleepy(0.05), tool_b=_sleepy(0.05), tool_c=_sleepy(0.05),
    )
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]

    events = []
    lock = threading.Lock()

    def cb(data):
        with lock:
            events.append(data)

    with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
        engine.execute_plan(plan, step_callback=cb)

    exec_events = [e for e in events if e.get("phase") == "execute"]
    check("イベントは2件 (running + done)", len(exec_events) == 2,
          str([(e["status"], len(e.get("wave", []))) for e in exec_events]))
    check("running が1件",
          sum(1 for e in exec_events if e["status"] == "running") == 1, str(exec_events))
    check("done が1件",
          sum(1 for e in exec_events if e["status"] == "done") == 1, str(exec_events))
    check("wave キーに3件入る",
          all(len(e.get("wave", [])) == 3 for e in exec_events), str(exec_events))
    check("後方互換: step/total が残っている",
          all("step" in e and "total" in e for e in exec_events), str(exec_events))


def test_wave_error_event_carries_the_failed_observation():
    """status="error" のイベントが成功ステップの本文を載せていないこと。

    常に results[0] を載せると「失敗と言いながら成功の中身を見せる」になる。
    今の描画は observation を使わないが、食い違ったデータを流すこと自体が
    「表示が実態と食い違う」バグの種。
    """
    print("\n[18] エラーイベントは失敗したステップの observation を載せる")
    tools = _fake_map(
        tool_a=_sleepy(0.01, "Success: 1件目は成功"),
        tool_b=_sleepy(0.01, "Error: 2件目が失敗"),
        tool_c=_sleepy(0.01, "Success: 3件目は成功"),
    )
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]
    events = []
    with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
        engine.execute_plan(plan, step_callback=events.append)
    done = [e for e in events
            if e.get("phase") == "execute" and e.get("status") in ("done", "error")][0]
    check("status は error", done["status"] == "error", str(done))
    check("observation が失敗ステップのもの",
          "2件目が失敗" in str(done.get("observation")), str(done.get("observation")))
    check("failed_steps に 2 が入る", done.get("failed_steps") == [2],
          str(done.get("failed_steps")))


def test_broken_worker_contract_does_not_lose_the_wave():
    """_run_step が契約を破って例外を投げても、ウェーブの他の結果を失わない。

    future.result() をそのまま呼ぶと executor を抜けた時点で execute_plan ごと
    落ち、既に完了している他ステップの結果まで消える。
    """
    print("\n[19] ワーカーが結果を返せなくてもウェーブを道連れにしない")
    tools = _fake_map()
    plan = [_step(1, "tool_a"), _step(2, "tool_b"), _step(3, "tool_c")]

    orig = engine._run_step
    calls = {"n": 0}

    def _flaky(item, tools_map, context, fallback_step=1):
        if item.get("tool") == "tool_b":
            raise RuntimeError("_run_step の契約が破れた")
        return orig(item, tools_map, context, fallback_step)

    engine._run_step = _flaky
    try:
        with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
            res = engine.execute_plan(plan)
    finally:
        engine._run_step = orig

    steps = [r["step"] for r in res["results"]]
    check("3件とも結果が残る", steps == [1, 2, 3], str(steps))
    check("壊れたステップは failed", not res["results"][1]["success"],
          str(res["results"][1]))
    check("例外本文が残る", "契約が破れた" in str(res["results"][1]["observation"]),
          str(res["results"][1]["observation"]))
    check("他2件は成功のまま",
          res["results"][0]["success"] and res["results"][2]["success"],
          str(res["results"]))


def test_single_step_events_keep_legacy_shape():
    print("\n[13] 単独ステップのイベント形は従来どおり")
    tools = _fake_map()
    plan = [_step(1, "tool_write")]
    events = []
    with _patched(tools), contextlib.redirect_stdout(_io.StringIO()):
        engine.execute_plan(plan, step_callback=events.append)
    exec_events = [e for e in events if e.get("phase") == "execute"]
    check("running + done の2件", len(exec_events) == 2, str(exec_events))
    for e in exec_events:
        check(f"step/total/task/tool が入っている ({e['status']})",
              all(k in e for k in ("step", "total", "task", "tool")), str(e))
        check(f"wave の長さは1 ({e['status']})", len(e.get("wave", [])) == 1, str(e))


def test_render_progress_shared_by_both_entrypoints():
    print("\n[14] 進捗描画を /agent と /chat agent が共用する")
    from commands.bot_commands_agent import render_progress

    single = {"phase": "execute", "status": "running", "step": 1, "total": 3,
              "task": "検索する", "tool": "tool_web_search",
              "wave": [{"step": 1, "task": "検索する", "tool": "tool_web_search"}]}
    text = render_progress(single)
    check("単独ステップは従来の文言", text.startswith("▶️ **実行中:** Step 1/3"), str(text))

    wave = {"phase": "execute", "status": "running", "step": 1, "total": 4,
            "task": "a", "tool": "tool_a",
            "wave": [{"step": 1, "task": "a", "tool": "tool_a"},
                     {"step": 2, "task": "b", "tool": "tool_b"}]}
    text = render_progress(wave)
    check("ウェーブは1行に集約", "2件を並列実行中" in text, str(text))

    done_failed = dict(wave, status="error", failed_steps=[2])
    text = render_progress(done_failed)
    check("失敗を含むウェーブに ✅ を出さない", not text.startswith("✅"), str(text))
    check("失敗したステップ番号を出す", "Step 2" in text, str(text))

    done_ok = dict(wave, status="done", failed_steps=[])
    check("全成功なら ✅", render_progress(done_ok).startswith("✅"),
          str(render_progress(done_ok)))
    check("未知の phase は None", render_progress({"phase": "zzz"}) is None, "")

    # 知らない status を成功に丸めないこと (将来 status を増やしたときに
    # 未対応の状態が ✅ で出るのがこのプロジェクトの定番バグ)
    for unknown in ("queued", "", "skipped"):
        check(f"未知の status '{unknown}' は None (ウェーブ)",
              render_progress(dict(wave, status=unknown)) is None, "")
        check(f"未知の status '{unknown}' は None (単独)",
              render_progress(dict(single, status=unknown)) is None, "")

    # bot_discord.py が自前で描いていないこと (入口ごとの写経の再発防止)
    with open(os.path.join(_PROJECT_ROOT, "bot_discord.py"), encoding="utf-8") as f:
        src = f.read()
    check("bot_discord.py は render_progress を呼ぶ",
          "render_progress(step_data)" in src, "")
    check("bot_discord.py に進捗文言のコピーが残っていない",
          "**実行中:** Step" not in src, "")


# ---------------------------------------------------------
# [15] 静的検査
# ---------------------------------------------------------

_POOL_NAME = "ThreadPoolExecutor"


def _uses_threadpool(src):
    """ast で「実際に使っている」ものだけを拾う。

    文字列走査だと **この検査を避けた理由を書いた docstring / コメント自体**が
    違反として報告される (実際 scripts/run_test_sweep.py で踏んだ)。
    CLAUDE.md の「検査は文字列走査ではなく ast で書く」と同じ話で、
    そうなると「教訓を書けなくする検査」になり、外される方向に力が働く。

    構文エラーのファイルは判定できないので False を返す (構文は
    verify_project の [2] の担当)。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == _POOL_NAME:
            return True
        if isinstance(node, ast.Attribute) and node.attr == _POOL_NAME:
            return True
        if isinstance(node, ast.ImportFrom):
            if any(a.name == _POOL_NAME for a in node.names):
                return True
    return False


def test_threadpool_usage_is_confined():
    print("\n[15] ThreadPoolExecutor の使用箇所を限定する")

    # 検出器が飾りになっていないことの両方向確認。「違反0件でした」は
    # 検出器が壊れていても同じ出力になる。
    check("実際に使っていれば拾う",
          _uses_threadpool("from concurrent.futures import ThreadPoolExecutor\n"
                           "ThreadPoolExecutor(max_workers=2)\n"), "")
    check("属性参照でも拾う",
          _uses_threadpool("import concurrent.futures\n"
                           "concurrent.futures.ThreadPoolExecutor(max_workers=2)\n"), "")
    check("名前に言及しただけの docstring / コメントは拾わない",
          not _uses_threadpool('"""ThreadPoolExecutor を使わない理由"""\n'
                               "# ThreadPoolExecutor は使わない\n"
                               'x = "ThreadPoolExecutor"\n'), "")

    allowed = {
        os.path.join("engine_plan_execute.py"),
        os.path.join("tools", "tool_curator.py"),
        os.path.join("tools", "tool_xtrend.py"),
    }
    # 走査対象を自前コードに限定する (ホワイトリスト)。ブラックリストにすると
    # models/hf_pkg のようなベンダリング済み依存を毎回足すことになる。
    scan_roots = [_PROJECT_ROOT] + [
        os.path.join(_PROJECT_ROOT, d) for d in ("lib", "tools", "commands", "scripts")
    ]
    offenders = []
    seen = set()
    for scan_root in scan_roots:
        recurse = scan_root != _PROJECT_ROOT
        for root, dirs, files in os.walk(scan_root):
            if not recurse:
                dirs[:] = []          # プロジェクト直下の .py だけ見る
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, _PROJECT_ROOT)
                if rel in allowed or rel in seen:
                    continue
                seen.add(rel)
                try:
                    with open(path, encoding="utf-8") as f:
                        src = f.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if _uses_threadpool(src):
                    offenders.append(rel)
    check("許可された3ファイル以外で使っていない", not offenders, str(offenders))
    check("走査が空振りしていない (engine を実際に読んでいる)",
          "engine_plan_execute.py" in seen or "engine_plan_execute.py" in allowed,
          str(len(seen)))


def test_run_step_does_not_call_step_callback():
    """ワーカーから進捗通知を飛ばしていないこと。

    飛ばすと3本同時のとき edit が6回ほぼ同時に走り Discord のレート制限に当たる。
    実行時テスト ([12]) は「私が書いた計画」しか守れないので、静的にも見る。
    """
    print("\n[16] _run_step は step_callback を呼ばない")
    with open(os.path.join(_PROJECT_ROOT, "engine_plan_execute.py"),
              encoding="utf-8") as f:
        tree = ast.parse(f.read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_run_step")
    names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    check("step_callback を参照していない", "step_callback" not in names,
          str(sorted(names)))
    check("シグネチャに step_callback が無い",
          "step_callback" not in [a.arg for a in func.args.args],
          str([a.arg for a in func.args.args]))


# ---------------------------------------------------------
# [17] 学習DBへの記録
# ---------------------------------------------------------

def test_log_execution_records_parallel_stats():
    print("\n[17] log_execution が fan-out の実測を追加のみで記録する")
    import tempfile
    import engine_plan_execute as eng

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "learning_db.json")
        orig = eng.LEARNING_DB_PATH
        eng.LEARNING_DB_PATH = db_path
        try:
            results = [{"step": 1, "task": "t", "tool": "tool_a",
                        "observation": "Success:", "success": True}]
            with contextlib.redirect_stdout(_io.StringIO()):
                eng.log_execution("q", [], results, {"status": "ok"}, 1.0,
                                  parallel={"waves": 1, "saved_seconds": 3.25})
                eng.log_execution("q2", [], results, {"status": "ok"}, 1.0)
                eng.log_execution("q3", [], results, {"status": "ok"}, 1.0,
                                  parallel={"waves": 0, "saved_seconds": 0.0})
            with open(db_path, encoding="utf-8") as f:
                execs = json.load(f)["executions"]
            check("並列したら記録される",
                  execs[0].get("parallel") == {"waves": 1, "saved_seconds": 3.25},
                  str(execs[0].get("parallel")))
            check("省略時はキーごと無い (未計測と0秒を区別)",
                  "parallel" not in execs[1], str(execs[1]))
            check("waves=0 でもキーを足さない", "parallel" not in execs[2], str(execs[2]))
        finally:
            eng.LEARNING_DB_PATH = orig


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("\n" + "=" * 55)
    print(f"PASS: {sum(1 for _ in tests)} 関数 / FAIL: {len(_FAILED)} チェック")
    if _FAILED:
        for f in _FAILED:
            print(f"  ❌ {f}")
        sys.exit(1)
