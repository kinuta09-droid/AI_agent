# -*- coding: utf-8 -*-
"""
検索専用の評価ハーネス (Phase A2)

scripts/eval_agent.py は「回答全体」を20問で採点するもので、検索そのものの
精度は測っていない。multi-query 書き換え・LLMリランクは回答の言い回しを変えずに
順位だけを動かすので、あちらでは改善も劣化も見えない。ここで検索を単体で測る。

  py scripts/eval_retrieval.py --multiquery off --rerank off --label baseline
  py scripts/eval_retrieval.py --multiquery on  --rerank on  --compare logs/eval/retrieval_XXXX.json

指標:
  Hit@5   — 上位5件に許容正解が1件でも入った質問の割合
            (expected は「許容される正解」のリストなので厳密には Success@5。
             1ノートしか正解が無い質問では Recall@5 と一致する)
  MRR@10  — 最初に当たった許容正解の順位の逆数の平均 (10位以内、外れは0)
  latency — 1問あたりの秒数 (multi-query はキーワード全走査を N 倍にするため)

前提:
  - logs/vault_index.db が構築済み (未構築ならキーワード検索だけの数字になり
    比較の意味が消えるので、その場合は測定せずに落とす)
  - Ollama (bge-m3) が起動している
  - --multiquery/--rerank on では Light Gemini を 1問あたり2回叩く
    (15問なら30回。cost_guard の日次上限に効く)

読み取り専用: Vault にもインデックスにも書き込まない。書くのは logs/eval/ のみ。
"""

import argparse
import datetime
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_vault_hybrid import _hybrid_hits

FIXTURE_PATH = os.path.join(_PROJECT_ROOT, "tests", "fixtures", "retrieval_eval.json")
EVAL_OUT_DIR = os.path.join(_PROJECT_ROOT, "logs", "eval")

HIT_K = 5
MRR_K = 10
# 採点は上位 MRR_K 件まで見る。tool 既定の top_k=5 より深いのは MRR のため。
# 両アームで同じ値を使うので比較の公平性は保たれる。
RETRIEVE_K = MRR_K


# ---------------------------------------------------------
# 採点 (純粋関数 — テストから直接呼べる)
# ---------------------------------------------------------

def first_hit_rank(got: list, expected: list):
    """got (rel_path の順位順) の中で expected のどれかが最初に現れる順位 (1始まり)。

    見つからなければ None。expected を「許容される正解のいずれか」として扱うのは、
    同じ問いに答えるノートが複数ある Vault では、どれを1位にしても
    ユーザーにとっては当たりだから。
    """
    wanted = set(expected)
    for i, path in enumerate(got, start=1):
        if path in wanted:
            return i
    return None


def score(results: list) -> dict:
    """結果リストから指標を計算する。"""
    n = len(results) or 1
    hits = sum(1 for r in results if r["rank"] is not None and r["rank"] <= HIT_K)
    rr = sum(1.0 / r["rank"] for r in results
             if r["rank"] is not None and r["rank"] <= MRR_K)
    return {
        f"hit@{HIT_K}": round(hits / n, 4),
        f"mrr@{MRR_K}": round(rr / n, 4),
        "hits": hits,
        "total": len(results),
        "mean_latency_sec": round(sum(r["latency_sec"] for r in results) / n, 2),
        "degraded": sum(1 for r in results if r["notes"]),
    }


# ---------------------------------------------------------
# 実行
# ---------------------------------------------------------

def _preflight() -> str:
    """インデックスと Ollama を確認する。問題があれば理由を返す (正常は "")。

    キーワード検索だけでも数字は出てしまうが、それは「意味検索の効果を
    測ったつもりで測れていない」状態なので、静かに続行させない。
    """
    from tools.tool_vault_index import DB_PATH, _semantic_hits
    if not os.path.exists(DB_PATH):
        return f"[ERROR] semantic index がありません: {DB_PATH} — py tools/tool_vault_index.py を先に実行してください"
    probe = _semantic_hits("疎通確認", top_k=1)
    if isinstance(probe, str):
        return f"[ERROR] semantic search が使えません: {probe}"
    return ""


