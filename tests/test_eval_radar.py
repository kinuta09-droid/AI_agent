# -*- coding: utf-8 -*-
"""夜間の自己評価レーダー (scripts/eval_radar.py) のテスト

このテストが存在する理由:
  レーダーは「品質が落ちたことに気付くための仕組み」なので、レーダー自身が
  壊れていると二重に気付けなくなる。特に危ないのは次の3つ:
    (a) 測れていないのに成功として扱う → 監視が消えたことに気付けない
    (b) 汚れた数値を履歴に混ぜる → 以後の比較が恒久的に嘘になる
    (c) 設定変更を「劣化」と誤報する → 通知そのものが信用されなくなる
  そのため「測れなかったケース」を網羅的に持ち、履歴に1行も増えないことを
  実ファイルで確認する。

実行: py tests/test_eval_radar.py
"""
import ast
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.bot_errors import is_failure_result
from scripts import eval_radar as radar

# 本番の実ファイル。テストが1バイトでも触っていないことを最後に確認する
# (Known Bug Patterns C: skill_radar_latest.json をテストが汚した事故と同型)。
_REAL_HISTORY = radar.HISTORY_PATH
_REAL_STATE = radar.STATE_PATH
_REAL_EVAL_DIR = radar.EVAL_DIR


def _fingerprint(path: str):
    return os.path.getsize(path) if os.path.exists(path) else None


_BEFORE = (_fingerprint(_REAL_HISTORY), _fingerprint(_REAL_STATE))

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


# ---------------------------------------------------------
# 一時環境
# ---------------------------------------------------------

class Sandbox:
    """HISTORY_PATH / STATE_PATH / EVAL_DIR を一時ディレクトリへ退避する。

    **3つとも直接差し替える**のが要点。これらは import 時に LOGS_DIR から
    計算済みの別定数なので、LOGS_DIR だけ書き換えても連動しない。
    """

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="eval_radar_test_")
        self._saved = (radar.EVAL_DIR, radar.HISTORY_PATH, radar.STATE_PATH,
                       radar.subprocess, radar._notify, radar.budget_shortage)
        radar.EVAL_DIR = os.path.join(self.tmp, "eval")
        radar.HISTORY_PATH = os.path.join(radar.EVAL_DIR, "history.jsonl")
        radar.STATE_PATH = os.path.join(radar.EVAL_DIR, "radar_state.json")
        os.makedirs(radar.EVAL_DIR, exist_ok=True)
        self.notices = []
        radar._notify = lambda msg: self.notices.append(msg)
        radar.budget_shortage = lambda estimated: ""  # 予算は別テストで見る
        return self

    def __exit__(self, *exc):
        (radar.EVAL_DIR, radar.HISTORY_PATH, radar.STATE_PATH,
         radar.subprocess, radar._notify, radar.budget_shortage) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def history(self):
        return radar.load_history()

    def fake_runs(self, payloads: dict, returncode: int = 0, produce: bool = True,
                  stdout: str = ""):
        """subprocess を差し替え、指定した JSON を logs/eval 相当へ書かせる。

        radar.subprocess をモジュールごと差し替える (subprocess.run を直接
        書き換えると、同じプロセスで動く他のテストにまで影響する)。
        """
        def _run(cmd, **kwargs):
            script = os.path.basename(cmd[1])
            kind = "retrieval" if "retrieval" in script else "agent"
            if produce and kind in payloads:
                prefix = radar._SCRIPTS[kind]["prefix"]
                path = os.path.join(radar.EVAL_DIR, f"{prefix}20260824_0400.json")
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(payloads[kind], f, ensure_ascii=False)
            return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        radar.subprocess = types.SimpleNamespace(
            run=_run, TimeoutExpired=subprocess.TimeoutExpired)


def retrieval_payload(hit=0.82, mrr=0.61, clean=True, rerank=True):
    return {
        "timestamp": "2026-08-24T04:00:00",
        "config": {"multiquery": True, "rerank": rerank, "loop": True,
                   "link_expand": False, "rerank_backend": "gemini",
                   "fixture": "retrieval_eval.json"},
        "arm_clean": clean,
        "metrics": {"hit@5": hit, "mrr@10": mrr, "hits": 14, "total": 17,
                    "mean_latency_sec": 3.2, "degraded": 0 if clean else 4},
        "results": [],
    }


