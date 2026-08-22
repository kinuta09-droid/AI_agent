# -*- coding: utf-8 -*-
"""
Agent Eval Harness — エージェント回答品質の外形的な計測

なぜこれが要るか:
  learning_db.json の outcome は「全ステップで例外が出なかったか」しか見ていない
  (engine_plan_execute.log_execution)。間違ったツールを選んでも、日付を2024と
  誤解しても、「Web検索せずに」を無視しても、例外さえ出なければ success になる。
  唯一の品質判定である Critic はエージェント自身であり、2026-07-26 に一掃した
  「失敗を ✅ で表示する」自己申告と同じ構造をしている。
  ここに、再現可能で機械判定可能な合否を入れる。

二層構造 (副作用と課金を分ける):
  Tier A (既定)  generate_plan() のみ。副作用ゼロ。1問あたり Gemini 1回。
  Tier B (--full) run_plan_and_execute() を safe_exec=true の問だけで実行。
                  本物のツールを叩くため、書き込み系が絡む問は除外される。

使い方:
  py scripts/eval_agent.py                    # Tier A のみ
  py scripts/eval_agent.py --full             # Tier A + Tier B (安全問のみ)
  py scripts/eval_agent.py --only T06,T07     # ID またはカテゴリで絞り込み
  py scripts/eval_agent.py --dry-run          # Gemini を叩かず設定の整合だけ検証
  py scripts/eval_agent.py --compare logs/eval/eval_20260727_1200.json

採点方針:
  判定は原則コードで行い、LLM に採点させない (採点器まで自己申告になるため)。
  落ちた問は必ず「どのアサーションがどう外れたか」を根拠としてレポートに残す。

  正規表現の境界は \\b を使わない。判定対象は日本語混じりのテキストで、
  '年' や '_' は Unicode の単語文字なので "2026年" に \\b2026\\b は一致せず、
  正しい計画を偽の不合格にする (2026-07-27 の初回ベースラインで実際に踏んだ)。
  数字は (?<!\\d)...(?!\\d)、英単語は (?<![A-Za-z])...(?![A-Za-z]) を使う。
  validate_question() が \\b の再混入を落とす。
"""
import os
import re
import sys
import json
import time
import argparse
import datetime
import contextlib

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(_ROOT)
sys.stdout.reconfigure(encoding='utf-8')

# ルートは直書きしない。すぐ上で __file__ から算出しているのに、以前ここだけ
# "C:\\AI_agent" と書いていたため、リポジトリを別の場所に置いた環境 (CI の
# D:\\a\\AI_agent\\AI_agent) では load_eval_set() が FileNotFoundError になり、
# tests/test_eval_agent.py が丸ごと落ちていた (2026-07-30)。
EVAL_SET_PATH = os.path.join(_ROOT, "tests", "fixtures", "agent_eval_set.json")
EVAL_OUT_DIR = os.path.join(_ROOT, "logs", "eval")

# Tier B 1問あたりの Gemini 呼び出し見積り (計画1 + 引数解決/本文生成 + Critic + 最終要約)
TIER_B_CALLS_PER_QUESTION = 5

# Tier B で既定ブロックするツール = 実行すると Vault や外部サービスに永続的な
# 書き込みが残るもの。「読み取り系に見えるが結果をノート保存する」ツール
# (gmail_check / analyze_crypto / fetch_daily_news 等) もここに含める。
#
# なぜ計画を見て判断するのでは足りないか:
#   2026-07-27 の初回 --full 実行で、gmail の問が Critic の revised_plan により
#   tool_create_note を後から足し、実 Vault にノートを1件作った。
#   計画段階で「書き込み無し」に見えても、実行中に書き込みが生えてくる。
#   したがって実行時のツールマップ側で塞ぐしかない。
WRITE_TOOLS = frozenset({
    "tool_create_note", "tool_update_note", "tool_refactor_note", "tool_move_note",
    "tool_fetch_daily_news", "tool_generate_briefing", "tool_gmail_check",
    "tool_analyze_crypto", "tool_scan_crypto_market",
    "tool_future_search", "tool_xtrend", "tool_xtrend_prompt",
    "tool_add_event", "tool_create_task", "tool_sync_to_daily_note",
})