def run(questions: list, multiquery: bool, rerank: bool, only: str = "") -> list:
    results = []
    for q in questions:
        if only and q["id"] not in {s.strip() for s in only.split(",")}:
            continue
        started = time.time()
        hits, notes, queries = _hybrid_hits(
            q["query"], top_k=RETRIEVE_K, multiquery=multiquery, rerank=rerank)
        elapsed = time.time() - started

        got = [h["rel_path"] for h in hits]
        rank = first_hit_rank(got, q["expected"])
        results.append({
            "id": q["id"],
            "query": q["query"],
            "expected": q["expected"],
            "got": got,
            "got_citations": [
                f"[[{h['filename']}#{h['heading']}]]" if h.get("heading")
                else f"[[{h['filename']}]]" for h in hits
            ],
            "rank": rank,
            "queries_used": queries,
            "notes": notes,
            "latency_sec": round(elapsed, 2),
        })
        mark = "✅" if (rank and rank <= HIT_K) else "❌"
        print(f"  {mark} {q['id']} rank={rank if rank else '-'} "
              f"({elapsed:.1f}s, {len(queries)}クエリ) {q['query'][:34]}")
        if notes:
            for note in notes:
                print(f"       ⚠️ {note[:110]}")
    return results


# ---------------------------------------------------------
# 比較
# ---------------------------------------------------------

def is_clean_arm(config: dict, results: list) -> bool:
    """要求した施策が全問で実際に効いたか。

    ON を要求したのに Gemini の日次上限・Ollama 停止でフォールバックした問が
    混じっていると、集計値は「一部が施策あり・残りがベースライン」の混合になる。
    それを普通の数値として出すと、このプロジェクトが繰り返し踏んできた
    「表示と実態の食い違い」そのものになるので、計測の汚染をフラグで持ち回る。
    """
    requested = config.get("multiquery") or config.get("rerank")
    if not requested:
        return True  # OFF アームはフォールバックしようがない
    return not any(r["notes"] for r in results)


def _looks_like_retrieval_run(payload) -> bool:
    """このスクリプトが出した JSON か。

    logs/eval/ には scripts/eval_agent.py の出力 (eval_*.json) も同居しており、
    取り違えて --compare に渡すと KeyError でスタックトレースが出るだけになる。
    """
    return (isinstance(payload, dict)
            and isinstance(payload.get("metrics"), dict)
            and isinstance(payload.get("results"), list)
            and f"hit@{HIT_K}" in payload["metrics"])


def compare(before: dict, after: dict) -> str:
    """before/after の差分表を作る。コミットメッセージにそのまま貼れる形式。"""
    if not _looks_like_retrieval_run(before):
        return ("⛔ --compare に渡した JSON は eval_retrieval.py の出力ではありません "
                "(logs/eval/retrieval_*.json を指定してください)。")
    lines = []
    for label, payload in (("before", before), ("after", after)):
        if payload.get("arm_clean") is False:
            dirty = sum(1 for r in payload.get("results", []) if r["notes"])
            lines.append(
                f"⛔ {label} は {dirty}問がフォールバックしており、施策あり/なしの混合です。"
                "この表をマージ判定に使ってはいけません。")
    if lines:
        lines.append("")

    b_cfg, a_cfg = before.get("config", {}), after.get("config", {})
    lines.append(f"before: multiquery={b_cfg.get('multiquery')} rerank={b_cfg.get('rerank')} "
                 f"({before.get('timestamp', '?')})")
    lines.append(f"after : multiquery={a_cfg.get('multiquery')} rerank={a_cfg.get('rerank')} "
                 f"({after.get('timestamp', '?')})")
    lines.append("")
    lines.append("| 指標 | before | after | 差分 |")
    lines.append("|------|--------|-------|------|")
    for key in (f"hit@{HIT_K}", f"mrr@{MRR_K}", "mean_latency_sec"):
        b = before["metrics"].get(key, 0)
        a = after["metrics"].get(key, 0)
        lines.append(f"| {key} | {b} | {a} | {a - b:+.4f} |")

    lines.append("")
    lines.append("| id | before rank | after rank | |")
    lines.append("|----|-------------|------------|-|")
    b_rank = {r["id"]: r["rank"] for r in before.get("results", [])}
    changed = 0
    for r in after.get("results", []):
        rb, ra = b_rank.get(r["id"]), r["rank"]
        if rb == ra:
            continue
        changed += 1
        # rank は小さいほど良い。None (圏外) は最悪として扱う
        better = (ra is not None) and (rb is None or ra < rb)
        lines.append(f"| {r['id']} | {rb if rb else '圏外'} | {ra if ra else '圏外'} | "
                     f"{'⬆️改善' if better else '⬇️悪化'} |")
    if not changed:
        lines.append("| — | — | — | 順位変化なし |")
    return "\n".join(lines)


