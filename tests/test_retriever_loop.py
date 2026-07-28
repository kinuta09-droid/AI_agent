# -*- coding: utf-8 -*-
"""tests/test_retriever_loop.py — Phase D1: Retriever Agent (適応リトライループ)

検証する不変条件:
  [1]  判定行が無い応答 → ループしない (旧挙動への安全な縮退)
  [2]  SUFFICIENT の語彙ゆれ (yes/no/全角コロン/判読不能)
  [3]  MISSING に角括弧が混ざっても番号配列を取り違えない
  [4]  SUFFICIENT: no → 2周目が走り、クエリが queries に残る
  [5]  ループが発火しないとき、結果は loop=off と完全に一致する
  [6]  1周目のヒットは2周目で **1件も捨てられない** (並べ替えであって足切りではない)
  [7]  2周目の新規ヒットが候補枠に実際に入る (枠が埋まっていても)
  [8]  RETRIEVER_MAX_ITERS で止まり、固定の設計値は note に出さない
  [9]  締切超過は note に出る (実行時の事故なので気付けないと困る)
  [10] リランクOFF ではループ自体が動かない (LLM を1回も叩かない)
  [11] 書き換えが既出クエリしか返さなければ中止し、理由が note に残る
  [12] 2周目の query_ids が1周目と衝突しない
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tool_vault_hybrid import (  # noqa: E402
    _hybrid_hits, _llm_rerank, _looks_solid, _merge_new_hits, _parse_sufficiency,
    _retrieve_loop, _rewrite_query, _should_retry, _split_verdict,
    RETRIEVER_MAX_ITERS, RETRIEVER_DEADLINE_SEC,
)


# ---------------------------------------------------------
# ヘルパ
# ---------------------------------------------------------

def _cand(rel_path, rrf=0.01):
    return {"rel_path": rel_path, "filename": rel_path, "rrf": rrf,
            "sources": ["keyword"], "query_ids": [1], "heading": None,
            "chunk_id": None, "snippet": f"{rel_path} の本文", "rerank_pos": None,
            "source": "vault"}


def _kw(*names):
    """rel_path を返すだけのキーワード検索。score は並び順どおり降順。"""
    def fn(q):
        return [{"rel_path": n, "filename": n, "score": len(names) - i,
                 "snippet": f"{n} の本文"} for i, n in enumerate(names)]
    return fn


class _Scripted:
    """呼ばれた順に応答を返す LLM スタブ。プロンプトも記録する。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError(f"想定より多く LLM が呼ばれた ({len(self.prompts)}回目)")
        return self.responses.pop(0)


def _hits3(**kw):
    """_hybrid_hits の注入契約どおり keyword/semantic/activity の3本を必ず塞ぐ。

    2本しか渡さないと隔離したつもりで実インデックス (logs/vault_index.db) が
    混ざる — _hybrid_hits の docstring に明記された罠。
    """
    kw.setdefault("semantic_fn", lambda q, k: [])
    kw.setdefault("activity_keyword_fn", lambda q, k: [])
    kw.setdefault("multiquery", False)
    return _hybrid_hits(**kw)


# ---------------------------------------------------------
# [1][2][3] 十分性判定のパース
# ---------------------------------------------------------

def test_no_verdict_line_means_no_loop():
    """判定行が無い = 旧来のリランク応答。ループしない (安全側へ縮退)"""
    assert _parse_sufficiency("[1, 2, 3]") == (None, "")
    assert _should_retry([_cand("a.md")], (None, "")) is False
    print("✅ [1] 判定行が無い応答は sufficient=None → 引き直さない")


def test_sufficiency_vocabulary():
    assert _parse_sufficiency("[1]\nSUFFICIENT: yes")[0] is True
    assert _parse_sufficiency("[1]\nSUFFICIENT: no")[0] is False
    # 日本語プロンプトに答えるモデルは全角コロンを混ぜてくる
    assert _parse_sufficiency("[1]\nSUFFICIENT： no")[0] is False
    # 箇条書き記号を前置してくることもある
    assert _parse_sufficiency("[1]\n- SUFFICIENT: no")[0] is False
    assert _parse_sufficiency("[1]\nsufficient: YES")[0] is True
    # 読めない語彙は None (= 引き直さない)。False に倒すと毎回2周して倍遅くなる
    assert _parse_sufficiency("[1]\nSUFFICIENT: maybe")[0] is None
    assert _parse_sufficiency("[1]\nSUFFICIENT: 十分")[0] is True
    print("✅ [2] SUFFICIENT の語彙ゆれ (全角コロン/大小文字/日本語/判読不能)")