def agent_payload(passed=17, graded=20, full=False):
    results = ([{"id": f"T{i:02d}", "category": "plan", "status": "pass"} for i in range(passed)]
               + [{"id": f"F{i:02d}", "category": "plan", "status": "fail"}
                  for i in range(graded - passed)])
    return {"meta": {"model": "gemini-2.5-flash", "full": full,
                     "eval_set": "agent_eval_set.json", "version": 3,
                     "actual_calls": 20, "duration_seconds": 120.0},
            "results": results}


NOW = datetime.datetime(2026, 8, 24, 4, 0, 0)


# ---------------------------------------------------------
# [1] 閾値判定の境界
# ---------------------------------------------------------

def test_evaluate_boundaries():
    print("\n[1] evaluate() の境界")
    prior = [0.80, 0.82, 0.80]
    v, _ = radar.evaluate(0.70, prior, floor=0.70, max_drop=0.13)
    check("下限ちょうどは OK 側", v == "ok", v)
    v, d = radar.evaluate(0.6999, prior, floor=0.70, max_drop=0.13)
    check("下限を1つ下回れば breach_floor", v == "breach_floor", f"{v} {d}")
    # 中央値 0.80 / 許容 0.13 → 0.67 ちょうどは許容、0.66 は割れ
    v, _ = radar.evaluate(0.67, prior, floor=None, max_drop=0.13)
    check("許容下落ちょうどは OK 側", v == "ok", v)
    v, d = radar.evaluate(0.66, prior, floor=None, max_drop=0.13)
    check("許容を超える下落は breach_drop", v == "breach_drop", f"{v} {d}")
    check("breach_drop の説明に今回値と中央値が入る",
          "0.66" in d and "0.8" in d, d)
    # 上振れは常に OK
    v, _ = radar.evaluate(0.95, prior, floor=0.70, max_drop=0.13)
    check("改善は OK", v == "ok", v)
    # 下限未設定 (None) は下限判定をしない
    v, _ = radar.evaluate(0.10, [0.10, 0.10], floor=None, max_drop=0.13)
    check("下限未設定なら絶対判定をしない", v == "ok", v)


# ---------------------------------------------------------
# [2] 過去が足りないときは通知しない
# ---------------------------------------------------------

def test_baseline_is_not_an_alarm():
    print("\n[2] 履歴が足りないときは baseline (誤報しない)")
    for prior in ([], [0.80]):
        v, _ = radar.evaluate(0.40, prior, floor=None, max_drop=0.13)
        check(f"過去{len(prior)}件なら baseline", v == "baseline", v)
    # ただし人間が決めた絶対下限は初回から効く
    v, _ = radar.evaluate(0.40, [], floor=0.70, max_drop=0.13)
    check("過去0件でも絶対下限は効く", v == "breach_floor", v)


# ---------------------------------------------------------
# [3] 設定が変われば比較しない
# ---------------------------------------------------------

def test_config_key_isolates_baselines():
    print("\n[3] config_key が違う実行は比較に使わない")
    on = {"multiquery": True, "rerank": True}
    off = {"multiquery": True, "rerank": False}
    k_on, k_off = radar.config_key("retrieval", on), radar.config_key("retrieval", off)
    check("設定が違えば鍵も違う", k_on != k_off, f"{k_on} {k_off}")
    check("同じ設定なら鍵は安定 (dictの順序に依存しない)",
          radar.config_key("retrieval", {"rerank": True, "multiquery": True}) == k_on)
    check("kind が違えば鍵も違う",
          radar.config_key("agent", on) != k_on)
    history = [{"kind": "retrieval", "config_key": k_on, "metrics": {"hit@5": 0.82}},
               {"kind": "retrieval", "config_key": k_on, "metrics": {"hit@5": 0.80}},
               {"kind": "retrieval", "config_key": k_off, "metrics": {"hit@5": 0.55}}]
    check("鍵で絞り込める", radar.prior_values(history, "retrieval", k_on, "hit@5") == [0.82, 0.80])
    v, _ = radar.evaluate(0.55, radar.prior_values(history, "retrieval", k_off, "hit@5"),
                          floor=None, max_drop=0.13)
    check("設定を変えた初回は劣化と報告しない", v == "baseline", v)