BLOCKED_MESSAGE = "Error: [eval] 書き込み系ツールは評価中は無効化されています"
INJECTED_MESSAGE = "Error: [eval] 意図的に失敗させたツールです"


# ---------------------------------------------------------
# 評価セットの読み込みと静的検証
# ---------------------------------------------------------

def load_eval_set(path: str = EVAL_SET_PATH) -> dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def known_tools() -> set:
    """現在ロードできるツール名の集合 (唯一の情報源は lib.tool_registry)。

    optional ツール (calendar/todoist) は認証情報が無いと登録されないため、
    ここに含まれないことが「壊れている」ではなく「今は無い」を意味する。
    """
    from lib.tool_registry import get_tools_config
    config_tools, _ = get_tools_config()
    return {t["name"] for t in config_tools}


@contextlib.contextmanager
def patched_tools(overrides: dict):
    """Tier B 実行中だけツールの実体を差し替える。

    lib.tool_registry._CACHE の tools_map を直接書き換える。get_tools_config()
    は毎回この dict のコピーを返すため、execute_plan がステップ毎に取り直しても
    差し替えが効く。終了時に必ず元へ戻す。

    engine 側にテスト用の分岐を足さないための措置 — ロジックのフォークを作ると
    「評価では通るが本番では通らない」が生まれる。
    """
    from lib import tool_registry
    if not overrides:
        yield
        return
    tool_registry.get_tools_config()  # キャッシュを構築させる
    _, tools_map = tool_registry._CACHE
    saved = {name: tools_map[name] for name in overrides if name in tools_map}
    try:
        for name, func in overrides.items():
            if name in tools_map:
                tools_map[name] = func
        yield
    finally:
        tools_map.update(saved)


def _stub(message: str):
    """どんな引数で呼ばれても固定文字列を返すスタブ。

    例外ではなく "Error: " 文字列を返すのは、ツール層の戻り値契約
    (例外を投げず Error 文字列を返す) に合わせるため。
    """
    def _f(*args, **kwargs):
        return message
    return _f


def build_tier_b_overrides(q: dict, allow_writes: bool) -> dict:
    """Tier B 実行時に差し替えるツールの辞書を作る。"""
    overrides = {}
    if not allow_writes:
        for name in WRITE_TOOLS:
            overrides[name] = _stub(BLOCKED_MESSAGE)
    for name in q.get("exec_assertions", {}).get("inject_tool_failure", []):
        overrides[name] = _stub(INJECTED_MESSAGE)
    return overrides


def _iter_expected_tools(assertions: dict):
    """アサーションが言及している全ツール名を列挙する。"""
    for name in assertions.get("must_use_all", []):
        yield name
    for group in assertions.get("must_use_any", []):
        for name in group:
            yield name
    for name in assertions.get("must_not_use", []):
        yield name
    for before, after in assertions.get("order", []):
        for name in list(before) + list(after):
            yield name