def test_missing_with_brackets_does_not_steal_the_array():
    """MISSING の角括弧を候補番号として拾わない

    _parse_json_array は非貪欲マッチを**後ろから**優先するので、判定行を
    切り離さずに渡すと「MISSING: [重要]な条文」の [重要] を先に試してしまう。
    """
    raw = "[1, 3]\nSUFFICIENT: no\nMISSING: [重要]な条文名"
    array_part, verdict_part = _split_verdict(raw)
    assert "[重要]" not in array_part, array_part
    assert array_part.strip() == "[1, 3]", array_part
    assert verdict_part.startswith("SUFFICIENT"), verdict_part

    cands = [_cand("a.md"), _cand("b.md"), _cand("c.md")]
    ordered, note, verdict = _llm_rerank("質問", cands, llm_fn=lambda p: raw, enabled=True)
    assert [h["rel_path"] for h in ordered][:2] == ["a.md", "c.md"], ordered
    assert note == "", note
    assert verdict == (False, "[重要]な条文名"), verdict
    print("✅ [3] MISSING の角括弧を番号配列と取り違えない")


# ---------------------------------------------------------
# [4][5] 発火する / しない
# ---------------------------------------------------------

def test_loop_fires_and_records_rewritten_query():
    llm = _Scripted(
        "[1]\nSUFFICIENT: no\nMISSING: 導入コストの金額",  # 1周目リランク
        '["導入コスト 見積もり"]',                          # 書き換え
        "[2, 1]\nSUFFICIENT: yes",                          # 2周目リランク
    )
    hits, notes, queries = _hits3(
        query="コストの話", keyword_fn=_kw("a.md", "b.md"), llm_fn=llm,
        rerank=True, loop=True, top_k=5)

    assert queries == ["コストの話", "導入コスト 見積もり"], queries
    assert len(llm.prompts) == 3, llm.prompts
    assert notes == [], notes  # 正常な引き直しは事故ではないので note を汚さない
    print("✅ [4] SUFFICIENT: no → 引き直し、クエリが queries に残る")


def test_no_fire_is_byte_identical_to_loop_off():
    """発火しないときは loop=off と完全一致 (既存の全計測に影響しない)

    これが D1 の最重要ゲート: 既存15問のベースラインが動かない根拠。
    """
    def _run(loop_flag, response):
        return _hits3(query="質問", keyword_fn=_kw("a.md", "b.md", "c.md"),
                      llm_fn=lambda p: response, rerank=True, loop=loop_flag,
                      top_k=3)

    for response in ("[2, 1, 3]", "[2, 1, 3]\nSUFFICIENT: yes"):
        on_hits, on_notes, on_q = _run(True, response)
        off_hits, off_notes, off_q = _run(False, response)
        assert [h["rel_path"] for h in on_hits] == [h["rel_path"] for h in off_hits], response
        assert on_notes == off_notes, (on_notes, off_notes)
        assert on_q == off_q, (on_q, off_q)
    print("✅ [5] 発火しない検索は loop=off と同一結果 (既存ベースラインを動かさない)")


# ---------------------------------------------------------
# [6][7] 候補集合の扱い (multi-query の失敗を繰り返さない)
# ---------------------------------------------------------

def test_first_round_hits_are_never_dropped():
    """2周目で候補枠から溢れた1周目のヒットも、捨てずに後ろへ回す"""
    base = [_cand(f"old{i}.md") for i in range(20)]
    extra = [_cand(f"new{i}.md") for i in range(5)]
    merged = _merge_new_hits(base, extra)
    assert len(merged) == 25, len(merged)
    assert [h["rel_path"] for h in merged[:20]] == [h["rel_path"] for h in base]

    # _retrieve_loop を直接呼ぶときは verdict を引数で渡しているので、
    # 中で最初に走る LLM 呼び出しは **書き換え** (1周目のリランクは済んでいる)。
    llm = _Scripted('["別クエリ"]',
                    "[1, 2]\nSUFFICIENT: yes")
    names = [f"f{i:02d}.md" for i in range(20)]
    reranked, notes, queries = _retrieve_loop(
        "質問", [_cand(n) for n in names], (False, "別の観点"), ["質問"], time.time(),
        keyword_fn=_kw("new1.md", "new2.md"), semantic_fn=lambda q, k: [],
        activity_keyword_fn=lambda q, k: [], llm_fn=llm, rerank=True,
        rerank_backend=None, per_query_k=10, n_cand=20, loop=True)

    survivors = {h["rel_path"] for h in reranked}
    missing = [n for n in names if n not in survivors]
    assert not missing, f"1周目のヒットが消えた: {missing}"
    print("✅ [6] 1周目のヒットは2周目で1件も捨てられない")