# ---------------------------------------------------------
# [4] 測れなかったケースは履歴に残さない
# ---------------------------------------------------------

def _unmeasured_case(name, setup):
    with Sandbox() as box:
        setup(box)
        result = radar.run_eval_radar(now=NOW)
        rows = box.history()
        check(f"{name}: 履歴に追記しない", rows == [], f"{rows}")
        check(f"{name}: 失敗として返る", is_failure_result(result), result)
        check(f"{name}: 通知が出る", any("測定できませんでした" in m for m in box.notices),
              str(box.notices))


def test_unmeasured_never_recorded():
    print("\n[4] 測れなかったケース (契約2・3)")

    def exit_nonzero(box):
        box.fake_runs({"retrieval": retrieval_payload(), "agent": agent_payload()},
                      returncode=1, stdout="[ERROR] semantic index がありません")
    _unmeasured_case("前提不足 (exit 1)", exit_nonzero)

    def no_json(box):
        box.fake_runs({}, returncode=0, produce=False, stdout="なにも書かなかった")
    _unmeasured_case("JSON が生成されない", no_json)

    def dirty_arm(box):
        box.fake_runs({"retrieval": retrieval_payload(clean=False),
                       "agent": agent_payload()})
    with Sandbox() as box:
        dirty_arm(box)
        result = radar.run_eval_radar(now=NOW)
        rows = box.history()
        check("混合アーム: retrieval は履歴に残らない",
              all(r["kind"] != "retrieval" for r in rows), str(rows))
        check("混合アーム: 健全な agent 側は記録される",
              any(r["kind"] == "agent" for r in rows), str(rows))
        check("混合アーム: 失敗として返る", is_failure_result(result), result)
        check("混合アーム: 理由が arm_clean に言及する",
              any("arm_clean" in m for m in box.notices), str(box.notices))

    with Sandbox() as box:
        box.fake_runs({"agent": agent_payload(full=True)})
        out = radar._run_one("agent")
        check("Tier B の結果は受け付けない", out["status"] == "unmeasured", str(out))

    with Sandbox() as box:
        def _raise(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1800)
        radar.subprocess = types.SimpleNamespace(
            run=_raise, TimeoutExpired=subprocess.TimeoutExpired)
        out = radar._run_one("retrieval")
        check("タイムアウトは unmeasured", out["status"] == "unmeasured", str(out))
        check("タイムアウトの理由が読める", "タイムアウト" in out["reason"], out["reason"])

    with Sandbox() as box:
        box.fake_runs({"agent": {"meta": {"full": False}, "results": [
            {"id": "S1", "category": "plan", "status": "skipped"}]}})
        out = radar._run_one("agent")
        check("全問スキップは unmeasured", out["status"] == "unmeasured", str(out))


# ---------------------------------------------------------
# [5] 戻り値の契約 (catch_up との結合)
# ---------------------------------------------------------

def test_return_value_contract():
    print("\n[5] 戻り値の契約 (agent_scheduler の catch_up と噛み合うか)")
    with Sandbox() as box:
        box.fake_runs({"retrieval": retrieval_payload(), "agent": agent_payload()})
        result = radar.run_eval_radar(now=NOW)
        check("初回 (baseline) は失敗ではない", not is_failure_result(result), result)
        check("初回は履歴が2行", len(box.history()) == 2, str(box.history()))
        check("初回はベースライン通知が出る",
              any("ベースライン" in m for m in box.notices), str(box.notices))

        # 2回目・3回目 (同じ値) を積んでから、劣化した4回目を測る
        for i, when in enumerate([NOW + datetime.timedelta(days=7),
                                  NOW + datetime.timedelta(days=14)]):
            box.fake_runs({"retrieval": retrieval_payload(), "agent": agent_payload()})
            radar.run_eval_radar(now=when)
        box.notices.clear()
        box.fake_runs({"retrieval": retrieval_payload(hit=0.55, mrr=0.30),
                       "agent": agent_payload()})
        result = radar.run_eval_radar(now=NOW + datetime.timedelta(days=21))
        check("劣化は失敗接頭辞を付けない (測定自体は成功)",
              not is_failure_result(result), result)
        check("劣化は空文字でもない (ログに残る)", bool(result), repr(result))
        check("劣化時は履歴に記録される (推移が途切れない)",
              len(box.history()) == 8, str(len(box.history())))
        check("劣化通知が飛ぶ", any("劣化" in m for m in box.notices), str(box.notices))
        check("通知に指標名が入る", any("hit@5" in m for m in box.notices), str(box.notices))

    with Sandbox() as box:
        box.fake_runs({"retrieval": retrieval_payload(), "agent": agent_payload()})
        radar.budget_shortage = lambda estimated: "残り 10回"
        result = radar.run_eval_radar(now=NOW)
        check("予算不足は失敗として返る (= 繰り越される)", is_failure_result(result), result)
        check("予算不足では eval を起動しない", box.history() == [], str(box.history()))


