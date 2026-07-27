# -*- coding: utf-8 -*-
"""
検索評価ハーネス (scripts/eval_retrieval.py) の採点ロジックのテスト
(純粋関数のみ — ネットワーク・Vault・インデックス不要)

このファイルが守るもの:
  採点器が嘘をつくと、その上に乗る「マージ可否の判断」が全部嘘になる。
  2026-07-28 に実際に起きたのがこれで、施策ONで走らせた15問のうち10問が
  Gemini の日次上限でフォールバックしていたのに、集計値だけ見ると
  「Hit@5 が 0.93 → 1.0 に改善」と読めてしまった (実際は5問にしか
  施策が効いておらず、残り10問はベースラインと同一の結果)。
  → is_clean_arm() で計測の汚染を検出し、比較表とプロセス終了コードに出す。

実行: py tests/test_eval_retrieval.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.eval_retrieval import first_hit_rank, score, is_clean_arm, compare


def _res(id_, rank, notes=None, latency=1.0):
    return {"id": id_, "rank": rank, "notes": notes or [], "latency_sec": latency}


def test_first_hit_rank():
    got = ["a.md", "b.md", "c.md"]
    assert first_hit_rank(got, ["b.md"]) == 2
    assert first_hit_rank(got, ["zzz.md"]) is None
    # expected は「許容される正解のいずれか」— 最も早く現れたものの順位を採る
    assert first_hit_rank(got, ["c.md", "a.md"]) == 1
    assert first_hit_rank([], ["a.md"]) is None
    print("✅ first_hit_rank: 最初に当たった許容正解の順位")


def test_score_metrics():
    results = [_res("R1", 1), _res("R2", 3), _res("R3", None), _res("R4", 7)]
    m = score(results)
    # rank<=5 が当たり: R1(1) と R2(3) の2件
    assert m["hit@5"] == 0.5, m
    # MRR@10: (1/1 + 1/3 + 0 + 1/7) / 4
    assert abs(m["mrr@10"] - (1 + 1 / 3 + 1 / 7) / 4) < 1e-4, m
    assert m["hits"] == 2 and m["total"] == 4
    print("✅ score: Hit@5 / MRR@10 の計算")


def test_score_excludes_out_of_window_rank():
    """MRR@10 の窓の外 (11位) は 0 として扱う — 窓を無視して加点しない"""
    m = score([_res("R1", 11)])
    assert m["mrr@10"] == 0.0, m
    assert m["hit@5"] == 0.0, m
    print("✅ score: 窓外の順位は加点しない")


def test_is_clean_arm_detects_mixed_measurement():
    """施策ONを要求したのにフォールバックが混ざった計測を汚染として検出する"""
    on = {"multiquery": True, "rerank": True}
    off = {"multiquery": False, "rerank": False}

    assert is_clean_arm(on, [_res("R1", 1), _res("R2", 1)]) is True
    assert is_clean_arm(on, [_res("R1", 1), _res("R2", 1, ["予算超過"])]) is False
    # OFF アームはフォールバックしようがないので常にクリーン
    assert is_clean_arm(off, [_res("R1", 1)]) is True
    # 片方だけ ON でも「要求した施策がある」以上は汚染を見る
    assert is_clean_arm({"multiquery": True, "rerank": False},
                        [_res("R1", 1, ["フォールバック"])]) is False
    print("✅ is_clean_arm: 混合計測を検出する")


def test_compare_flags_dirty_arm():
    """汚染したアームは比較表の先頭で警告する (数字だけ見て判断させない)"""
    before = {"timestamp": "t0", "config": {"multiquery": False, "rerank": False},
              "arm_clean": True,
              "metrics": {"hit@5": 0.93, "mrr@10": 0.73, "mean_latency_sec": 2.7},
              "results": [_res("R1", 4)]}
    after = {"timestamp": "t1", "config": {"multiquery": True, "rerank": True},
             "arm_clean": False,
             "metrics": {"hit@5": 1.0, "mrr@10": 0.9, "mean_latency_sec": 5.0},
             "results": [_res("R1", 1, ["予算超過"])]}

    table = compare(before, after)
    assert "⛔ after" in table, table
    assert "マージ判定に使ってはいけません" in table, table
    # 数字自体は隠さない (隠すと今度は何も分からなくなる)
    assert "hit@5" in table and "1.0" in table, table

    clean_after = dict(after, arm_clean=True, results=[_res("R1", 1)])
    assert "⛔" not in compare(before, clean_after)
    print("✅ compare: 汚染アームに警告を出し、クリーンなら出さない")


def test_compare_direction_of_rank_change():
    """順位は小さいほど良い。圏外(None)は最悪として扱う"""
    base = {"timestamp": "t", "arm_clean": True,
            "config": {"multiquery": False, "rerank": False},
            "metrics": {"hit@5": 0, "mrr@10": 0, "mean_latency_sec": 0}}
    before = dict(base, results=[_res("R1", 5), _res("R2", 1), _res("R3", None)])
    after = dict(base, config={"multiquery": True, "rerank": True},
                 results=[_res("R1", 1), _res("R2", 4), _res("R3", 2)])

    table = compare(before, after)
    rows = {line.split("|")[1].strip(): line for line in table.splitlines()
            if line.startswith("| R")}
    assert "⬆️改善" in rows["R1"], rows["R1"]      # 5 -> 1
    assert "⬇️悪化" in rows["R2"], rows["R2"]      # 1 -> 4
    assert "⬆️改善" in rows["R3"], rows["R3"]      # 圏外 -> 2
    print("✅ compare: 改善/悪化の向きが正しい (圏外を含む)")


def test_fixture_is_wellformed():
    """正解ラベルの体裁 — ID重複なし・expected 非空・why 必須"""
    import json
    path = os.path.join(_PROJECT_ROOT, "tests", "fixtures", "retrieval_eval.json")
    with open(path, encoding='utf-8') as f:
        questions = json.load(f)["questions"]

    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), f"ID重複: {ids}"
    assert len(questions) >= 10, f"問数が少なすぎる: {len(questions)}"
    for q in questions:
        assert q.get("query", "").strip(), q
        assert q.get("expected"), f"{q['id']}: expected が空"
        assert all(e.endswith(".md") for e in q["expected"]), q
        # 根拠が無いラベルは後から検証できない = 評価が信用できなくなる
        assert q.get("why", "").strip(), f"{q['id']}: why が無い"
    print(f"✅ fixture: {len(questions)}問のラベルが体裁を満たす")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
