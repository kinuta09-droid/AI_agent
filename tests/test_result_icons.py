# -*- coding: utf-8 -*-
"""「失敗を ✅ で表示しない」ことの回帰テスト

このテストが存在する理由:
  ARMS のツール層は例外を投げずに "Error: ..." という文字列を返す規約になっている。
  そのため bot 層が見出しに "✅" をハードコードすると、失敗がそのまま
  成功として Discord に表示され、ユーザーが壊れていることに気付けない。
  2026-07-26 に /curate だけ _result_icon() で直したが、
  /agent の "Mission Complete" と深掘りボタンの "深掘り完了" が同じ形で残っていた。

  同族の表示箇所をこの1ファイルに集約し、次に増えたときも
  ここに足せば守れるようにしておく。

実行: py tests/test_result_icons.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.bot_errors import log_command_error, is_failure_result, FAILURE_PREFIXES

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _error_string(prefix: str = "Error") -> str:
    """log_command_error() が実際に返す形式を、実際に例外を投げて得る。"""
    import contextlib
    import io as _io
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            raise RuntimeError("boom")
        except RuntimeError as e:
            return log_command_error("/test", e, prefix)


# ---------------------------------------------------------
# 1. 共有判定
# ---------------------------------------------------------

def test_is_failure_result():
    print("\n[1] lib.bot_errors.is_failure_result")

    check("ツール層の 'Error: ' を拾う",
          is_failure_result("Error: トピックが空です。"), "")
    check("log_command_error の 'Error (型名): ' を拾う",
          is_failure_result(_error_string()), _error_string())
    check("正常結果を失敗と誤判定しない",
          not is_failure_result("Errorハンドリングを調査しました"), "")
    check("成功文字列は失敗ではない",
          not is_failure_result("Success: Created [[x]]"), "")
    check("追加接頭辞を受け取れる",
          is_failure_result("タスク失敗: 中断", ("タスク失敗:",)), "")
    check("str 以外は失敗としない", not is_failure_result(None), "")

    # tools/ を実際に洗って出てきた失敗形。接頭辞 tuple では拾えず、
    # engine 側が success として learning_db に記録していた分 (2026-07-27)。
    tool_failures = [
        "Error creating note: [Errno 13] Permission denied",   # tool_obsidian
        "Error searching web: read timeout",                   # tool_web
        "Error fetching URL: 404 Client Error",                # tool_web
        "Error fetching BTC/USDT data: rate limit",            # tool_crypto
        "Error during revision: quota exceeded",               # tool_client_review
        "[30_Main] Error merging reports: disk full",          # tool_obsidian (先頭に [タグ])
        "[ERROR] Ollama not running. Start with: ollama serve",  # tool_gmail/vault_index
        "[Error: Obsidian note 'x' could not be read]\n",      # tool_client_review
        "[xtrend error] twitterAPI.io 401",                    # lib/tool_registry
        "[future_search error] Tavily timeout",                # lib/tool_registry
        "[browser_use error] playwright not installed",        # lib/tool_registry
        "[xtrend_prompt error] boom",                          # lib/tool_registry
        "Error: Unknown tool 'tool_nope'",                     # engine 側の戻り値
    ]
    for msg in tool_failures:
        check(f"実在の失敗形を拾う ({msg[:34]}…)", is_failure_result(msg), msg[:60])

    # 誤検知させてはいけない正常な戻り値 (同じ形をした成功系)
    tool_successes = [
        "Errorハンドリングを調査しました",          # 日本語が直後に続く
        "Errors は複数形なので失敗ではない",         # 英字が直後に続く
        "[30_Main] No files to merge.",             # 同じ [タグ] 形式の正常系
        "[xtrend] 12件の機能を分析しました。",       # lazy wrapper の成功系
        "Success: Updated [[30_Main/x]]",
    ]
    for msg in tool_successes:
        check(f"正常な戻り値を誤検知しない ({msg[:30]}…)",
              not is_failure_result(msg), msg[:60])


# ---------------------------------------------------------
# 2. /agent の Mission 見出し
# ---------------------------------------------------------

def test_mission_header():
    print("\n[2] /agent の見出しが失敗を ✅ で出さない")

    from commands.bot_commands_agent import mission_header as _mission_header
    from commands.bot_commands_agent import _FAILURE_PREFIXES

    check("共有の接頭辞を含む",
          all(p in _FAILURE_PREFIXES for p in FAILURE_PREFIXES), str(_FAILURE_PREFIXES))

    failures = [
        "Error: ツールが見つかりません",
        _error_string(),
        "計画生成に失敗しました。",
        "計画を生成できませんでした",
        "タスク失敗: ステップ2で中断",
    ]
    for msg in failures:
        check(f"失敗 → ❌ ({msg[:24]}…)",
              _mission_header(msg).startswith("❌"), _mission_header(msg))

    ok = "## 📋 結論\nノートを作成しました [[30_Main/x]]"
    check("成功 → ✅", _mission_header(ok) == "✅ **Mission Complete**", _mission_header(ok))
    check("Error で始まる正常本文を誤判定しない",
          _mission_header("Errorハンドリングの調査結果です").startswith("✅"), "")


# ---------------------------------------------------------
# 3. 深掘りボタンの完了通知
# ---------------------------------------------------------

def test_dive_result_header():
    print("\n[3] 深掘りボタンの見出しが失敗を ✅ で出さない")

    from commands.bot_views import _dive_result_header

    ok = _dive_result_header("AGI", "# レポート\n本文" * 50)
    check("成功 → ✅", ok.startswith("✅ 深掘り完了"), ok)

    ng = _dive_result_header("AGI", "Error: Tavily検索でソースが取得できませんでした。")
    check("ツールのエラー文字列 → ❌", ng.startswith("❌ 深掘り失敗"), ng)

    # 例外経路は log_command_error の user_prefix ("❌ …") で始まるため
    # 接頭辞判定には載らない。raised フラグで明示的に落とす契約。
    raised = _dive_result_header(
        "AGI", _error_string("❌ 深掘りリサーチでエラーが発生しました"), True
    )
    check("例外経路 → ❌", raised.startswith("❌ 深掘り失敗"), raised)
    check("トピック名は見出しに残る", "AGI" in ng, ng)


# ---------------------------------------------------------
# 4. ツール側の実際の戻り値と噛み合っているか
# ---------------------------------------------------------

def test_future_search_error_contract():
    """run_future_search の早期 return が失敗判定に載ることを実際の戻り値で確認する。

    API は一切呼ばない (空トピックは課金前に return するため)。
    """
    print("\n[4] run_future_search の戻り値契約")

    try:
        from tools.tool_future_search import run_future_search
    except Exception as e:  # 依存欠落時はスキップ (判定側は [1][3] で担保済み)
        print(f"  ⏭  skip: tool_future_search をロードできません ({type(e).__name__}: {e})")
        return

    result = run_future_search("")
    check("空トピックは失敗と判定される", is_failure_result(result), result[:60])

    from commands.bot_views import _dive_result_header
    check("その戻り値で見出しが ❌ になる",
          _dive_result_header("x", result).startswith("❌"), result[:60])


# ---------------------------------------------------------
# 5. /curate 側 (先に直した族の元祖 — 退行していないこと)
# ---------------------------------------------------------

def test_curator_result_icon():
    print("\n[5] /curate の _result_icon が退行していない")

    import commands.bot_commands_curator as bcc

    cases = [
        ("Success: Created [[30_Main/x.md]] (Path: D:\\x)", "✅"),
        ("Error: 'X' の検索結果が見つかりませんでした。", "❌"),
        ("Error searching for topic: timeout", "❌"),
        ("Error generating summary: quota exceeded", "❌"),
        ("既にノートが存在します: 2026-07-26_X.md", "⚠️"),
    ]
    for result, expected in cases:
        got = bcc._result_icon(result)
        check(f"{result[:26]}… → {expected}", got == expected, f"got {got}")


# ---------------------------------------------------------
# 6. 同じ見出しを使う入口が取り残されていないか (静的スキャン)
# ---------------------------------------------------------

def test_no_hardcoded_success_headers():
    """"✅ Mission Complete" / "✅ Task Completed" の直書きが復活していないこと。

    2026-07-26: /agent だけ直して /chat agent (bot_discord.py) と
    スケジューラ完了通知が ✅ 固定のまま残っていた。入口が増えるたびに
    同じ見逃しが起きるので、文字列の直書き自体を禁止する。
    """
    print("\n[6] ✅ 見出しの直書きが残っていない")

    # 判定ルール: 成功見出しの文字列は「同じ近傍に失敗側 (❌) の分岐がある」場合のみ許す。
    # 見出しを1箇所に集約したヘルパー (mission_header 等) はこの条件を満たし、
    # 新しい送信経路に ✅ だけをコピペすると満たさない。
    WINDOW = 12
    banned = ("✅ **Mission Complete**", "✅ **Task Completed**", "✅ 深掘り完了:")
    targets = [
        os.path.join(_PROJECT_ROOT, "bot_discord.py"),
        os.path.join(_PROJECT_ROOT, "agent_scheduler.py"),
        os.path.join(_PROJECT_ROOT, "commands", "bot_commands_agent.py"),
        os.path.join(_PROJECT_ROOT, "commands", "bot_views.py"),
    ]

    findings = []
    for path in targets:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if not any(p in line for p in banned):
                continue
            near = "".join(lines[max(0, i - 1 - WINDOW): i + WINDOW])
            if "❌" not in near:
                findings.append(
                    f"{os.path.relpath(path, _PROJECT_ROOT)}:{i} {stripped[:60]}")

    check(f"失敗側の分岐を伴わない ✅ 見出しが0件 ({len(targets)}ファイル走査)",
          not findings, " / ".join(findings))

    # 検出器が飾りになっていないことの確認 (❌ を持たない ✅ を捕まえられるか)
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="icons_"), "sample.py")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('async def send(x):\n    await x.send("✅ **Mission Complete**")\n')
    with open(tmp, encoding="utf-8") as f:
        sample = f.readlines()
    caught = any(
        any(p in ln for p in banned) and "❌" not in "".join(sample)
        for ln in sample
    )
    check("検出ルールが実際に違反を捕まえる", caught, "".join(sample))


def test_summarize_results_marks_failures():
    print("\n[7] engine のフォールバック要約が失敗を「完了」と書かない")

    from engine_plan_execute import _summarize_results

    results = [
        {"step": 1, "task": "検索", "observation": "Success: found", "success": True},
        {"step": 2, "task": "保存", "observation": "Error: disk full", "success": True},
        {"step": 3, "task": "通知", "observation": "Error: boom", "success": False},
    ]
    out = _summarize_results(results)
    lines = out.split("\n")

    check("成功ステップは従来どおり", "Success: found" in lines[0], lines[0])
    check("文字列で失敗を返したステップが ❌ になる",
          "❌ 失敗" in lines[1], lines[1])
    check("例外で失敗したステップが ❌ になる", "❌ 失敗" in lines[2], lines[2])
    check("エラー本文が捨てられない", "disk full" in lines[1], lines[1])
    check("失敗が「完了」と書かれない",
          not any("失敗" in ln and "完了" in ln for ln in lines), out)


# ---------------------------------------------------------
# 8. スケジューラの完了通知 (実際に _execute_task を走らせる)
# ---------------------------------------------------------

def test_scheduler_notifies_string_failures():
    """例外を投げずに "Error: ..." を返したタスクを ✅ で通知しないこと。

    定時ジョブは無人で動くため、ここが嘘をつくと「毎朝 ✅ が来ている＝健全」
    と誤認したまま壊れ続ける (tool_analyze_crypto 等は実際に文字列で失敗を返す)。
    """
    print("\n[8] スケジューラの完了通知")

    import logging
    import threading
    import agent_scheduler as sched

    # 意図的に投げる例外のトレースでテスト出力を汚さない
    # (logger は stderr/ファイル側なので redirect_stdout では止まらない)
    logging.disable(logging.CRITICAL)

    sent = []
    orig_notify = sched.send_discord_notification
    orig_record = sched._record_job_run
    recorded = []
    sched.send_discord_notification = lambda msg, **kw: sent.append(msg)
    sched._record_job_run = lambda name, ts: recorded.append(name)

    def _run(func):
        sent.clear()
        recorded.clear()
        task = {"name": "Dummy", "func": func, "_lock": threading.Lock(),
                "catch_up": True}
        import contextlib
        import io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            sched._execute_task(task)
        return list(sent), list(recorded)

    try:
        msgs, rec = _run(lambda: "Error: Could not fetch any crypto data.")
        completed = [m for m in msgs if "Task Completed" in m]
        failed = [m for m in msgs if "Task Failed" in m]
        check("失敗文字列は ✅ Task Completed で通知しない", not completed, str(msgs))
        check("❌ Task Failed で通知する", bool(failed), str(msgs))
        check("失敗時は catch-up 成功時刻を記録しない", not rec, str(rec))

        msgs, rec = _run(lambda: "Success: 週次レポートを保存しました")
        check("成功は従来どおり ✅ Task Completed",
              any("Task Completed" in m for m in msgs), str(msgs))
        check("成功時は catch-up 成功時刻を記録する", rec == ["Dummy"], str(rec))

        msgs, _ = _run(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        check("例外は従来どおり ❌ Task Failed",
              any("Task Failed" in m for m in msgs), str(msgs))
    finally:
        sched.send_discord_notification = orig_notify
        sched._record_job_run = orig_record
        logging.disable(logging.NOTSET)


# ---------------------------------------------------------
# 9. エンジン中核: 例外の有無だけで成否を決めていないか
# ---------------------------------------------------------

def test_execute_plan_marks_string_failures():
    """ツールが例外を投げず "Error: ..." を返したステップを failed にすること。

    2026-07-27: execute_plan は try/except の外に success=True を置いていたため、
    ツール層の戻り値契約 (例外を投げず Error 文字列を返す) と噛み合っておらず、
    失敗が Critic には成功として渡り、learning_db には outcome="success" で
    残っていた。2026-07-26 に bot 層で潰した穴が、エンジン中核に残っていた形。

    Gemini は呼ばない (args_json を計画に埋めて引数解決をスキップさせる)。
    """
    print("\n[9] execute_plan が文字列の失敗を failed にする")

    import contextlib
    import io as _io
    import engine_plan_execute as engine

    def _ok_tool(query: str = "") -> str:
        return "Success: 検索しました"

    def _string_fail_tool(query: str = "") -> str:
        # tools/ の規約どおり「例外を投げずに」失敗を返す
        return "Error: 意図的に失敗させたツールです"

    def _bracket_fail_tool(query: str = "") -> str:
        # lib/tool_registry の lazy wrapper 形式 (接頭辞 tuple では拾えない)
        return "[xtrend error] twitterAPI.io 401"

    fake_map = {
        "tool_ok": _ok_tool,
        "tool_string_fail": _string_fail_tool,
        "tool_bracket_fail": _bracket_fail_tool,
    }

    orig_config = engine._get_tools_config
    orig_observe = engine.observe
    engine._get_tools_config = lambda: ([], dict(fake_map))
    engine.observe = lambda name, func, caller, **kw: func(**kw)  # 観測ログを汚さない
    try:
        def _run(tool_name):
            plan = [{"step": 1, "task": "t", "tool": tool_name,
                     "args_json": '{"query": "x"}'}]
            with contextlib.redirect_stdout(_io.StringIO()):
                return engine.execute_plan(plan)

        ok = _run("tool_ok")
        check("成功するツールは従来どおり success", ok["results"][0]["success"], str(ok))

        for tool_name, label in (("tool_string_fail", "Error: 形式"),
                                 ("tool_bracket_fail", "[xtrend error] 形式")):
            res = _run(tool_name)
            step = res["results"][0]
            check(f"{label} のステップが failed になる", not step["success"], str(step))
            check(f"{label} で execute_plan 全体も失敗", not res["success"], str(res))
            check(f"{label} でエラー本文が observation に残る",
                  is_failure_result(str(step["observation"])), str(step["observation"]))
    finally:
        engine._get_tools_config = orig_config
        engine.observe = orig_observe


def test_log_execution_outcome_not_success():
    """文字列で失敗したステップが learning_db に outcome="success" で残らないこと。

    Agent_Performance.md の成功率はこの outcome の集計なので、ここが嘘をつくと
    「成功率92%」という水増しされた数字だけが残り、誰も壊れに気付けない。
    実 learning_db は触らない (ロード/セーブを差し替える)。
    """
    print("\n[10] log_execution の outcome が水増しされない")

    import contextlib
    import io as _io
    import engine_plan_execute as engine

    saved = {}
    orig_load, orig_save = engine._load_learning_db, engine._save_learning_db
    engine._load_learning_db = lambda: {"executions": [], "seq": 0}
    engine._save_learning_db = lambda db: saved.update(db)

    def _log(results, critique_status="ok"):
        saved.clear()
        with contextlib.redirect_stdout(_io.StringIO()):
            engine.log_execution("q", [{"step": 1}], results,
                                 {"status": critique_status}, 1.0)
        return saved["executions"][-1]

    try:
        entry = _log([{"step": 1, "task": "t", "tool": "tool_ok",
                       "observation": "Success: ok", "success": True}])
        check("全ステップ成功なら従来どおり success",
              entry["outcome"] == "success", str(entry))

        # execute_plan が is_failure_result を通した後の姿 (success=False)
        entry = _log([{"step": 1, "task": "t", "tool": "tool_x",
                       "observation": "Error: 意図的に失敗させたツールです",
                       "success": False}])
        check("文字列で失敗したステップは success で記録されない",
              entry["outcome"] != "success", str(entry))
        check("失敗理由が記録される",
              "意図的に失敗" in (entry["failure_reason"] or ""), str(entry))
        check("失敗ステップ番号が記録される", entry["failed_step"] == 1, str(entry))

        entry = _log([
            {"step": 1, "task": "a", "tool": "tool_ok",
             "observation": "Success: ok", "success": True},
            {"step": 2, "task": "b", "tool": "tool_x",
             "observation": "[xtrend error] 401", "success": False},
        ])
        check("一部失敗は partial", entry["outcome"] == "partial", str(entry))
    finally:
        engine._load_learning_db, engine._save_learning_db = orig_load, orig_save


def test_run_plan_and_execute_reports_total_failure():
    """全ステップが失敗した実行を、成功の文面で返さないこと。

    2026-07-27: execute_plan の成否判定を直しても、run_plan_and_execute は
    「修正回数を使い切った」経路で無条件に Gemini の要約を返していた。
    要約に失敗接頭辞は無いので mission_header は ✅ Mission Complete を出し、
    learning_db の outcome="failed" と食い違う (評価ハーネス T18/T19 で実測)。

    Gemini は呼ばない (generate_plan / critique_results / 要約生成を差し替える)。
    """
    print("\n[11] 全滅した実行が ✅ Mission Complete にならない")

    import contextlib
    import io as _io
    import engine_plan_execute as engine
    # 判定は bot 層と同じ窓口を使う (汎用の is_failure_result はエンジン固有の
    # "タスク失敗:" を知らない — その差分こそ _FAILURE_PREFIXES の存在理由)
    from commands.bot_commands_agent import mission_header, _is_failure

    plan = [{"step": 1, "task": "検索", "tool": "tool_x"}]
    fail_step = {"step": 1, "task": "検索", "tool": "tool_x",
                 "observation": "Error: 意図的に失敗させたツールです",
                 "success": False}

    orig = {name: getattr(engine, name) for name in
            ("generate_plan", "execute_plan", "critique_results",
             "log_execution", "_generate_final_summary")}
    logged = []
    engine.generate_plan = lambda q, session_context="": {
        "plan": list(plan), "expected_outcome": "x"}
    engine.log_execution = lambda *a, **kw: logged.append(a[2])
    # 「Critic が失敗を認めない」最悪ケースを固定で作る。
    # revised_plan を返し続けるので、修正回数を使い切って for を抜ける経路に入る。
    engine.critique_results = lambda *a: {
        "status": "needs_revision", "feedback": "もう一度やって",
        "revised_plan": list(plan)}
    engine._generate_final_summary = lambda q, r: (
        "## 📋 結論\n情報を取得できたのでまとめました")

    try:
        engine.execute_plan = lambda p, cb=None: {
            "success": False, "results": [dict(fail_step)], "final_output": ""}
        with contextlib.redirect_stdout(_io.StringIO()):
            result = engine.run_plan_and_execute("LoRAのメモを探して")
        check("全滅時は失敗として返る", _is_failure(result), result[:80])
        check("見出しが ❌ になる", mission_header(result).startswith("❌"),
              mission_header(result))
        check("エラー本文が捨てられない", "意図的に失敗" in result, result[:120])
        check("learning_db 側も success ではない",
              logged and not any(r.get("success") for r in logged[-1]), str(logged))

        # 1歩でも成功していれば従来どおり Gemini 要約を返す (過剰に ❌ にしない)
        engine.execute_plan = lambda p, cb=None: {
            "success": True,
            "results": [{"step": 1, "task": "検索", "tool": "tool_x",
                         "observation": "Success: ok", "success": True}],
            "final_output": ""}
        with contextlib.redirect_stdout(_io.StringIO()):
            result = engine.run_plan_and_execute("LoRAのメモを探して")
        check("成功があれば従来どおり要約を返す",
              mission_header(result).startswith("✅"), result[:80])
    finally:
        for name, func in orig.items():
            setattr(engine, name, func)


if __name__ == "__main__":
    print("=" * 55)
    print(" 結果アイコン (失敗を ✅ で出さない) 回帰テスト")
    print("=" * 55)

    test_is_failure_result()
    test_mission_header()
    test_dive_result_header()
    test_future_search_error_contract()
    test_curator_result_icon()
    test_no_hardcoded_success_headers()
    test_summarize_results_marks_failures()
    test_scheduler_notifies_string_failures()
    test_execute_plan_marks_string_failures()
    test_log_execution_outcome_not_success()
    test_run_plan_and_execute_reports_total_failure()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
