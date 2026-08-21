# -*- coding: utf-8 -*-
r"""
夜間の自己評価レーダー — 検索品質とエージェント計画品質の週次回帰テスト

なぜこれが要るか:
  scripts/eval_retrieval.py と scripts/eval_agent.py は手動実行のみで、
  logs/eval/ には過去の測定が1件も残っていなかった。検索チューニングや Planner
  プロンプトの変更で品質が落ちても、例外は出ず・ログにも一行も残らない
  (Known Bug Patterns A の「無言の死」)。ここで週次の自動計測と閾値監視を入れる。

このモジュールが守る契約 (壊すと機能そのものが嘘になる):
  1. **終了コードだけで成否を判定しない**。scripts/eval_agent.py は 20問すべてが
     不合格でも exit 0 を返す。判定は必ず書き出された JSON を読み直して行う。
  2. **汚れた測定値を履歴に混ぜない**。eval_retrieval の arm_clean=false は
     「施策ONのつもりが途中でフォールバックした混合アーム」であり、これを
     ベースラインに混ぜると以後の比較が恒久的に嘘になる。
  3. **測れなかったことを黙って成功にしない**。前提不足・予算不足・タイムアウトは
     戻り値を "Error: ..." にする。agent_scheduler の catch_up は失敗を記録しない
     ため、PC が落ちていた週や測れなかった週は**次回起動時に自動で繰り越される**。
  4. **Tier B (eval_agent --full) は自動実行しない**。実ツールを叩くため、無人の
     定時ジョブに実 Vault への書き込みの可能性を持たせない
     (Known Bug Patterns C の混入事故2件と同型を避ける)。

使い方:
  py scripts/eval_radar.py --dry-run     # 何をどれだけ消費するか (API 0回)
  py scripts/eval_radar.py --report      # これまでの推移を表示 (API 0回)
  py scripts/eval_radar.py               # 実測 (Gemini 約54回)
  py scripts/eval_radar.py --only agent  # 片方だけ
  py scripts/eval_radar.py --staleness   # レーダー自身の停止検知 (API 0回)

.env のノブ (すべて任意。閾値は未設定なら「絶対下限の判定をしない」):
  EVAL_RADAR_MIN_HIT5             Hit@5 の絶対下限        (例 0.70)
  EVAL_RADAR_MIN_MRR10            MRR@10 の絶対下限       (例 0.55)
  EVAL_RADAR_MIN_AGENT_PASS_RATE  計画合格率の絶対下限     (0-1 で書く。例 0.80)
  EVAL_RADAR_MAX_DROP             直近中央値からの許容下落 (既定 0.13)
  EVAL_RADAR_MIN_RESERVE          実行後に残す Gemini 予算 (既定 150)
  EVAL_RADAR_SKIP_RETRIEVAL       1 で検索評価を回さない   (Ollama 非常駐運用の逃げ道)
  EVAL_RADAR_STALE_DAYS           推移が途絶えたと見なす日数 (既定 10)
  EVAL_RADAR_RETRY_COOLDOWN_HOURS 測定失敗後の再試行間隔   (既定 6)
"""
import argparse
import datetime
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time

# LEVEL 0 (このハンドルだけ)。スケジューラから import されるモジュールなので、
# stdout が差し替えられている/存在しない環境でも import 自体は落とさない。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.paths import LOGS_DIR

# 注意 (Known Bug Patterns C): 以下は import 時に LOGS_DIR から計算される定数で、
# 後から LOGS_DIR を差し替えても連動しない。テストは **この3つを直接** 一時パスへ
# 退避すること (skill_radar_latest.json の本番汚染と同型の事故を避ける)。
EVAL_DIR = os.path.join(LOGS_DIR, "eval")
HISTORY_PATH = os.path.join(EVAL_DIR, "history.jsonl")
STATE_PATH = os.path.join(EVAL_DIR, "radar_state.json")