# ---------------------------------------------------------
# [6] 停止検知
# ---------------------------------------------------------

def test_staleness():
    print("\n[6] レーダー自身の停止検知")
    hist = [{"timestamp": (NOW - datetime.timedelta(days=9)).isoformat(),
             "kind": "agent", "metrics": {}}]
    check("9日前なら正常", radar.check_staleness(hist, NOW, 10) == "")
    hist = [{"timestamp": (NOW - datetime.timedelta(days=11)).isoformat(),
             "kind": "agent", "metrics": {}}]
    reason = radar.check_staleness(hist, NOW, 10)
    check("11日前なら停滞と判定", bool(reason), reason)
    check("停滞理由に日数が入る", "11日" in reason, reason)
    check("履歴も導入日も無ければ判定しない", radar.check_staleness([], NOW, 10) == "")
    reason = radar.check_staleness([], NOW, 10,
                                   installed_at=(NOW - datetime.timedelta(days=30)).isoformat())
    check("一度も測定できていない状態も検知する", bool(reason), reason)

    with Sandbox() as box:
        check("初回は基準を作るだけで通知しない", radar.run_eval_staleness(now=NOW) == "")
        radar.append_history({"timestamp": (NOW - datetime.timedelta(days=30)).isoformat(),
                              "kind": "agent", "config_key": "x", "metrics": {"pass_rate": 0.8}})
        result = radar.run_eval_staleness(now=NOW)
        check("停滞は失敗として返る", is_failure_result(result), result)
        check("停滞を通知する", any("途絶え" in m for m in box.notices), str(box.notices))
        box.notices.clear()
        radar.run_eval_staleness(now=NOW + datetime.timedelta(hours=1))
        check("同じ理由を短時間に再通知しない", box.notices == [], str(box.notices))


# ---------------------------------------------------------
# [7] 通知の重複抑止
# ---------------------------------------------------------

def test_notice_cooldown():
    print("\n[7] 通知の重複抑止 (再起動のたびの連投を防ぐ)")
    state = {}
    check("初回は通知する", radar.should_notify(state, "budget", NOW))
    check("直後の同一理由は抑止", not radar.should_notify(state, "budget", NOW))
    check("別の理由は抑止しない", radar.should_notify(state, "retrieval:unmeasured", NOW))
    later = NOW + datetime.timedelta(hours=radar._NOTICE_COOLDOWN_HOURS + 1)
    check("時間が経てば再通知する", radar.should_notify(state, "budget", later))


# ---------------------------------------------------------
# [8] 実資源の隔離 (本番 logs/eval を触っていないか)
# ---------------------------------------------------------

def test_production_files_untouched():
    print("\n[8] 本番の logs/eval を汚していないか")
    after = (_fingerprint(_REAL_HISTORY), _fingerprint(_REAL_STATE))
    check("本番 history.jsonl / radar_state.json に変化なし", after == _BEFORE,
          f"before={_BEFORE} after={after}")
    check("モジュール定数が元に戻っている",
          (radar.HISTORY_PATH, radar.STATE_PATH, radar.EVAL_DIR)
          == (_REAL_HISTORY, _REAL_STATE, _REAL_EVAL_DIR))
    check("subprocess モジュールが元に戻っている",
          radar.subprocess is subprocess, str(radar.subprocess))


# ---------------------------------------------------------
# [9] 静的検査 — 判定の作法が将来の編集で崩れないように縛る
# ---------------------------------------------------------

def _module_ast():
    path = os.path.join(_PROJECT_ROOT, "scripts", "eval_radar.py")
    with open(path, encoding='utf-8') as f:
        return ast.parse(f.read())