def validate_question(q: dict, tools: set) -> list:
    """1問の設定の静的検証。問題があれば説明文字列のリストを返す。

    「期待値に存在しないツール名を書いたまま、誰も気付かない」を防ぐのが目的。
    optional ツールは欠落を警告に留める (認証が無い環境でも dry-run を通す)。
    """
    problems = []
    for key in ("id", "category", "query", "note"):
        if not q.get(key):
            problems.append(f"必須キー '{key}' が無い/空")

    assertions = q.get("assertions", {})
    if not assertions:
        problems.append("assertions が空 — 何も判定しない問は評価セットに置かない")

    for name in _iter_expected_tools(assertions):
        if name not in tools:
            problems.append(f"未知のツール名を期待値に書いている: {name}")

    for key in ("args_must_match", "args_must_not_match"):
        for pattern in assertions.get(key, []):
            try:
                re.compile(pattern)
            except re.error as e:
                problems.append(f"{key} の正規表現が不正: {pattern} ({e})")
            if "\\b" in pattern:
                # 判定対象は日本語混じりのテキスト。'年' や '_' は Unicode の単語文字
                # なので "2026年" に \b2026\b は一致せず、正しい計画を偽の不合格にする。
                problems.append(
                    f"{key} に \\b を使っている ({pattern}) — 日本語に隣接した語では "
                    f"境界が成立しない。数字なら (?<!\\d)...(?!\\d) を使う")

    exec_assertions = q.get("exec_assertions", {})
    for key in ("answer_must_match", "answer_must_not_match"):
        for pattern in exec_assertions.get(key, []):
            try:
                re.compile(pattern)
            except re.error as e:
                problems.append(f"{key} の正規表現が不正: {pattern} ({e})")

    injected = exec_assertions.get("inject_tool_failure", [])
    for name in injected:
        if name not in tools:
            problems.append(f"存在しないツールに失敗を注入しようとしている: {name}")
    if injected:
        # 注入対象が計画に現れる保証が無いと、失敗が起きず判定が空振りする
        reachable = set(assertions.get("must_use_all", []))
        for group in assertions.get("must_use_any", []):
            reachable.update(group)
        if not set(injected) & reachable:
            problems.append("inject_tool_failure の対象が must_use_* に含まれていない "
                            "— 計画に現れず注入が空振りする")

    if exec_assertions and not q.get("safe_exec"):
        problems.append("exec_assertions があるのに safe_exec=false — Tier B で永久に評価されない")

    return problems


def missing_required_tools(q: dict, tools: set) -> list:
    """この問が必要とするのに現在ロードされていないツール (→ SKIP 理由)。

    must_not_use は「無ければ自明に満たされる」ので対象外。
    must_use_any はグループ内に1つでも生きていれば評価できるので、
    グループ全滅のときだけ欠落とみなす。
    """
    assertions = q.get("assertions", {})
    missing = [n for n in assertions.get("must_use_all", []) if n not in tools]
    for group in assertions.get("must_use_any", []):
        if not any(n in tools for n in group):
            missing.append("|".join(group))
    return missing


# ---------------------------------------------------------
# 計画からの抽出
# ---------------------------------------------------------

def plan_tools(plan: list) -> list:
    """計画に現れるツール名を出現順で返す (重複はそのまま)。"""
    return [str(item.get("tool", "")) for item in plan]


def plan_args_blob(plan: list) -> str:
    """args_json と args_hint を連結した、正規表現判定用のテキスト。

    task 本文は含めない: プランナーがユーザーの語をそのまま写経しただけで
    通ってしまい、「引数として実際に効いているか」を測れなくなるため。
    """
    parts = []
    for item in plan:
        parts.append(str(item.get("args_json", "")))
        parts.append(str(item.get("args_hint", "")))
    return "\n".join(parts)


# ---------------------------------------------------------
# 採点 (Tier A)
# ---------------------------------------------------------

def _check(checks: list, name: str, ok: bool, detail: str = "",
           fail_detail: str = "") -> None:
    """チェック1件を積む。

    detail      合否に関わらず残す事実。「実際は 1 ステップ (['tool_update_note'])」
                のように、合格した問でも読む価値がある情報だけを渡す。
    fail_detail 失敗の説明。合格時は捨てる。

    2つに分けている理由: 失敗の説明文を detail に無条件で入れると、
    ok=true なのに detail が「回答に一致なし: ...」になり、レポートを読む人が
    合格を失敗と読み違える。このプロジェクトが何度も潰してきた
    「表示が実態と食い違う」バグ類型そのものなので、
    呼び出し側が「これは失敗時にしか意味を成さない文か」を明示する。
    """
    ok = bool(ok)
    parts = [detail] if ok else [detail, fail_detail]
    checks.append({"name": name, "ok": ok,
                   "detail": " ".join(p for p in parts if p)})