# 実行するハーネス。prefix は「今回の実行で新しく生まれた JSON」を見分けるため。
_SCRIPTS = {
    "retrieval": {
        "script": os.path.join("scripts", "eval_retrieval.py"),
        "args": ["--label", "radar"],
        "prefix": "retrieval_",
        "timeout": 1800,
    },
    "agent": {
        # --full は付けない (契約4)。付けると実ツールが動く。
        "script": os.path.join("scripts", "eval_agent.py"),
        "args": [],
        "prefix": "eval_",
        "timeout": 1200,
    },
}

# Gemini 見積り。retrieval は 17問 × 最大2回、agent (Tier A) は 20問 × 1回。
# 予算予約ガードは「多めに見積もって遠慮する」側に倒す。
ESTIMATED_CALLS = {"retrieval": 34, "agent": 20}

# 監視する指標と、その絶対下限を指定する .env のキー。
# **すべて 0-1 に正規化して扱う** — eval_agent の summarize() は pass_rate を
# 0-100 で返すので取り込み時に /100 する。単位が混ざると MAX_DROP の意味が壊れる。
METRIC_SPECS = {
    "retrieval": [("hit@5", "EVAL_RADAR_MIN_HIT5"), ("mrr@10", "EVAL_RADAR_MIN_MRR10")],
    "agent": [("pass_rate", "EVAL_RADAR_MIN_AGENT_PASS_RATE")],
}

_EPS = 1e-9
_NOTICE_COOLDOWN_HOURS = 20  # 同じ理由の通知を再送しない間隔 (再起動のたびの連投を防ぐ)


# ---------------------------------------------------------
# .env ノブ
# ---------------------------------------------------------

