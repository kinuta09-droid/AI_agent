# -*- coding: utf-8 -*-
"""採点器そのもののテスト (scripts/eval_agent.py)

このテストが存在する理由:
  評価ハーネスは「エージェントが壊れていることに気付くための仕組み」なので、
  ハーネス自身が壊れていると二重に気付けなくなる。特に危ないのは
  **甘い採点器** — 本来落とすべき計画を pass にしてしまうと、
  「合格率100%」という新しい嘘が1つ増えるだけになる。
  そのため「悪い計画を確実に落とす」ケースを明示的に持つ。

  加えて、評価セット JSON 自体もここで検証する
  (期待値に存在しないツール名を書いたまま気付かない事故を防ぐ)。

実行: py tests/test_eval_agent.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.eval_agent import (
    load_eval_set, validate_question, missing_required_tools,
    plan_tools, plan_args_blob, score_plan, score_execution,
    select_questions, estimate_calls, summarize, render_report,
    build_tier_b_overrides, patched_tools, WRITE_TOOLS,
    BLOCKED_MESSAGE, INJECTED_MESSAGE,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}")


def step(n, tool, args_json="", args_hint="", task="タスク"):
    return {"step": n, "task": task, "tool": tool,
            "args_json": args_json, "args_hint": args_hint}


def passed(checks) -> bool:
    return all(c["ok"] for c in checks)


def failed_names(checks):
    return [c["name"] for c in checks if not c["ok"]]


TOOLS = {"tool_web_search", "tool_vault_hybrid_search", "tool_create_note",
         "tool_update_note", "tool_analyze_crypto", "tool_fetch_url",
         "tool_audit_predictions", "tool_crypto_stats"}


# ---------------------------------------------------------
# 1. 計画からの抽出
# ---------------------------------------------------------

def test_extraction():
    print("\n[1] 計画からの抽出")
    plan = [step(1, "tool_web_search", '{"query": "AI 2026"}'),
            step(2, "tool_create_note", args_hint="調査結果を保存")]
    check("ツール名を出現順で取り出す",
          plan_tools(plan) == ["tool_web_search", "tool_create_note"])

    blob = plan_args_blob(plan)
    check("args_json と args_hint が判定対象に入る",
          "AI 2026" in blob and "調査結果を保存" in blob)

    # task にだけ書かれた語で通ってしまうと「引数として効いているか」を測れない
    task_only = [step(1, "tool_web_search", task="2026年のAI動向を調べる")]
    check("task 本文は判定対象に入らない",
          "2026" not in plan_args_blob(task_only), plan_args_blob(task_only))


# ---------------------------------------------------------
# 2. 採点器が「悪い計画」を確実に落とすこと（最重要）
# ---------------------------------------------------------

def test_must_not_use_is_enforced():
    print("\n[2] must_not_use — 甘い採点器を作らない")
    q = {"assertions": {"must_not_use": ["tool_web_search"], "min_steps": 1}}

    ok_plan = [step(1, "tool_vault_hybrid_search")]
    check("禁止ツールを使っていない計画は合格", passed(score_plan(q, ok_plan, TOOLS)))

    ng_plan = [step(1, "tool_vault_hybrid_search"), step(2, "tool_web_search")]
    checks = score_plan(q, ng_plan, TOOLS)
    check("禁止ツールが1つでも混ざれば不合格", not passed(checks), failed_names(checks))
    check("落ちた理由が根拠として残る",
          any("計画に含まれる" in c["detail"] for c in checks if not c["ok"]))

    # 空の計画は「禁止ツールを使っていない」を自明に満たしてしまう。
    # 否定制約の問で min_steps を併記していないと、ここが抜け穴になる。
    empty_checks = score_plan(q, [], TOOLS)
    check("空の計画は min_steps で落ちる", not passed(empty_checks), failed_names(empty_checks))


def test_must_use():
    print("\n[3] must_use_all / must_use_any")
    q_all = {"assertions": {"must_use_all": ["tool_analyze_crypto"]}}
    check("必須ツールがあれば合格",
          passed(score_plan(q_all, [step(1, "tool_analyze_crypto")], TOOLS)))
    check("必須ツールが無ければ不合格",
          not passed(score_plan(q_all, [step(1, "tool_web_search")], TOOLS)))

    q_any = {"assertions": {"must_use_any": [["tool_fetch_url", "tool_web_search"]]}}
    check("グループのいずれか1つで合格",
          passed(score_plan(q_any, [step(1, "tool_fetch_url")], TOOLS)))
    check("グループが全滅なら不合格",
          not passed(score_plan(q_any, [step(1, "tool_create_note")], TOOLS)))


def test_steps():
    print("\n[4] ステップ数")
    q = {"assertions": {"min_steps": 1, "max_steps": 2}}
    check("範囲内は合格", passed(score_plan(q, [step(1, "tool_web_search")], TOOLS)))
    check("超過は不合格",
          not passed(score_plan(q, [step(i, "tool_web_search") for i in range(1, 4)], TOOLS)))
    check("空は不合格", not passed(score_plan(q, [], TOOLS)))


def test_order():
    print("\n[5] 順序")
    q = {"assertions": {"order": [[["tool_web_search"], ["tool_create_note"]]]}}
    check("収集 → 保存 は合格",
          passed(score_plan(q, [step(1, "tool_web_search"),
                                step(2, "tool_create_note")], TOOLS)))
    checks = score_plan(q, [step(1, "tool_create_note"),
                            step(2, "tool_web_search")], TOOLS)
    check("保存 → 収集 は不合格 (空ノートができる)", not passed(checks), failed_names(checks))
    check("片方が欠けたら判定不能として不合格",
          not passed(score_plan(q, [step(1, "tool_web_search")], TOOLS)))


def test_args_regex():
    print("\n[6] args 正規表現 — 境界の罠を再現しない")
    pat_ok, pat_ng = r"(?<!\d)2026(?!\d)", r"(?<!\d)202[0-5](?!\d)"
    q = {"assertions": {"args_must_match": [pat_ok], "args_must_not_match": [pat_ng]}}
    check("正しい年号は合格",
          passed(score_plan(q, [step(1, "tool_web_search", '{"query": "AI 2026"}')], TOOLS)))

    checks = score_plan(q, [step(1, "tool_web_search", '{"query": "AI 2024"}')], TOOLS)
    check("古い年号は不合格", not passed(checks), failed_names(checks))
    check("一致した文字列が根拠に残る",
          any("2024" in c["detail"] for c in checks if not c["ok"]))

    # 部分文字列マッチで5ヶ月を溶かした tool_crypto のニュースゲートと同じ罠を
    # 採点器側で作り直していないことの確認
    boundary = score_plan(q, [step(1, "tool_web_search", '{"query": "id 32026x"}')], TOOLS)
    check("'32026x' には一致しない (数字の隣接を弾いている)",
          f"args_must_match: {pat_ok}" in failed_names(boundary))

    # 逆向きの罠: \b は日本語に効かない。'年' は Unicode の単語文字なので
    # \b2026\b は「2026年」に一致せず、正しい計画を偽の不合格にする。
    # 2026-07-27 の初回ベースライン実行で実際にこれを踏んだ。
    jp = [step(1, "tool_web_search", '{"query": "2026年 AI業界 主要な出来事"}')]
    check("日本語に隣接した年号 ('2026年') でも合格する", passed(score_plan(q, jp, TOOLS)),
          failed_names(score_plan(q, jp, TOOLS)))
    check(r"参考: \b2026\b は '2026年' に一致しない (この書き方を使わない根拠)",
          not passed(score_plan({"assertions": {"args_must_match": [r"\b2026\b"]}},
                                jp, TOOLS)))

    underscore = [step(1, "tool_create_note", '{"filename": "2026_AI_Events.md"}')]
    check("アンダースコア隣接 ('2026_AI') でも合格する",
          passed(score_plan(q, underscore, TOOLS)))


def test_unknown_tool():
    print("\n[7] 未知ツールの検出")
    q = {"assertions": {"min_steps": 1}}
    checks = score_plan(q, [step(1, "tool_send_email")], TOOLS)
    check("レジストリに無いツールを提案したら不合格", not passed(checks), failed_names(checks))
    check("既知ツールのみなら合格", passed(score_plan(q, [step(1, "tool_web_search")], TOOLS)))


# ---------------------------------------------------------
# 8. Tier B — 「失敗を ✅ で表示しない」の判定
# ---------------------------------------------------------

def test_execution_scoring():
    print("\n[8] Tier B: 失敗報告の整合性")
    q = {"exec_assertions": {"expect_consistent_report": True,
                             "inject_tool_failure": ["tool_web_search"]}}

    # 本命: ツールが失敗したのに、Gemini の最終要約が成功文で返ってきたケース。
    # run_plan_and_execute は失敗時も _generate_final_summary() を返す経路があり、
    # その文字列は失敗接頭辞を持たないため見出しが ✅ になる。
    ng = score_execution(q, "検索結果をまとめました。以下が要点です。", {"outcome": "failed"})
    check("outcome=failed なのに ✅ 見出しなら不合格", not passed(ng), failed_names(ng))
    check("矛盾の根拠が残る",
          any("失敗が成功として表示される" in c["detail"] for c in ng if not c["ok"]))

    ok = score_execution(q, "Error: [eval] 意図的に失敗させたツールです",
                         {"outcome": "failed"})
    check("失敗が失敗として報告されていれば合格", passed(ok), failed_names(ok))

    # 前提の陳腐化に強いこと: ツールが直って成功しても、注入がある問では
    # 「注入したのに success」を落とす (空振りを見逃さない)
    stale = score_execution(q, "予定を確認しました。", {"outcome": "success"})
    check("注入した失敗が起きていないなら不合格 (空振りの検出)",
          not passed(stale), failed_names(stale))

    no_entry = score_execution(q, "何か返ってきた", None)
    check("learning_db に記録が無ければ不合格", not passed(no_entry), failed_names(no_entry))

    q_answer = {"exec_assertions": {"answer_must_match": [r"存在し(ない|ません)"]}}
    check("回答の正規表現が効く",
          passed(score_execution(q_answer, "その機能は存在しません。", None)))
    check("回答が期待と違えば不合格",
          not passed(score_execution(q_answer, "音声通話機能の使い方は以下の通りです。", None)))


def test_tier_b_guards():
    print("\n[8b] Tier B: 書き込みブロックと失敗注入")
    q = {"exec_assertions": {"inject_tool_failure": ["tool_vault_hybrid_search"]}}

    overrides = build_tier_b_overrides(q, allow_writes=False)
    check("書き込み系ツールが既定でブロックされる",
          "tool_create_note" in overrides and "tool_update_note" in overrides)
    check("Critic が後から足しうる保存系も含まれる",
          {"tool_gmail_check", "tool_fetch_daily_news"} <= set(overrides))
    check("読み取り専用ツールはブロックしない", "tool_web_search" not in WRITE_TOOLS)
    check("注入対象が上書き対象に入る", "tool_vault_hybrid_search" in overrides)
    check("ブロックとスタブの文言が区別できる",
          overrides["tool_create_note"]() == BLOCKED_MESSAGE
          and overrides["tool_vault_hybrid_search"]() == INJECTED_MESSAGE)

    allowed = build_tier_b_overrides(q, allow_writes=True)
    check("--allow-writes ならブロックしない", "tool_create_note" not in allowed)
    check("--allow-writes でも失敗注入は残る", "tool_vault_hybrid_search" in allowed)

    check("スタブは任意の引数で呼べる",
          overrides["tool_create_note"]("a", filename="b") == BLOCKED_MESSAGE)

    # optional ツール (calendar 等) が未ロードの環境でも落ちないこと
    with patched_tools({"tool_does_not_exist": _stub_marker}):
        pass
    check("レジストリに無い名前の差し替えは黙って無視される", True)

    # 差し替えが engine 側から見えること、かつ必ず元へ戻ること
    from lib import tool_registry
    original, _ = tool_registry.get_tools_config()
    _, before_map = tool_registry.get_tools_config()
    baseline = before_map["tool_web_search"]
    with patched_tools({"tool_web_search": _stub_marker}):
        _, during = tool_registry.get_tools_config()
        check("差し替えが get_tools_config() 経由で見える",
              during["tool_web_search"] is _stub_marker)
    _, after = tool_registry.get_tools_config()
    check("コンテキストを抜けたら元に戻る", after["tool_web_search"] is baseline)


def _stub_marker():
    return "marker"


# ---------------------------------------------------------
# 9. 評価セット JSON 自体の検証
# ---------------------------------------------------------

def test_validate_question():
    print("\n[9] 設問の静的検証")
    base = {"id": "X", "category": "c", "query": "q", "note": "n",
            "assertions": {"must_use_all": ["tool_web_search"]}}
    check("健全な設問は問題なし", validate_question(base, TOOLS) == [])

    bad_tool = dict(base, assertions={"must_use_all": ["tool_nonexistent"]})
    check("存在しないツール名を期待値に書いたら検出",
          any("未知のツール名" in p for p in validate_question(bad_tool, TOOLS)))

    bad_re = dict(base, assertions={"args_must_match": ["(unclosed"]})
    check("壊れた正規表現を検出",
          any("正規表現が不正" in p for p in validate_question(bad_re, TOOLS)))

    word_boundary = dict(base, assertions={"args_must_match": [r"\b2026\b"]})
    check("日本語に効かない \\b の使用を検出",
          any("\\b を使っている" in p for p in validate_question(word_boundary, TOOLS)))

    no_assert = dict(base, assertions={})
    check("assertions が空の設問を検出",
          any("assertions が空" in p for p in validate_question(no_assert, TOOLS)))

    dead_exec = dict(base, safe_exec=False, exec_assertions={"expect_consistent_report": True})
    check("safe_exec=false なのに exec_assertions がある設問を検出",
          any("永久に評価されない" in p for p in validate_question(dead_exec, TOOLS)))

    bad_inject = dict(base, safe_exec=True,
                      exec_assertions={"inject_tool_failure": ["tool_nonexistent"]})
    check("存在しないツールへの失敗注入を検出",
          any("存在しないツールに失敗を注入" in p for p in validate_question(bad_inject, TOOLS)))

    orphan_inject = dict(base, safe_exec=True,
                         exec_assertions={"inject_tool_failure": ["tool_create_note"]})
    check("計画に現れないツールへの失敗注入 (空振り) を検出",
          any("空振り" in p for p in validate_question(orphan_inject, TOOLS)))

    good_inject = dict(base, safe_exec=True,
                       exec_assertions={"inject_tool_failure": ["tool_web_search"],
                                        "expect_consistent_report": True})
    check("must_use_all と対応した失敗注入は問題なし",
          validate_question(good_inject, TOOLS) == [])

    check("必須キー欠落を検出",
          any("必須キー" in p for p in validate_question({"assertions": {"max_steps": 1}}, TOOLS)))


def test_missing_required_tools():
    print("\n[10] 未ロードツールによるスキップ判定")
    q = {"assertions": {"must_use_all": ["tool_get_calendar"]}}
    check("must_use_all のツールが無ければスキップ対象",
          missing_required_tools(q, TOOLS) == ["tool_get_calendar"])

    q_any = {"assertions": {"must_use_any": [["tool_get_calendar", "tool_web_search"]]}}
    check("must_use_any はグループに1つ生きていればスキップしない",
          missing_required_tools(q_any, TOOLS) == [])

    q_not = {"assertions": {"must_not_use": ["tool_get_calendar"]}}
    check("must_not_use はロードされていなくてもスキップしない",
          missing_required_tools(q_not, TOOLS) == [])


def test_real_eval_set():
    print("\n[11] 実際の評価セット (tests/fixtures/agent_eval_set.json)")
    from scripts.eval_agent import known_tools
    data = load_eval_set()
    questions = data["questions"]
    check("20問ある", len(questions) == 20, f"実際は {len(questions)}問")

    ids = [q["id"] for q in questions]
    check("ID が重複していない", len(set(ids)) == len(ids))

    tools = known_tools()
    problems = []
    for q in questions:
        for issue in validate_question(q, tools):
            problems.append(f"{q['id']}: {issue}")
    check("全問が静的検証を通る", not problems, " / ".join(problems))

    check("全問に note (この問がある理由) がある",
          all(len(q.get("note", "")) > 10 for q in questions))

    negation = [q for q in questions if q["category"] == "negation"]
    check("否定制約の問は must_not_use を持つ",
          all("must_not_use" in q["assertions"] for q in negation))
    check("否定制約の問は空計画で通り抜けられない (min_steps か must_use_* がある)",
          all(("min_steps" in q["assertions"]
               or q["assertions"].get("must_use_all")
               or q["assertions"].get("must_use_any")) for q in negation),
          [q["id"] for q in negation
           if not ("min_steps" in q["assertions"] or q["assertions"].get("must_use_all")
                   or q["assertions"].get("must_use_any"))])

    tier_b = [q for q in questions if q.get("safe_exec") and q.get("exec_assertions")]
    check("Tier B 対象が存在する", len(tier_b) >= 1, f"{len(tier_b)}問")

    failure_qs = [q for q in questions if q["category"] == "failure_report"]
    check("失敗報告の問はハーネス側で決定的に失敗させる (壊れたツールに依存しない)",
          all(q.get("exec_assertions", {}).get("inject_tool_failure") for q in failure_qs),
          [q["id"] for q in failure_qs
           if not q.get("exec_assertions", {}).get("inject_tool_failure")])
    check("失敗報告の問は2本以上ある (入口ごとの取り残し対策)", len(failure_qs) >= 2)


# ---------------------------------------------------------
# 12. CLI 補助関数
# ---------------------------------------------------------

def test_selection_and_budget():
    print("\n[12] 絞り込みと呼び出し見積り")
    questions = load_eval_set()["questions"]
    check("ID で絞り込める", [q["id"] for q in select_questions(questions, "T06")] == ["T06"])
    check("カテゴリで絞り込める",
          {q["category"] for q in select_questions(questions, "negation")} == {"negation"})
    check("空指定は全件", len(select_questions(questions, "")) == len(questions))

    plan_only = estimate_calls(questions, full=False)
    check("Tier A の見積りは 1問1回", plan_only == len(questions), plan_only)
    check("--full は見積りが増える", estimate_calls(questions, full=True) > plan_only)

    tier_b_ids = {q["id"] for q in questions if q.get("safe_exec") and q.get("exec_assertions")}
    unsafe = [q for q in questions if not q.get("safe_exec")]
    check("safe_exec=false の問は Tier B 見積りに入らない",
          all(q["id"] not in tier_b_ids for q in unsafe))


def test_report():
    print("\n[13] レポート出力")
    results = [
        {"id": "T01", "category": "tool_selection", "query": "q1", "note": "n1",
         "status": "pass", "plan": [], "checks": [], "exec_checks": [],
         "answer": "", "error": ""},
        {"id": "T06", "category": "negation", "query": "q2", "note": "否定制約の回帰",
         "status": "fail", "plan": [step(1, "tool_web_search")],
         "checks": [{"name": "must_not_use: tool_web_search", "ok": False,
                     "detail": "禁止ツールが計画に含まれる"}],
         "exec_checks": [], "answer": "", "error": ""},
        {"id": "T18", "category": "failure_report", "query": "q3", "note": "n3",
         "status": "skipped", "plan": [], "checks": [], "exec_checks": [],
         "answer": "", "error": "必要なツールが現在ロードされていない: tool_get_calendar"},
    ]
    summary = summarize(results)
    check("スキップは合格率の母数に入らない", summary["graded"] == 2, summary)
    check("合格率が計算できる", abs(summary["pass_rate"] - 50.0) < 0.01, summary)

    meta = {"timestamp": "t", "model": "m", "full": False, "eval_set": "e",
            "version": 1, "estimated_calls": 3, "actual_calls": 3}
    report = render_report(results, meta)
    check("落ちた問の根拠がレポートに出る", "禁止ツールが計画に含まれる" in report)
    check("この問がある理由がレポートに出る", "否定制約の回帰" in report)
    check("スキップ理由がレポートに出る", "tool_get_calendar" in report)

    # Tier B 未実行の問を ✅ だけで見せると「失敗報告は正常」と誤読される
    pending = list(results)
    pending[0] = dict(results[0], tier_b_pending="実行していない (--full 未指定)")
    report2 = render_report(pending, meta)
    check("Tier B 未評価が印付きで出る", "✅* |" in report2 or "✅*" in report2)
    check("Tier B 未評価の理由が本文に出る", "--full 未指定" in report2)


def test_comparison():
    print("\n[14] ベースライン比較")
    from scripts.eval_agent import render_comparison
    base = [{"id": "T01", "status": "pass"}, {"id": "T09", "status": "fail"}]
    same = [{"id": "T01", "category": "c", "status": "pass"},
            {"id": "T09", "category": "n", "status": "fail"}]
    check("差分が無ければその旨を書く", "合否が変わった問はありません" in
          render_comparison(same, base))

    改善 = [{"id": "T01", "category": "c", "status": "pass"},
            {"id": "T09", "category": "negation", "status": "pass"}]
    diff = render_comparison(改善, base)
    check("合否が変わった問だけを出す", "T09" in diff and "| T01 |" not in diff)
    check("before/after が読める", "fail" in diff and "pass" in diff)

    new_q = render_comparison([{"id": "T99", "category": "x", "status": "pass"}], base)
    check("ベースラインに無い問は '-' として出る", "| - |" in new_q)


if __name__ == "__main__":
    print("=" * 60)
    print("Agent Eval Harness テスト")
    print("=" * 60)
    test_extraction()
    test_must_not_use_is_enforced()
    test_must_use()
    test_steps()
    test_order()
    test_args_regex()
    test_unknown_tool()
    test_execution_scoring()
    test_tier_b_guards()
    test_validate_question()
    test_missing_required_tools()
    test_real_eval_set()
    test_selection_and_budget()
    test_report()
    test_comparison()
    print("=" * 60)
    print(f"PASS: {PASS} / FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)