def score_plan(q: dict, plan: list, tools: set = None) -> list:
    """計画に対するアサーションを評価し、チェック結果のリストを返す。

    Gemini を呼ばない純関数。テストからそのまま叩ける。
    """
    assertions = q.get("assertions", {})
    used = plan_tools(plan)
    used_set = set(used)
    blob = plan_args_blob(plan)
    checks = []

    # 全問共通: 未知のツール名が計画に現れないこと。
    # 現在は _build_plan_schema の enum が構造的に防いでいるので、
    # ここが落ちたら enum の結線が外れたということ。
    if tools is not None:
        unknown = sorted(t for t in used_set if t and t not in tools)
        _check(checks, "未知のツールを提案していない", not unknown,
               fail_detail=f"未知のツール: {', '.join(unknown)}")

    for name in assertions.get("must_use_all", []):
        _check(checks, f"must_use_all: {name}", name in used_set,
               f"計画のツール: {used or '(空)'}")

    for group in assertions.get("must_use_any", []):
        label = " | ".join(group)
        _check(checks, f"must_use_any: {label}",
               any(n in used_set for n in group),
               f"計画のツール: {used or '(空)'}")

    for name in assertions.get("must_not_use", []):
        _check(checks, f"must_not_use: {name}", name not in used_set,
               fail_detail=f"禁止ツールが計画に含まれる (計画: {used})")

    if "min_steps" in assertions:
        lo = assertions["min_steps"]
        _check(checks, f"min_steps >= {lo}", len(plan) >= lo,
               f"実際は {len(plan)} ステップ")

    if "max_steps" in assertions:
        hi = assertions["max_steps"]
        _check(checks, f"max_steps <= {hi}", len(plan) <= hi,
               f"実際は {len(plan)} ステップ ({used})")

    for before, after in assertions.get("order", []):
        first_before = next((i for i, t in enumerate(used) if t in before), None)
        first_after = next((i for i, t in enumerate(used) if t in after), None)
        label = f"{'|'.join(before)} → {'|'.join(after)}"
        if first_before is None or first_after is None:
            # 片方が計画に無い場合は must_use_* 側で落ちる。順序としては判定不能。
            _check(checks, f"order: {label}", False,
                   fail_detail=f"順序を判定できない (計画: {used})")
        else:
            _check(checks, f"order: {label}", first_before < first_after,
                   fail_detail=f"順序が逆 (計画: {used})")

    for pattern in assertions.get("args_must_match", []):
        _check(checks, f"args_must_match: {pattern}",
               re.search(pattern, blob) is not None,
               fail_detail=f"args に一致なし: {blob[:200] or '(空)'}")

    for pattern in assertions.get("args_must_not_match", []):
        hit = re.search(pattern, blob)
        _check(checks, f"args_must_not_match: {pattern}", hit is None,
               fail_detail=f"禁止パターンが args に一致: '{hit.group(0)}'" if hit else "")

    return checks


# ---------------------------------------------------------
# 採点 (Tier B)
# ---------------------------------------------------------

def _mission_header(result: str) -> str:
    """本番の Discord 表示と同じ見出し判定を使う。

    判定を自前で書き直すと二重管理になって必ず割れる (2026-07-26 の教訓)。
    """
    from commands.bot_commands_agent import mission_header
    return mission_header(result)


def score_execution(q: dict, answer: str, learning_entry: dict = None) -> list:
    """実行結果 (最終回答テキスト + learning_db エントリ) を採点する純関数。"""
    exec_assertions = q.get("exec_assertions", {})
    checks = []

    for pattern in exec_assertions.get("answer_must_match", []):
        _check(checks, f"answer_must_match: {pattern}",
               re.search(pattern, answer) is not None,
               fail_detail=f"回答に一致なし: {answer[:200]}")

    for pattern in exec_assertions.get("answer_must_not_match", []):
        hit = re.search(pattern, answer)
        _check(checks, f"answer_must_not_match: {pattern}", hit is None,
               fail_detail=f"禁止パターンが回答に一致: '{hit.group(0)}'" if hit else "")

    if exec_assertions.get("expect_consistent_report"):
        # 「壊れているツールを踏ませる」型の判定は、そのツールが直った瞬間に
        # 前提が古くなって偽の不合格を出す (2026-07-27 に calendar/gmail で実際に起きた)。
        # そこで前提に依存しない不変条件を見る:
        #   learning_db が失敗と記録したなら、表示見出しも失敗でなければならない。
        # 失敗は inject_tool_failure で決定的に起こす。
        header = _mission_header(answer)
        if learning_entry is None:
            _check(checks, "learning_db に実行が記録される", False,
                   fail_detail="エントリが追記されていない — 表示との突き合わせができない")
        else:
            outcome = learning_entry.get("outcome")
            failed = outcome != "success"
            # 見出しは3状態 (✅ 完了 / ⚠️ 部分完了 / ❌ 失敗)。partial を ❌ に
            # 寄せると過剰判定になるので、見るのは「✅ でないこと」。
            reported = not header.startswith("✅")
            _check(checks, "learning_db と表示見出しが矛盾しない",
                   failed == reported,
                   fail_detail=f"outcome='{outcome}' なのに見出しは '{header}' "
                               f"— 失敗が成功として表示される")
            if exec_assertions.get("inject_tool_failure"):
                _check(checks, "注入した失敗が learning_db に失敗として残る", failed,
                       fail_detail=f"outcome='{outcome}' "
                                   f"— ツールが失敗したのに success で記録される")

    return checks