def test_new_hits_get_room_even_when_budget_is_full():
    """候補枠が1周目で埋まっていても、2周目の新規ヒットが枠に入る

    素朴に merged[:n_cand] と書くと新規が1件も入らず、引き直しが無意味になる。
    """
    # 1周目のリランクは済んでいるので、最初の応答は書き換え (test [6] のコメント参照)
    llm = _Scripted('["別クエリ"]',
                    "[11]\nSUFFICIENT: yes")  # 11番目 = 新規が枠に入っていないと選べない
    reranked, notes, queries = _retrieve_loop(
        "質問", [_cand(f"f{i:02d}.md") for i in range(20)], (False, "別の観点"),
        ["質問"], time.time(),
        keyword_fn=_kw("brandnew.md"), semantic_fn=lambda q, k: [],
        activity_keyword_fn=lambda q, k: [], llm_fn=llm, rerank=True,
        rerank_backend=None, per_query_k=10, n_cand=20, loop=True)

    assert reranked[0]["rel_path"] == "brandnew.md", [h["rel_path"] for h in reranked[:3]]
    print("✅ [7] 枠が埋まっていても2周目の新規ヒットが候補に入る")


# ---------------------------------------------------------
# [8][9] 予算・締切
# ---------------------------------------------------------

def test_max_iters_stops_and_stays_out_of_notes():
    """設計値による打ち切りは note に出さない (毎回同じ理由 = 診断情報ゼロ)

    ここを note に出すと eval_retrieval.is_clean_arm が
    「答えが Vault に無い問」まで汚染扱いにして ⛔ を出す。
    """
    # 1周目のリランクは済んでいるので、最初の応答は書き換え (test [6] のコメント参照)
    llm = _Scripted('["2周目クエリ"]',
                    "[1]\nSUFFICIENT: no\nMISSING: まだ足りない")
    reranked, notes, queries = _retrieve_loop(
        "質問", [_cand("a.md")], (False, "x"), ["質問"], time.time(),
        keyword_fn=_kw("b.md"), semantic_fn=lambda q, k: [],
        activity_keyword_fn=lambda q, k: [], llm_fn=llm, rerank=True,
        rerank_backend=None, per_query_k=10, n_cand=20, loop=True)

    assert len(queries) == RETRIEVER_MAX_ITERS, queries
    assert llm.responses == [], "予定した応答を使い切っていない"
    assert notes == [], f"設計値の打ち切りが note に漏れた: {notes}"
    print(f"✅ [8] 最大{RETRIEVER_MAX_ITERS}周で止まり、note を汚さない")


def test_deadline_is_reported_in_notes():
    """締切超過は実行時の事故なので note に残す (気付けないと困る)"""
    llm = _Scripted()  # 1回でも呼ばれたら AssertionError
    stale = time.time() - RETRIEVER_DEADLINE_SEC - 1
    reranked, notes, queries = _retrieve_loop(
        "質問", [_cand("a.md")], (False, "x"), ["質問"], stale,
        keyword_fn=_kw("b.md"), semantic_fn=lambda q, k: [],
        activity_keyword_fn=lambda q, k: [], llm_fn=llm, rerank=True,
        rerank_backend=None, per_query_k=10, n_cand=20, loop=True)

    assert queries == ["質問"], queries
    assert llm.prompts == [], "締切超過なのに LLM を呼んだ"
    assert any("締切" in n for n in notes), notes
    print("✅ [9] 締切超過は LLM を呼ばずに止まり、理由が note に残る")


# ---------------------------------------------------------
# [10][11][12]
# ---------------------------------------------------------

def test_rerank_off_disables_the_loop_entirely():
    """リランクOFF ではループが LLM を1回も叩かない

    「検索に LLM を使わない」設定なのに引き直しでだけ叩くのは筋が通らず、
    ベースライン計測用のアームも汚れる。
    """
    llm = _Scripted()  # 1回でも呼ばれたら AssertionError
    hits, notes, queries = _hits3(
        query="質問", keyword_fn=_kw("a.md"), llm_fn=llm,
        rerank=False, loop=True, top_k=3)
    assert llm.prompts == [], "リランクOFFなのに LLM を呼んだ"
    assert queries == ["質問"], queries
    assert notes == [], f"既知の設定が note に漏れた: {notes}"

    # ヒット0件でも同じ (0件は LLM 不要の合図だが、OFF の意思を上書きしない)
    hits, notes, queries = _hits3(
        query="質問", keyword_fn=lambda q: [], llm_fn=llm,
        rerank=False, loop=True, top_k=3)
    assert llm.prompts == [], "0件かつリランクOFFなのに LLM を呼んだ"
    print("✅ [10] リランクOFF ではループが LLM を1回も叩かない")