# ---------------------------------------------------------
# main
# ---------------------------------------------------------

def _flag(value: str) -> bool:
    return str(value).strip().lower() in ("on", "1", "true", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(description="Vault検索 (ハイブリッド) の精度を測る")
    parser.add_argument("--multiquery", default="on", choices=["on", "off"],
                        help="クエリ書き換えの有無 (.env より優先)")
    parser.add_argument("--rerank", default="on", choices=["on", "off"],
                        help="LLMリランクの有無 (.env より優先)")
    parser.add_argument("--only", default="", help="評価するIDをカンマ区切りで限定 (例: R01,R04)")
    parser.add_argument("--label", default="", help="出力ファイル名に付ける識別子")
    parser.add_argument("--compare", default="", help="比較対象の過去JSON")
    parser.add_argument("--fixture", default=FIXTURE_PATH)
    args = parser.parse_args()

    problem = _preflight()
    if problem:
        print(problem)
        return 1

    with open(args.fixture, encoding='utf-8') as f:
        questions = json.load(f)["questions"]

    multiquery, rerank = _flag(args.multiquery), _flag(args.rerank)
    print("=" * 62)
    print(f" 検索評価: multiquery={'ON' if multiquery else 'OFF'} / "
          f"rerank={'ON' if rerank else 'OFF'} / {len(questions)}問")
    print("=" * 62)

    started = time.time()
    results = run(questions, multiquery, rerank, only=args.only)
    metrics = score(results)

    config = {"multiquery": multiquery, "rerank": rerank,
              "retrieve_k": RETRIEVE_K, "fixture": os.path.basename(args.fixture)}
    payload = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "config": config,
        "arm_clean": is_clean_arm(config, results),
        "metrics": metrics,
        "results": results,
    }

    os.makedirs(EVAL_OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    suffix = f"_{args.label}" if args.label else ""
    out_path = os.path.join(EVAL_OUT_DIR, f"retrieval_{stamp}{suffix}.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("\n" + "-" * 62)
    print(f" Hit@{HIT_K}  : {metrics[f'hit@{HIT_K}']}  ({metrics['hits']}/{metrics['total']})")
    print(f" MRR@{MRR_K} : {metrics[f'mrr@{MRR_K}']}")
    print(f" 平均秒   : {metrics['mean_latency_sec']}  (総 {time.time() - started:.1f}s)")
    print(f" 劣化あり : {metrics['degraded']}問")
    print(f" → {out_path}")

    if args.compare:
        with open(args.compare, encoding='utf-8') as f:
            before = json.load(f)
        print("\n" + "=" * 62)
        print(compare(before, payload))

    if not payload["arm_clean"]:
        print("\n" + "!" * 62)
        print(f"⛔ 施策ONを指定したのに {metrics['degraded']}問がフォールバックしました。")
        print("   この結果は『施策あり』と『ベースライン』の混合で、マージ判定に使えません。")
        print("   上の ⚠️ 行の理由 (Gemini日次上限 / Ollama停止など) を解消して測り直してください。")
        print("!" * 62)
        return 2  # 1 = 実行できなかった / 2 = 実行できたが数字が使えない

    return 0


if __name__ == "__main__":
    sys.exit(main())