# ---------------------------------------------------------
# learning_db スナップショット (Tier B の副次観測)
# ---------------------------------------------------------

def _learning_db_count() -> int:
    try:
        from engine_plan_execute import _load_learning_db
        return len(_load_learning_db().get("executions", []))
    except Exception as e:
        print(f"[eval] ⚠️ learning_db を読めませんでした: {e}")
        return -1


def _learning_db_entry_after(before_count: int, query: str) -> dict:
    """実行によって追記された「この問の」エントリを返す。無ければ None。

    末尾を無条件に取らないのは、評価中も Bot / スケジューラが同じ learning_db に
    追記しうるため。他プロセスのエントリを掴むと、無関係な outcome で採点して
    しまう。log_execution は query を200字に切って保存するので、その前提で照合する。
    """
    if before_count < 0:
        return None
    try:
        from engine_plan_execute import _load_learning_db
        execs = _load_learning_db().get("executions", [])
        for entry in reversed(execs[before_count:]):
            if entry.get("query", "") == query[:200]:
                return entry
    except Exception as e:
        print(f"[eval] ⚠️ learning_db を読めませんでした: {e}")
    return None


# ---------------------------------------------------------
# 実行
# ---------------------------------------------------------

def run_question(q: dict, tools: set, full: bool, allow_writes: bool = False) -> dict:
    """1問を実行して結果 dict を返す。"""
    from engine_plan_execute import generate_plan

    result = {
        "id": q["id"],
        "category": q["category"],
        "query": q["query"],
        "note": q.get("note", ""),
        "status": "unknown",
        "plan": [],
        "checks": [],
        "exec_checks": [],
        "answer": "",
        "error": "",
    }

    missing = missing_required_tools(q, tools)
    if missing:
        result["status"] = "skipped"
        result["error"] = f"必要なツールが現在ロードされていない: {', '.join(missing)}"
        return result

    try:
        plan_data = generate_plan(q["query"], session_context=q.get("session_context", ""))
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    plan = plan_data.get("plan", []) or []
    result["plan"] = plan
    result["expected_outcome"] = plan_data.get("expected_outcome", "")
    result["checks"] = score_plan(q, plan, tools)

    if full and q.get("safe_exec") and q.get("exec_assertions"):
        from engine_plan_execute import run_plan_and_execute
        overrides = build_tier_b_overrides(q, allow_writes)
        result["blocked_tools"] = sorted(n for n in overrides if n in WRITE_TOOLS)
        result["injected_tools"] = list(q["exec_assertions"].get("inject_tool_failure", []))
        before = _learning_db_count()
        try:
            with patched_tools(overrides):
                answer = run_plan_and_execute(
                    q["query"], session_context=q.get("session_context", ""))
        except Exception as e:
            result["status"] = "error"
            result["error"] = f"実行時 {type(e).__name__}: {e}"
            return result
        result["answer"] = answer
        result["exec_checks"] = score_execution(
            q, answer, _learning_db_entry_after(before, q["query"]))
    elif q.get("exec_assertions"):
        # Tier B を回していない問は「Tier A だけ通った」ことしか言えない。
        # 合格表示だけ見て「失敗報告は正常」と読み違えないよう、明示的に残す。
        result["tier_b_pending"] = (
            "実行していない (--full 未指定)" if not full
            else "safe_exec=false のため実行しない")

    all_checks = result["checks"] + result["exec_checks"]
    result["status"] = "pass" if all(c["ok"] for c in all_checks) else "fail"
    return result