def _calls_in(node) -> set:
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_static_judgement_rules():
    print("\n[9] 静的検査 (終了コードだけで判定していないか)")
    tree = _module_ast()
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    # (a) status="measured" を**作る**関数は、必ず JSON を読み直していること。
    #     eval_agent は全問不合格でも exit 0 を返すため、終了コードだけの判定は
    #     「表示と実態の食い違い」を新規に作り込むことになる。
    #     ("measured" と比較するだけの関数 (compose_message 等) は対象外。
    #      判定を作る側と読む側を区別しないと、検査が形骸化して外される)
    producers = []
    for name, fn in funcs.items():
        for node in ast.walk(fn):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "status"
                        and isinstance(v, ast.Constant) and v.value == "measured"):
                    producers.append(name)
    check("status='measured' を作る関数が実在する", producers, "検査が空振りしている")
    for name in set(producers):
        check(f"{name} は JSON を読み直してから measured を名乗る",
              "_read_json" in _calls_in(funcs[name]), sorted(_calls_in(funcs[name])))

    # (b) 判定は _run_one に閉じる。上位が returncode を見始めたら二重判定になる。
    for name in ("run_eval_radar", "run_eval_staleness"):
        uses_rc = any(isinstance(n, ast.Attribute) and n.attr == "returncode"
                      for n in ast.walk(funcs[name]))
        check(f"{name} は returncode を直接見ない", not uses_rc)

    # (c) 予算確認は fail-closed。読めなかったときに "" (=OK) を返さないこと。
    budget = funcs["budget_shortage"]
    bad = [h for t in ast.walk(budget) if isinstance(t, ast.Try) for h in t.handlers
           for n in ast.walk(h)
           if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
           and n.value.value == ""]
    check("budget_shortage は確認不能を OK に倒さない", not bad)

    # (d) --full を自動実行しない (契約4)。実 Vault への書き込みを無人ジョブに
    #     持ち込まないための最後の砦。
    src = open(os.path.join(_PROJECT_ROOT, "scripts", "eval_radar.py"),
               encoding='utf-8').read()
    args_lists = [s["args"] for s in radar._SCRIPTS.values()]
    check("_SCRIPTS の引数に --full が無い",
          not any("--full" in a for a in args_lists), str(args_lists))
    check("--allow-writes も渡していない", "--allow-writes" not in src)


# ---------------------------------------------------------
# [10] スケジューラとの結線
# ---------------------------------------------------------

def test_scheduler_wiring():
    print("\n[10] agent_scheduler への登録")
    path = os.path.join(_PROJECT_ROOT, "agent_scheduler.py")
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    schedule = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SCHEDULE" for t in node.targets):
            schedule = node.value
    check("SCHEDULE が見つかる", schedule is not None)
    entries = {}
    for el in ast.walk(schedule):
        if not isinstance(el, ast.Dict):
            continue
        item = {}
        for k, v in zip(el.keys, el.values):
            if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                item[k.value] = v.value
            elif isinstance(k, ast.Constant) and isinstance(v, ast.Name):
                item[k.value] = v.id
        if "name" in item:
            entries[item["name"]] = item
    for name, weekday in (("Eval Radar (Weekly)", 6), ("Eval Radar Staleness", 0)):
        check(f"{name} が登録されている", name in entries, str(sorted(entries)))
        if name not in entries:
            continue
        item = entries[name]
        check(f"{name}: silent (正常時は無言)", item.get("silent") is True, str(item))
        check(f"{name}: catch_up (PC 停止中の週を繰り越す)",
              item.get("catch_up") is True, str(item))
        check(f"{name}: weekday={weekday}", item.get("weekday") == weekday, str(item))


if __name__ == "__main__":
    print("=" * 60)
    print("Eval Radar テスト")
    print("=" * 60)
    test_evaluate_boundaries()
    test_baseline_is_not_an_alarm()
    test_config_key_isolates_baselines()
    test_unmeasured_never_recorded()
    test_return_value_contract()
    test_staleness()
    test_notice_cooldown()
    test_static_judgement_rules()
    test_scheduler_wiring()
    test_production_files_untouched()
    print("=" * 60)
    print(f"PASS: {PASS} / FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)