def test_rewrite_refuses_to_repeat_a_tried_query():
    new_q, note = _rewrite_query("質問", "不足", ["質問", "既出クエリ"],
                                 llm_fn=lambda p: '["既出クエリ"]')
    assert new_q == "", new_q
    assert note and "中止" in note, note

    new_q, note = _rewrite_query("質問", "不足", ["質問"],
                                 llm_fn=lambda p: '["既出クエリ", "新しいクエリ"]')
    assert new_q == "既出クエリ", new_q
    assert note == "", note

    def boom(p):
        raise RuntimeError("ollama down")
    new_q, note = _rewrite_query("質問", "不足", ["質問"], llm_fn=boom)
    assert new_q == "", new_q
    assert "RuntimeError" in note, note
    print("✅ [11] 既出クエリ・例外では引き直しを中止し、理由が残る")


def test_second_round_query_ids_do_not_collide():
    """2周目のヒットの query_ids が1周目の q1 と区別できる"""
    llm = _Scripted("[1]\nSUFFICIENT: no\nMISSING: x",
                    '["2周目クエリ"]',
                    "[2, 1]\nSUFFICIENT: yes")
    hits, notes, queries = _hits3(
        query="質問", keyword_fn=_kw("a.md"), llm_fn=llm,
        rerank=True, loop=True, top_k=5)

    by_path = {h["rel_path"]: h for h in hits}
    assert by_path["a.md"]["query_ids"] == [1, 2], by_path["a.md"]["query_ids"]
    assert len(queries) == 2, queries
    print("✅ [12] 2周目の query_ids が1周目と衝突しない")


# ---------------------------------------------------------
# [13][14] 決定的な抑制条件 (_looks_solid)
# ---------------------------------------------------------

def _picked(rel_path, pos):
    h = _cand(rel_path)
    h["rerank_pos"] = pos
    return h


def test_looks_solid_requires_agreement_and_enough_picks():
    rrf = [_cand("a.md"), _cand("b.md"), _cand("c.md")]

    # 2つのランカーが1位で一致 + 2件以上選んでいる → 十分
    assert _looks_solid(rrf, [_picked("a.md", 1), _picked("b.md", 2), _cand("c.md")])
    # 1位が食い違う → まだ引き直す価値がある (R03 型)
    assert not _looks_solid(rrf, [_picked("c.md", 1), _picked("a.md", 2), _cand("b.md")])
    # 一致していても1件しか選べていない → 候補集合が薄い兆候
    assert not _looks_solid(rrf, [_picked("a.md", 1), _cand("b.md"), _cand("c.md")])
    # 1件も選べていない (リランクのフォールバック等)
    assert not _looks_solid(rrf, rrf)
    assert not _looks_solid([], [])
    print("✅ [13] 一致 + 採用2件以上 のときだけ「十分」と判定する")


def test_solid_overrides_a_negative_verdict():
    """一致しているなら SUFFICIENT: no でも引き直さない (LLM 判定を打ち消す)

    実測でリランカの no は出やすすぎ、正解が既に1位の問でも出ていた。
    ただし **ヒット0件は solid より優先** する。
    """
    rrf = [_cand("a.md"), _cand("b.md")]
    solid_rank = [_picked("a.md", 1), _picked("b.md", 2)]
    assert _should_retry(solid_rank, (False, "不足"), solid=True) is False
    assert _should_retry(solid_rank, (False, "不足"), solid=False) is True
    assert _should_retry([], (True, ""), solid=True) is True, "0件は solid より優先"

    # 経路全体でも LLM が1回も呼ばれない (書き換えに進まない)
    llm = _Scripted("[1, 2]\nSUFFICIENT: no\nMISSING: もっと欲しい")
    hits, notes, queries = _hits3(
        query="質問", keyword_fn=_kw("a.md", "b.md"), llm_fn=llm,
        rerank=True, loop=True, top_k=3)
    assert queries == ["質問"], f"一致しているのに引き直した: {queries}"
    assert len(llm.prompts) == 1, "リランク1回で済むはずが追加で呼ばれた"
    print("✅ [14] 1位が一致していれば no 判定でも引き直さない")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                failed += 1
                print(f"❌ {name}: {e}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    if failed:
        print(f"\nFAILURE: {failed}/{total} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {total} tests passed")