def select_questions(questions: list, only: str) -> list:
    if not only:
        return questions
    wanted = {s.strip().lower() for s in only.split(",") if s.strip()}
    return [q for q in questions
            if q["id"].lower() in wanted or q["category"].lower() in wanted]


def estimate_calls(questions: list, full: bool) -> int:
    calls = len(questions)
    if full:
        calls += sum(TIER_B_CALLS_PER_QUESTION for q in questions
                     if q.get("safe_exec") and q.get("exec_assertions"))
    return calls


def check_budget(estimated: int) -> str:
    """日次 Gemini 予算の残量を確認する。足りなければ理由文字列を返す。

    評価が本番の定時ジョブの予算を食い潰さないためのガード。
    新しいカウンタは足さず、既存の共有カウンタを参照するだけにする。
    """
    from lib.cost_guard import GEMINI_LIGHT_COUNTER, GEMINI_HEAVY_COUNTER
    # LIGHT/HEAVYはモデル別の別枠カウンタ (2026-08-17分離)。評価はどちらの
    # モデルも使いうるため、簡易な見積りとして合算する。
    limit = GEMINI_LIGHT_COUNTER.limit + GEMINI_HEAVY_COUNTER.limit
    if limit <= 0:
        return ""  # 両方とも上限無効 (キルスイッチ) — ガードしない
    used = GEMINI_LIGHT_COUNTER.current() + GEMINI_HEAVY_COUNTER.current()
    remaining = limit - used
    if estimated > remaining:
        return (f"日次 Gemini 予算が不足: 見積り {estimated}回 / 残り {remaining}回 "
                f"({used}/{limit} 使用済み)。--only で絞るか明日実行してください。")
    return ""


# ---------------------------------------------------------
# レポート
# ---------------------------------------------------------

def summarize(results: list) -> dict:
    by_category = {}
    for r in results:
        bucket = by_category.setdefault(r["category"], {"pass": 0, "fail": 0,
                                                        "skipped": 0, "error": 0})
        bucket[r["status"]] = bucket.get(r["status"], 0) + 1
    graded = [r for r in results if r["status"] in ("pass", "fail")]
    passed = [r for r in graded if r["status"] == "pass"]
    return {
        "total": len(results),
        "graded": len(graded),
        "passed": len(passed),
        "pass_rate": (len(passed) / len(graded) * 100) if graded else 0.0,
        "by_category": by_category,
    }