def _env_float(name: str, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[radar] ⚠️ {name}='{raw}' は数値として読めません — 既定 {default} を使います")
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def floor_for(env_key: str):
    """絶対下限。未設定なら None (= 下限の判定をしない)。

    既定値を code 側に持たない理由: 推測で書いた閾値は「実測より甘ければ永久に
    鳴らない / 厳しければ初回から鳴りっぱなし」のどちらかにしかならない。
    ブートストラップ実行の実測値から人間が決める。
    """
    return _env_float(env_key, None)


# ---------------------------------------------------------
# 履歴 (純関数 — テストから直接呼べる)
# ---------------------------------------------------------

def config_key(kind: str, config: dict) -> str:
    """比較可能性の鍵。設定が違う実行同士を比べない (誤報の主要因)。

    --rerank off に変えた週の Hit@5 低下を「劣化」と通知するのは誤報であり、
    それを1度でもやると通知そのものが信用されなくなる。
    """
    blob = json.dumps({"kind": kind, "config": config}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


def load_history(path: str = None) -> list:
    """履歴 JSONL を読む。壊れた行は捨てて続行する (1行の破損で監視を止めない)。"""
    path = path or HISTORY_PATH
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def append_history(row: dict, path: str = None) -> None:
    path = path or HISTORY_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def prior_values(history: list, kind: str, key: str, metric: str) -> list:
    """同一 kind・同一 config_key の過去の測定値を古い順に返す。"""
    out = []
    for row in history:
        if row.get("kind") != kind or row.get("config_key") != key:
            continue
        value = (row.get("metrics") or {}).get(metric)
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


def evaluate(value: float, prior: list, floor=None, max_drop: float = 0.13) -> tuple:
    """1指標の判定。("ok"|"baseline"|"breach_floor"|"breach_drop", 説明文) を返す。

    二段構えの理由:
      - 絶対下限だけだと、じわじわ下がって下限に触れるまで気付けない。
      - 直近1回との比較だけだと Gemini の非決定性で毎週フラップし、通知が
        狼少年になる → 直近3回の**中央値**を基準にする。
    過去が2件未満のときは下落判定を行わない (baseline)。ただし絶対下限は
    初回から効かせる — 人間が実測から決めた値なので、初回に割っているなら本物。
    """
    value = round(float(value), 4)
    if floor is not None and value < float(floor) - _EPS:
        return "breach_floor", f"絶対下限 {floor} を下回りました (今回 {value})"
    if len(prior) < 2:
        return "baseline", f"比較できる過去実行が {len(prior)}件 — 基準として記録 (今回 {value})"
    base = round(statistics.median(prior[-3:]), 4)
    if value < base - float(max_drop) - _EPS:
        return ("breach_drop",
                f"直近{min(3, len(prior))}回の中央値 {base} から {round(base - value, 4)} 下落 "
                f"(今回 {value} / 許容 {max_drop})")
    return "ok", f"{value} (直近中央値 {base})"


def trend_line(prior: list, value: float, width: int = 4) -> str:
    """推移を «0.82 → 0.80 → 0.76 → **0.64**» の形にする。"""
    tail = [round(v, 4) for v in prior[-(width - 1):]]
    return " → ".join([str(v) for v in tail] + [f"**{round(value, 4)}**"])


def check_staleness(history: list, now: datetime.datetime, max_age_days: int,
                    installed_at: str = "") -> str:
    """レーダー自体が止まっていないか。止まっていれば理由、正常なら空文字。

    これが無いと「ジョブが発火しなくなった」「毎週予算不足でスキップされ続けた」に
    気付けず、結局この機能を入れる前と同じ状態に戻る (レーダーの無言の死)。
    API を1回も叩かない。
    """
    stamps = [row.get("timestamp") for row in history if row.get("timestamp")]
    latest = max(stamps) if stamps else (installed_at or "")
    if not latest:
        return ""  # まだ一度も動かしていない — 基準が無いので判定しない
    try:
        ts = datetime.datetime.fromisoformat(latest)
    except ValueError:
        return f"履歴の日時 '{latest}' を解釈できません"
    age = (now - ts).days
    if age > max_age_days:
        what = "最後の測定" if stamps else "レーダーの導入"
        return (f"{what}から {age}日経っています ({ts:%Y-%m-%d %H:%M} 以降、推移が増えていません)。"
                f"スケジューラが動いているか、毎回スキップされていないか確認してください")
    return ""


# ---------------------------------------------------------
# 状態ファイル (通知の重複抑止・再試行間隔)
# ---------------------------------------------------------

def _load_state(path: str = None) -> dict:
    path = path or STATE_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict, path: str = None) -> None:
    path = path or STATE_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[radar] ⚠️ 状態ファイルの保存に失敗: {e}")


def should_notify(state: dict, key: str, now: datetime.datetime,
                  cooldown_hours: int = _NOTICE_COOLDOWN_HOURS) -> bool:
    """同じ理由の通知を短時間に繰り返さない。判定と同時に state を更新する。

    catch_up により測定失敗は次回起動ごとに再試行されるので、抑止が無いと
    再起動のたびに同じ ⚠️ が飛ぶ。
    """
    notices = state.setdefault("notices", {})
    last = notices.get(key)
    if last:
        try:
            if (now - datetime.datetime.fromisoformat(last)).total_seconds() < cooldown_hours * 3600:
                return False
        except ValueError:
            pass
    notices[key] = now.isoformat(timespec="seconds")
    return True


def _notify(message: str) -> None:
    """ログチャンネルへ通知する。失敗しても測定結果 (履歴) には影響させない。"""
    from lib.watchdog import notify_discord
    notify_discord(message[:1900])


# ---------------------------------------------------------
# 1本の eval を走らせて「測れたか」を判定する
# ---------------------------------------------------------

def _read_json(path: str) -> dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _tail(text: str, n: int = 300) -> str:
    text = (text or "").strip().replace("`", "'")
    return text[-n:] if len(text) > n else text


def _result_files(prefix: str, before: set, started: float) -> list:
    """今回の実行が書いた結果 JSON を古い順に返す。

    第一に「実行前に無かったファイル」を見る。ただし両ハーネスのファイル名は
    分単位 (%Y%m%d_%H%M) なので、同じ分に2回走らせると**同名を上書きする**。
    そのとき「新しいファイルが無い」= 測れなかった、と誤判定しないよう、
    更新時刻でも拾う (時刻の粒度に余裕を持たせる)。
    """
    if not os.path.isdir(EVAL_DIR):
        return []
    names = [f for f in os.listdir(EVAL_DIR)
             if f.startswith(prefix) and f.endswith(".json")]
    fresh = sorted(set(names) - before)
    if fresh:
        return fresh
    touched = [f for f in names
               if os.path.getmtime(os.path.join(EVAL_DIR, f)) >= started - 2]
    return sorted(touched)


def _unmeasured(kind: str, reason: str) -> dict:
    return {"kind": kind, "status": "unmeasured", "reason": reason,
            "metrics": {}, "config": {}, "config_key": "", "json": ""}


def _run_one(kind: str) -> dict:
    """eval を1本走らせ、**書き出された JSON を読み直して** 結果を判定する。

    終了コードだけを見ないのは契約1のため (eval_agent は全問不合格でも exit 0)。
    測れなかった場合は status="unmeasured" を返し、呼び出し側は履歴に追記しない。
    """
    spec = _SCRIPTS[kind]
    os.makedirs(EVAL_DIR, exist_ok=True)
    before = {f for f in os.listdir(EVAL_DIR)
              if f.startswith(spec["prefix"]) and f.endswith(".json")}

    cmd = [sys.executable, os.path.join(_PROJECT_ROOT, spec["script"])] + spec["args"]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=_PROJECT_ROOT, capture_output=True,
            encoding="utf-8", errors="replace", timeout=spec["timeout"],
            # LEVEL 1 (この子プロセス限りの env)。Windows 既定の cp932 で
            # 子の stdout が化けると、失敗理由が読めなくなる。
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return _unmeasured(kind, f"{spec['timeout']}秒でタイムアウトしました")
    except OSError as e:
        return _unmeasured(kind, f"起動できませんでした: {e}")
    duration = round(time.time() - started, 1)

    created = _result_files(spec["prefix"], before, started)
    if proc.returncode != 0:
        return _unmeasured(kind, f"exit={proc.returncode} / {_tail(proc.stdout) or _tail(proc.stderr)}")
    if not created:
        return _unmeasured(kind, f"結果 JSON が生成されませんでした / {_tail(proc.stdout)}")

    path = os.path.join(EVAL_DIR, created[-1])
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        return _unmeasured(kind, f"結果 JSON を読めませんでした ({e})")

    if kind == "retrieval":
        if not payload.get("arm_clean"):
            return _unmeasured(
                kind, "混合アーム (arm_clean=false) — 施策ONの指定に対し一部の問が "
                      "フォールバックしました。この数字は比較に使えません")
        raw = payload.get("metrics") or {}
        metrics = {"hit@5": raw.get("hit@5"), "mrr@10": raw.get("mrr@10"),
                   "mean_latency_sec": raw.get("mean_latency_sec"),
                   "degraded": raw.get("degraded")}
        config = payload.get("config") or {}
        calls = None
    else:
        meta = payload.get("meta") or {}
        if meta.get("full"):
            return _unmeasured(kind, "Tier B (--full) の結果でした — 自動実行は Tier A のみの契約です")
        # 集計は eval_agent の summarize() を共用する (ここで再実装すると
        # 「同じ数字を出す場所が2つ」になり、いずれ食い違う)
        from scripts.eval_agent import summarize
        summary = summarize(payload.get("results") or [])
        if not summary["graded"]:
            return _unmeasured(kind, "採点対象が0問でした (全問スキップ/エラー)")
        metrics = {"pass_rate": round(summary["pass_rate"] / 100.0, 4),
                   "passed": summary["passed"], "graded": summary["graded"]}
        config = {"model": meta.get("model"), "eval_set": meta.get("eval_set"),
                  "version": meta.get("version"), "full": bool(meta.get("full"))}
        calls = meta.get("actual_calls")

    if any(metrics.get(name) is None for name, _ in METRIC_SPECS[kind]):
        return _unmeasured(kind, f"指標が欠けています: {metrics}")

    return {"kind": kind, "status": "measured", "reason": "", "metrics": metrics,
            "config": config, "config_key": config_key(kind, config),
            "json": os.path.basename(path), "duration_sec": duration,
            "gemini_calls": calls}


# ---------------------------------------------------------
# 予算
# ---------------------------------------------------------

def budget_shortage(estimated: int) -> str:
    """実行後に予約分を残せるか。残せないなら理由を返す。

    catch_up=True なので日曜に PC が落ちていれば平日の起動直後に発火し得る。
    そのとき朝の生成系ジョブ (06:00〜08:00) の予算を奪わないためのガード。
    """
    try:
        from lib.cost_guard import GEMINI_LIGHT_COUNTER, GEMINI_HEAVY_COUNTER
        limit = GEMINI_LIGHT_COUNTER.limit + GEMINI_HEAVY_COUNTER.limit
        used = GEMINI_LIGHT_COUNTER.current() + GEMINI_HEAVY_COUNTER.current()
    except Exception as e:
        # 確認できないときは「OK」に倒さない (fail-closed)。カウンタを読めない
        # まま実行すると、日次上限の意味が黙って消える。
        return f"予算を確認できませんでした ({e}) — 安全側に倒して測定を見送ります"
    if limit <= 0:
        return ""  # 上限無効 (キルスイッチ) — ガードしない
    remaining = limit - used
    reserve = _env_int("EVAL_RADAR_MIN_RESERVE", 150)
    if remaining - estimated < reserve:
        return (f"日次 Gemini 予算の残り {remaining}回 — 測定 {estimated}回 + "
                f"他ジョブ用の予約 {reserve}回 に足りません")
    return ""


# ---------------------------------------------------------
# 通知の組み立て
# ---------------------------------------------------------

def compose_message(runs: list, verdicts: list, now: datetime.datetime) -> str:
    """異常時に送る本文。正常時は空文字 (silent ジョブなので何も送らない)。

    verdicts: (kind, metric, verdict, detail, trend) のリスト。
    """
    breaches = [v for v in verdicts if v[2].startswith("breach")]
    baselines = [v for v in verdicts if v[2] == "baseline"]
    unmeasured = [r for r in runs if r["status"] == "unmeasured"]
    if not breaches and not unmeasured and not baselines:
        return ""

    lines = []
    if breaches:
        lines.append("🔴 **Eval Radar: 品質の劣化を検知**")
        for kind, metric, _v, detail, trend in breaches:
            lines.append(f"- `{kind}` **{metric}** — {detail}")
            lines.append(f"  推移: {trend}")
    if unmeasured:
        lines.append("⚠️ **Eval Radar: 測定できませんでした**"
                     " (次回のスケジューラ起動時に繰り越します)")
        for r in unmeasured:
            lines.append(f"- `{r['kind']}` — {r['reason']}")
    if baselines and not breaches and not unmeasured:
        lines.append("ℹ️ **Eval Radar: ベースラインを記録しました** (設定が変わった/初回)")
        for kind, metric, _v, detail, _t in baselines:
            lines.append(f"- `{kind}` {metric}: {detail}")

    measured = [r for r in runs if r["status"] == "measured"]
    if measured:
        stamp = now.strftime("%Y-%m-%d %H:%M")
        detail = " / ".join(f"{r['kind']} {r['duration_sec']}s"
                            + (f" Gemini{r['gemini_calls']}回" if r.get("gemini_calls") else "")
                            for r in measured)
        lines.append(f"— {stamp} 実行: {detail}")
    return "\n".join(lines)


# ---------------------------------------------------------
# ジョブ本体
# ---------------------------------------------------------

def run_eval_radar(now: datetime.datetime = None, only: str = "", force: bool = False) -> str:
    """週次ジョブ本体。agent_scheduler から silent タスクとして呼ばれる。

    戻り値の契約 (agent_scheduler の is_failure_result / catch_up と噛み合う):
      ""                 正常 — 測れて閾値内。catch_up に成功として記録される
      "Eval Radar: ..."  測れたが劣化を検知 (通知済み)。**測定自体は成功**なので
                         失敗接頭辞を付けない = その週の分を再実行させない
      "Error: ..."       測れなかった。成功記録が残らないので、次回のスケジューラ
                         起動時に catch_up が自動で繰り越す
    """
    now = now or datetime.datetime.now()
    state = _load_state()
    state.setdefault("installed_at", now.isoformat(timespec="seconds"))

    kinds = [k for k in ("retrieval", "agent") if not only or k == only]
    if not kinds:
        return f"Error: --only '{only}' に一致する評価がありません"
    if "retrieval" in kinds and _env_flag("EVAL_RADAR_SKIP_RETRIEVAL"):
        kinds.remove("retrieval")
        print("[radar] EVAL_RADAR_SKIP_RETRIEVAL=1 のため検索評価をスキップします")
    if not kinds:
        return "Error: すべての評価がスキップ設定です (EVAL_RADAR_SKIP_RETRIEVAL)"

    # 測定失敗の直後に再起動を繰り返されると、そのたびに Gemini を使ってしまう。
    # 繰り越しは維持したまま (= 失敗を返したまま)、再試行の間隔だけ空ける。
    cooldown_h = _env_int("EVAL_RADAR_RETRY_COOLDOWN_HOURS", 6)
    last_attempt, last_status = state.get("last_attempt"), state.get("last_status")
    if not force and last_status == "unmeasured" and last_attempt:
        try:
            elapsed = (now - datetime.datetime.fromisoformat(last_attempt)).total_seconds()
            if elapsed < cooldown_h * 3600:
                return (f"Error: 前回の測定失敗から {cooldown_h}時間以内のため再試行を"
                        f"見送りました (残り {round((cooldown_h * 3600 - elapsed) / 60)}分)")
        except ValueError:
            pass

    shortage = budget_shortage(sum(ESTIMATED_CALLS[k] for k in kinds))
    if shortage:
        state["last_attempt"] = now.isoformat(timespec="seconds")
        state["last_status"] = "unmeasured"
        if should_notify(state, "budget", now):
            _notify(f"⚠️ **Eval Radar: 測定を見送りました**\n{shortage}\n"
                    f"次回のスケジューラ起動時に繰り越します")
        _save_state(state)
        return f"Error: {shortage}"

    runs = [_run_one(kind) for kind in kinds]

    history = load_history()  # 追記前に読む (今回の行を自分の基準にしない)
    max_drop = _env_float("EVAL_RADAR_MAX_DROP", 0.13)
    verdicts = []
    for run in runs:
        if run["status"] != "measured":
            continue
        for metric, env_key in METRIC_SPECS[run["kind"]]:
            value = run["metrics"][metric]
            prior = prior_values(history, run["kind"], run["config_key"], metric)
            verdict, detail = evaluate(value, prior, floor_for(env_key), max_drop)
            verdicts.append((run["kind"], metric, verdict, detail,
                             trend_line(prior, value)))
        append_history({
            "timestamp": now.isoformat(timespec="seconds"),
            "kind": run["kind"],
            "config_key": run["config_key"],
            "config": run["config"],
            "metrics": run["metrics"],
            "source": run["json"],
            "duration_sec": run.get("duration_sec"),
            "gemini_calls": run.get("gemini_calls"),
        })

    message = compose_message(runs, verdicts, now)
    unmeasured = [r for r in runs if r["status"] == "unmeasured"]
    breaches = [v for v in verdicts if v[2].startswith("breach")]

    if message:
        # 通知キーに理由を混ぜる: 別の異常が起きたときは抑止せずに知らせる
        key = "|".join(sorted([f"{r['kind']}:unmeasured" for r in unmeasured]
                              + [f"{v[0]}:{v[1]}:{v[2]}" for v in breaches]
                              + [f"{v[0]}:{v[1]}:baseline" for v in verdicts if v[2] == "baseline"]))
        if should_notify(state, key, now):
            _notify(message)
        print(message)

    state["last_attempt"] = now.isoformat(timespec="seconds")
    state["last_status"] = "unmeasured" if unmeasured else "measured"
    _save_state(state)

    if unmeasured:
        return "Error: " + " / ".join(f"{r['kind']}: {r['reason']}" for r in unmeasured)
    if breaches:
        return "Eval Radar: 劣化を検知 — " + ", ".join(f"{v[0]} {v[1]}" for v in breaches)
    return ""


def run_eval_staleness(now: datetime.datetime = None) -> str:
    """レーダー自身が止まっていないかを見る (API 0回)。

    戻り値: 正常なら ""、停滞していれば "Error: ..." (通知は重複抑止つき)。
    失敗を返すことで catch_up の成功記録が残らず、回復するまで起動のたびに
    再チェックされる。
    """
    now = now or datetime.datetime.now()
    state = _load_state()
    if not state.get("installed_at"):
        # 初回はここで基準を作るだけ。基準が無いのに「古い」とは言えない。
        state["installed_at"] = now.isoformat(timespec="seconds")
        _save_state(state)
        return ""
    reason = check_staleness(load_history(), now,
                             _env_int("EVAL_RADAR_STALE_DAYS", 10),
                             state.get("installed_at", ""))
    if not reason:
        return ""
    if should_notify(state, "stale", now):
        _notify(f"🟠 **Eval Radar: 推移が途絶えています**\n{reason}")
    _save_state(state)
    return f"Error: {reason}"


# ---------------------------------------------------------
# CLI
# ---------------------------------------------------------

def render_report(history: list) -> str:
    if not history:
        return "履歴がありません。まず py scripts/eval_radar.py を実行してください。"
    lines = ["| 日時 | 種別 | 設定 | 指標 |", "|---|---|---|---|"]
    for row in history[-30:]:
        metrics = row.get("metrics") or {}
        shown = " / ".join(f"{m}={metrics.get(m)}"
                           for m, _ in METRIC_SPECS.get(row.get("kind"), [])
                           if metrics.get(m) is not None)
        lines.append(f"| {row.get('timestamp', '?')[:16]} | {row.get('kind')} | "
                     f"`{row.get('config_key')}` | {shown} |")
    return "\n".join(lines)


def cmd_dry_run(only: str) -> int:
    kinds = [k for k in ("retrieval", "agent") if not only or k == only]
    if "retrieval" in kinds and _env_flag("EVAL_RADAR_SKIP_RETRIEVAL"):
        kinds.remove("retrieval")
    estimated = sum(ESTIMATED_CALLS[k] for k in kinds)
    print("=" * 62)
    print(" Eval Radar — dry-run (API 呼び出しは0回)")
    print("=" * 62)
    for kind in kinds:
        spec = _SCRIPTS[kind]
        cmd = [os.path.basename(sys.executable), spec["script"]] + spec["args"]
        print(f"  {kind:10s} {' '.join(cmd)}  (timeout {spec['timeout']}s / "
              f"Gemini 最大 {ESTIMATED_CALLS[kind]}回)")
    print(f"  合計見積り : {estimated}回")
    shortage = budget_shortage(estimated)
    print(f"  予算        : {shortage or 'OK'}")
    print(f"  閾値        : " + ", ".join(
        f"{m}>={floor_for(k) if floor_for(k) is not None else '未設定'}"
        for kind in kinds for m, k in METRIC_SPECS[kind]))
    print(f"  許容下落    : {_env_float('EVAL_RADAR_MAX_DROP', 0.13)}")
    history = load_history()
    print(f"  履歴        : {len(history)}行 ({HISTORY_PATH})")
    if history:
        print(f"  最終測定    : {max(r.get('timestamp', '') for r in history)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="eval を定期実行して品質の推移を監視する")
    parser.add_argument("--only", default="", choices=["", "retrieval", "agent"],
                        help="片方だけ実行する")
    parser.add_argument("--dry-run", action="store_true",
                        help="実行せず、消費見積りと閾値と履歴の状態だけ表示する")
    parser.add_argument("--report", action="store_true", help="推移表を表示する")
    parser.add_argument("--staleness", action="store_true",
                        help="レーダー自身の停止検知だけを実行する")
    parser.add_argument("--force", action="store_true",
                        help="失敗直後の再試行クールダウンを無視する")
    args = parser.parse_args(argv)

    if args.report:
        print(render_report(load_history()))
        return 0
    if args.dry_run:
        return cmd_dry_run(args.only)
    if args.staleness:
        result = run_eval_staleness()
        print(result or "✅ 推移は最新です")
        return 1 if result else 0

    result = run_eval_radar(only=args.only, force=args.force)
    if not result:
        print("✅ 測定完了 — 閾値内です (通知なし)")
        return 0
    print(result)
    return 1 if result.startswith("Error:") else 2


if __name__ == "__main__":
    sys.exit(main())