def render_report(results: list, meta: dict) -> str:
    summary = summarize(results)
    lines = [
        "# Agent Eval Report",
        "",
        f"- 実行日時: {meta['timestamp']}",
        f"- モデル: `{meta['model']}`",
        f"- モード: {'Tier A + Tier B (--full)' if meta['full'] else 'Tier A (計画のみ)'}"
        + ("" if not meta['full'] else
           ("  書き込み系ツール: **解除 (--allow-writes)**" if meta.get('allow_writes')
            else "  書き込み系ツール: ブロック済み")),
        f"- 評価セット: `{meta['eval_set']}` (version {meta['version']})",
        f"- Gemini 呼び出し: 見積り {meta['estimated_calls']} / 実測 {meta['actual_calls']} "
        f"(共有カウンタの差分。評価中に Bot / スケジューラが使った分も含む)",
        "",
        f"## 総合: {summary['passed']}/{summary['graded']} 合格 "
        f"({summary['pass_rate']:.0f}%)",
        "",
        "| カテゴリ | 合格 | 不合格 | スキップ | エラー |",
        "|---|---|---|---|---|",
    ]
    for category, bucket in sorted(summary["by_category"].items()):
        lines.append(f"| {category} | {bucket.get('pass', 0)} | {bucket.get('fail', 0)} "
                     f"| {bucket.get('skipped', 0)} | {bucket.get('error', 0)} |")

    lines += ["", "## 問別の結果", "",
              "| ID | カテゴリ | 結果 | 質問 |", "|---|---|---|---|"]
    icon = {"pass": "✅", "fail": "❌", "skipped": "⏭️", "error": "💥"}
    for r in results:
        query = r["query"].replace("|", "\\|")[:50]
        mark = icon.get(r["status"], "?")
        if r.get("tier_b_pending"):
            mark += "*"
        lines.append(f"| {r['id']} | {r['category']} | {mark} | {query} |")

    pending = [r for r in results if r.get("tier_b_pending")]
    if pending:
        lines += ["", "\\* Tier B (実行+回答品質) 未評価 — 計画だけを見た結果です:", ""]
        for r in pending:
            lines.append(f"- {r['id']}: {r['tier_b_pending']}")

    failing = [r for r in results if r["status"] in ("fail", "error")]
    if failing:
        lines += ["", "## 不合格の根拠", ""]
        for r in failing:
            lines += [f"### {icon.get(r['status'])} {r['id']} — {r['query']}", "",
                      f"**この問がある理由**: {r['note']}", ""]
            if r["error"]:
                lines += [f"**エラー**: {r['error']}", ""]
            if r.get("injected_tools"):
                lines += [f"**意図的に失敗させたツール**: "
                          f"{', '.join(r['injected_tools'])}", ""]
            if r["plan"]:
                lines.append("**生成された計画**:")
                lines.append("")
                for item in r["plan"]:
                    lines.append(f"{item.get('step', '?')}. `{item.get('tool', '?')}` "
                                 f"— {item.get('task', '')} "
                                 f"`args_json={item.get('args_json', '') or '(空)'}`")
                lines.append("")
            failed_checks = [c for c in r["checks"] + r["exec_checks"] if not c["ok"]]
            if failed_checks:
                lines.append("**外れたアサーション**:")
                lines.append("")
                for c in failed_checks:
                    lines.append(f"- `{c['name']}` — {c['detail']}")
                lines.append("")
            if r["answer"]:
                lines += ["**最終回答(先頭300字)**:", "", "```",
                          r["answer"][:300], "```", ""]

    skipped = [r for r in results if r["status"] == "skipped"]
    if skipped:
        lines += ["", "## スキップ", ""]
        for r in skipped:
            lines.append(f"- {r['id']}: {r['error']}")

    return "\n".join(lines) + "\n"


def render_comparison(current: list, baseline: list) -> str:
    """before/after の差分表。合否が変わった問だけを並べる。"""
    base_by_id = {r["id"]: r["status"] for r in baseline}
    rows = []
    for r in current:
        was = base_by_id.get(r["id"], "-")
        now = r["status"]
        if was != now:
            rows.append((r["id"], r["category"], was, now))
    if not rows:
        return "\n## ベースラインとの差分\n\n合否が変わった問はありません。\n"
    lines = ["", "## ベースラインとの差分", "",
             "| ID | カテゴリ | before | after |", "|---|---|---|---|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------
# CLI
# ---------------------------------------------------------

def cmd_dry_run(questions: list, tools: set, full: bool) -> int:
    print(f"[eval] ツールレジストリ: {len(tools)} 件ロード済み")
    problems = 0
    for q in questions:
        issues = validate_question(q, tools)
        missing = missing_required_tools(q, tools)
        if issues:
            problems += len(issues)
            print(f"  ❌ {q['id']}: " + " / ".join(issues))
        elif missing:
            print(f"  ⏭️ {q['id']}: 実行時スキップ予定 ({', '.join(missing)})")
        else:
            print(f"  ✅ {q['id']} ({q['category']})")

    ids = [q["id"] for q in questions]
    if len(set(ids)) != len(ids):
        problems += 1
        print("  ❌ ID が重複しています")

    tier_b = [q["id"] for q in questions if q.get("safe_exec") and q.get("exec_assertions")]
    print(f"[eval] Tier B 対象 ({len(tier_b)}問): {', '.join(tier_b) or 'なし'}")
    print(f"[eval] Gemini 見積り: {estimate_calls(questions, full)} 回 (dry-run では0回)")
    print(f"[eval] {'❌ 問題あり' if problems else '✅ 設定は健全です'}")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="エージェント回答品質の評価セットを実行する")
    parser.add_argument("--full", action="store_true",
                        help="Tier B (実行+回答品質) も走らせる。safe_exec=true の問のみ")
    parser.add_argument("--allow-writes", action="store_true",
                        help="Tier B で書き込み系ツールのブロックを解除する "
                             "(実 Vault / 外部サービスに書き込みが残る)")
    parser.add_argument("--only", default="",
                        help="ID またはカテゴリのカンマ区切り (例: T06,T07 / negation)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Gemini を叩かず設定の整合だけ検証する")
    parser.add_argument("--compare", default="",
                        help="比較するベースライン JSON のパス")
    parser.add_argument("--eval-set", default=EVAL_SET_PATH)
    args = parser.parse_args()

    data = load_eval_set(args.eval_set)
    questions = select_questions(data["questions"], args.only)
    if not questions:
        print(f"[eval] ❌ --only '{args.only}' に一致する問がありません")
        return 1

    tools = known_tools()

    if args.dry_run:
        return cmd_dry_run(questions, tools, args.full)

    # dry-run 相当の静的検証は実行前にも必ず通す (壊れた期待値のまま課金しない)
    broken = [(q["id"], validate_question(q, tools)) for q in questions]
    broken = [(qid, issues) for qid, issues in broken if issues]
    if broken:
        print("[eval] ❌ 評価セットの設定に問題があります (--dry-run で詳細を確認してください)")
        for qid, issues in broken:
            print(f"  {qid}: " + " / ".join(issues))
        return 1

    estimated = estimate_calls(questions, args.full)
    shortage = check_budget(estimated)
    if shortage:
        print(f"[eval] ⛔ {shortage}")
        return 1

    from lib.cost_guard import GEMINI_LIGHT_COUNTER, GEMINI_HEAVY_COUNTER
    from lib.lib_shared import MODEL_NAME
    calls_before = GEMINI_LIGHT_COUNTER.current() + GEMINI_HEAVY_COUNTER.current()

    print(f"[eval] {len(questions)}問 / モード={'full' if args.full else 'plan-only'} "
          f"/ Gemini 見積り {estimated}回")

    results = []
    t_start = time.time()
    for i, q in enumerate(questions, 1):
        print(f"[eval] ({i}/{len(questions)}) {q['id']} {q['query'][:40]}...")
        r = run_question(q, tools, args.full, allow_writes=args.allow_writes)
        icon = {"pass": "✅", "fail": "❌", "skipped": "⏭️", "error": "💥"}
        failed = [c["name"] for c in r["checks"] + r["exec_checks"] if not c["ok"]]
        print(f"        {icon.get(r['status'], '?')} {r['status']}"
              + (f" — {'; '.join(failed)}" if failed else "")
              + (f" — {r['error']}" if r["error"] else ""))
        results.append(r)

    meta = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": MODEL_NAME,
        "full": args.full,
        "allow_writes": args.allow_writes,
        "eval_set": os.path.basename(args.eval_set),
        "version": data.get("version"),
        "estimated_calls": estimated,
        "actual_calls": (GEMINI_LIGHT_COUNTER.current() + GEMINI_HEAVY_COUNTER.current())
                        - calls_before,
        "duration_seconds": round(time.time() - t_start, 1),
    }

    report = render_report(results, meta)
    if args.compare:
        with open(args.compare, encoding='utf-8') as f:
            report += render_comparison(results, json.load(f)["results"])

    os.makedirs(EVAL_OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    md_path = os.path.join(EVAL_OUT_DIR, f"eval_{stamp}.md")
    json_path = os.path.join(EVAL_OUT_DIR, f"eval_{stamp}.json")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(report)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({"meta": meta, "results": results}, f, ensure_ascii=False, indent=2)

    summary = summarize(results)
    print("\n" + "=" * 60)
    print(f"合格 {summary['passed']}/{summary['graded']} ({summary['pass_rate']:.0f}%)"
          f"  Gemini 実測 {meta['actual_calls']}回  {meta['duration_seconds']}秒")
    for category, bucket in sorted(summary["by_category"].items()):
        print(f"  {category:16s} pass={bucket.get('pass', 0)} fail={bucket.get('fail', 0)} "
              f"skip={bucket.get('skipped', 0)} err={bucket.get('error', 0)}")
    print(f"レポート: {md_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
