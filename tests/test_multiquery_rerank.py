# -*- coding: utf-8 -*-
"""
Phase A2 (multi-query 書き換え / LLMリランク / 出典) のユニットテスト
(keyword_fn / semantic_fn / llm_fn 注入 — ネットワーク・Vault実体なし)

このファイルが守るもの:
  A2 の3施策はどれも「失敗したら黙って元の挙動に戻る」設計なので、
  壊れても例外が出ない = 気付けない。特に危ないのは以下の3つで、
  それぞれ専用のケースを置いてある。
    - LLM が変な物を返したときに検索ごと死なないか (1・5)
    - リランクが「並べ替え」ではなく「足切り」に退化していないか (4)
    - 劣化したときに理由が残るか / 設定OFFを事故と混同していないか (6・8)

RRF 融合そのものの回帰は tests/test_hybrid_search.py が別に見ている。

実行: py tests/test_multiquery_rerank.py
"""
import os
import sys

# A2 の機能は llm_fn 注入で検証するため、環境変数側は明示的に ON にしておく
# (実行環境の .env で OFF になっていてもテストの意味が変わらないように)。
# テストは専用プロセスで走るのでこの代入はプロセス内で閉じる。
os.environ["VAULT_SEARCH_MULTIQUERY"] = "1"
os.environ["VAULT_SEARCH_RERANK"] = "1"

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.lib_obsidian_utils import format_wikilink, sanitize_heading, split_heading_anchor
from tools.tool_vault_hybrid import (
    _expand_queries, _llm_rerank, _hybrid_hits, tool_vault_hybrid_search,
)


def _hit(rel_path, score, snippet="snippet-body", heading=None, chunk_index=0):
    return {"rel_path": rel_path, "filename": os.path.basename(rel_path),
            "score": score, "snippet": snippet, "heading": heading,
            "chunk_id": f"{rel_path}#{chunk_index}"}


def _cand(rel_path, rrf, heading=None):
    return {"rel_path": rel_path, "filename": os.path.basename(rel_path),
            "rrf": rrf, "sources": ["semantic"], "query_ids": [1],
            "heading": heading, "chunk_id": f"{rel_path}#0",
            "snippet": f"body of {rel_path}", "rerank_pos": None}


class _Spy:
    """呼ばれた回数と渡されたプロンプトを記録する llm_fn スタブ"""

    def __init__(self, reply='["改善 履歴", "quant_core ATR"]'):
        self.reply = reply
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.reply if isinstance(self.reply, str) else self.reply(prompt)


# ---------------------------------------------------------
# 1. 書き換えは何があっても原クエリに戻る (例外を投げない)
# ---------------------------------------------------------

def test_expand_never_raises():
    def boom(prompt):
        raise RuntimeError("Gemini exploded")

    for name, llm in [
        ("例外", boom),
        ("空文字", lambda p: ""),
        ("JSONでない", lambda p: "すみません、わかりません"),
        ("空配列", lambda p: "[]"),
        ("配列でないJSON", lambda p: '{"queries": "a"}'),
        ("予算超過", _budget_exceeded),
    ]:
        queries, note = _expand_queries("元の質問", llm_fn=llm)
        assert queries == ["元の質問"], f"{name}: {queries}"
        assert note, f"{name}: フォールバック理由が空 (無言で劣化している)"
    print("✅ expand: 例外/空/非JSON/空配列/予算超過 → 原クエリのみ + 理由あり")


def _budget_exceeded(prompt):
    """cost_guard の日次上限に当たったケース。検索まで巻き込んで落ちてはいけない。"""
    from lib.cost_guard import GeminiBudgetExceeded
    raise GeminiBudgetExceeded("daily limit reached")


def test_expand_returns_original_first():
    """原クエリは必ず先頭。重複した変種は捨てる"""
    spy = _Spy('["元の質問", "改善 履歴", "改善 履歴", "quant_core"]')
    queries, note = _expand_queries("元の質問", llm_fn=spy, n=2)
    assert queries[0] == "元の質問", queries
    assert queries == ["元の質問", "改善 履歴", "quant_core"], queries
    assert note == "", note
    assert "元の質問" in spy.calls[0], "プロンプトに原クエリが入っていない"
    print("✅ expand: 原クエリが先頭・重複除去・n件で打ち切り")


# ---------------------------------------------------------
# 2. 展開した各クエリが融合に効く
# ---------------------------------------------------------

def test_variant_only_hit_enters_candidates():
    """変種クエリでしか当たらないファイルも候補に入る (multi-query の存在意義)"""
    per_query = {
        "元の質問": [],                       # 原クエリでは1件も当たらない
        "quant_core ATR": [_hit("30_Main/target.md", 0.9, heading="Phase3")],
    }
    hits, notes, queries = _hybrid_hits(
        "元の質問", top_k=5,
        keyword_fn=lambda q: [],
        semantic_fn=lambda q, k: per_query.get(q, []),
        llm_fn=_Spy('["quant_core ATR"]'))

    assert queries == ["元の質問", "quant_core ATR"], queries
    paths = [h["rel_path"] for h in hits]
    assert "30_Main/target.md" in paths, f"変種でしか当たらないファイルが落ちた: {paths}"
    print("✅ multi-query: 変種でしか当たらないファイルも候補に入る")


# ---------------------------------------------------------
# 3〜5. リランクの挙動
# ---------------------------------------------------------

def test_rerank_reorders():
    cands = [_cand("a.md", 0.03), _cand("b.md", 0.02), _cand("c.md", 0.01)]
    ordered, note = _llm_rerank("質問", cands, llm_fn=lambda p: "[3, 1, 2]")
    assert [h["rel_path"] for h in ordered] == ["c.md", "a.md", "b.md"], ordered
    assert ordered[0]["rerank_pos"] == 1 and ordered[1]["rerank_pos"] == 2
    assert note == "", note
    print("✅ rerank: RRF順を LLM の判断で並べ替える")


def test_rerank_does_not_drop_candidates():
    """LLM が挙げなかった候補は捨てない (並べ替えであって足切りではない)"""
    cands = [_cand("a.md", 0.03), _cand("b.md", 0.02), _cand("c.md", 0.01)]
    ordered, note = _llm_rerank("質問", cands, llm_fn=lambda p: "[2]")
    assert len(ordered) == 3, f"候補が削られた: {ordered}"
    assert ordered[0]["rel_path"] == "b.md", ordered
    # 選から漏れた分は RRF 順のまま後ろに付く
    assert [h["rel_path"] for h in ordered[1:]] == ["a.md", "c.md"], ordered
    assert ordered[1]["rerank_pos"] is None, "未選択に順位を付けてはいけない"
    print("✅ rerank: 未選択の候補も RRF 順で末尾に残す (足切りにしない)")


def test_rerank_survives_garbage_indices():
    """存在しない番号・重複・非数値・0/負数を返されても落ちない"""
    cands = [_cand("a.md", 0.03), _cand("b.md", 0.02)]
    ordered, note = _llm_rerank("質問", cands, llm_fn=lambda p: '[99, 2, 2, "三位", 0, -1]')
    assert [h["rel_path"] for h in ordered] == ["b.md", "a.md"], ordered
    assert note == "", note

    # 有効な番号が1つも無ければ RRF 順のまま + 理由を残す
    ordered, note = _llm_rerank("質問", cands, llm_fn=lambda p: "[99, 100]")
    assert [h["rel_path"] for h in ordered] == ["a.md", "b.md"], ordered
    assert note, "有効番号ゼロなのに理由が残っていない"

    # 例外・非JSONでも同じ
    def boom(p):
        raise ValueError("bad")
    ordered, note = _llm_rerank("質問", cands, llm_fn=boom)
    assert [h["rel_path"] for h in ordered] == ["a.md", "b.md"], ordered
    assert note, "例外なのに理由が残っていない"
    print("✅ rerank: 不正な番号・例外でも RRF 順にフォールバック")


# ---------------------------------------------------------
# 6. フラグ OFF なら LLM を一度も呼ばない
# ---------------------------------------------------------

def test_flags_off_never_call_llm():
    def must_not_be_called(prompt):
        raise AssertionError("フラグOFFなのに LLM が呼ばれた")

    saved = (os.environ.get("VAULT_SEARCH_MULTIQUERY"),
             os.environ.get("VAULT_SEARCH_RERANK"))
    try:
        for value in ("0", "false", "OFF", "no"):
            os.environ["VAULT_SEARCH_MULTIQUERY"] = value
            os.environ["VAULT_SEARCH_RERANK"] = value
            hits, notes, queries = _hybrid_hits(
                "質問", top_k=3,
                keyword_fn=lambda q: [_hit("a.md", 2)],
                semantic_fn=lambda q, k: [],
                llm_fn=must_not_be_called)
            assert queries == ["質問"], f"{value}: {queries}"
            assert [h["rel_path"] for h in hits] == ["a.md"], hits
            # 設定OFF は事故ではないので戻り値のノートを汚さない (ログには出る)
            assert notes == [], f"{value}: OFF が note に混ざっている: {notes}"
    finally:
        os.environ["VAULT_SEARCH_MULTIQUERY"], os.environ["VAULT_SEARCH_RERANK"] = saved
    print("✅ flags: OFF のとき LLM を呼ばず、note も汚さない")


def test_explicit_override_beats_env():
    """明示引数は .env より優先される (eval の A/B 計測が環境に左右されないこと)"""
    def must_not_be_called(prompt):
        raise AssertionError("override=False なのに LLM が呼ばれた")

    saved = (os.environ.get("VAULT_SEARCH_MULTIQUERY"),
             os.environ.get("VAULT_SEARCH_RERANK"))
    try:
        # .env が ON でも override=False が勝つ
        os.environ["VAULT_SEARCH_MULTIQUERY"] = "1"
        os.environ["VAULT_SEARCH_RERANK"] = "1"
        hits, notes, queries = _hybrid_hits(
            "質問", top_k=3, keyword_fn=lambda q: [_hit("a.md", 2)],
            semantic_fn=lambda q, k: [], llm_fn=must_not_be_called,
            multiquery=False, rerank=False)
        assert queries == ["質問"] and notes == [], (queries, notes)

        # .env が OFF でも override=True が勝つ
        os.environ["VAULT_SEARCH_MULTIQUERY"] = "0"
        os.environ["VAULT_SEARCH_RERANK"] = "0"
        spy = _Spy('["言い換え"]')
        hits, notes, queries = _hybrid_hits(
            "質問", top_k=3, keyword_fn=lambda q: [_hit("a.md", 2)],
            semantic_fn=lambda q, k: [], llm_fn=spy,
            multiquery=True, rerank=True)
        assert queries == ["質問", "言い換え"], queries
        assert len(spy.calls) == 2, f"書き換え+リランクで2回呼ばれるはず: {len(spy.calls)}"
    finally:
        os.environ["VAULT_SEARCH_MULTIQUERY"], os.environ["VAULT_SEARCH_RERANK"] = saved
    print("✅ flags: 明示引数が .env を上書きする (両方向)")


# ---------------------------------------------------------
# 7. 出典リンク
# ---------------------------------------------------------

def test_citation_link_format():
    # 見出しあり → アンカー付き
    result = tool_vault_hybrid_search(
        "質問", top_k=1, keyword_fn=lambda q: [],
        semantic_fn=lambda q, k: [_hit("30_Main/foo.md", 0.9, heading="Phase3 実装")],
        llm_fn=_Spy("[1]"))
    assert "[[foo.md#Phase3 実装]]" in result, result

    # 見出しなし → プレーンリンク (壊れた '#' を出さない)
    result = tool_vault_hybrid_search(
        "質問", top_k=1, keyword_fn=lambda q: [],
        semantic_fn=lambda q, k: [_hit("30_Main/foo.md", 0.9, heading=None)],
        llm_fn=_Spy("[1]"))
    assert "[[foo.md]]" in result and "[[foo.md#" not in result, result

    # リンク構文を壊す文字はサニタイズ
    assert format_wikilink("a.md", "見出し #tag |x [y] ^z") == "[[a.md#見出し tag x y z]]"
    assert format_wikilink("a.md", "###") == "[[a.md]]", "除去後に空なら見出しなし"
    assert format_wikilink("a.md", None) == "[[a.md]]"
    assert sanitize_heading("  複数   空白 \n改行 ") == "複数 空白 改行"

    # 出す側と解決する側が対になっていること (Discordの「Obsidianで開く」対策)
    assert split_heading_anchor("foo.md#見出し") == ("foo.md", "見出し")
    assert split_heading_anchor("foo.md") == ("foo.md", "")
    print("✅ citation: [[file#heading]] の生成・サニタイズ・分解が対になっている")


# ---------------------------------------------------------
# 8. 劣化したら理由が戻り値に残る
# ---------------------------------------------------------

def test_degradation_reason_is_visible():
    """LLM が死んでも検索は返るが、なぜ劣化したかが必ず出力に残る"""
    def boom(prompt):
        raise RuntimeError("Gemini exploded")

    result = tool_vault_hybrid_search(
        "質問", top_k=3,
        keyword_fn=lambda q: [_hit("a.md", 2)],
        semantic_fn=lambda q, k: [],
        llm_fn=boom)

    assert "[[a.md]]" in result, f"検索結果が失われた:\n{result}"
    assert "クエリ書き換え: フォールバック" in result, result
    assert "LLMリランク: フォールバック" in result, result
    assert "RuntimeError" in result, "例外の型/文言が残っていない"

    # 意味検索が死んだときの既存の注記も併存すること
    result = tool_vault_hybrid_search(
        "質問", top_k=3,
        keyword_fn=lambda q: [_hit("a.md", 2)],
        semantic_fn=lambda q, k: "[ERROR] Ollama not running.",
        llm_fn=boom)
    assert "⚠️ 意味検索は利用できない" in result, result
    assert "LLMリランク: フォールバック" in result, result
    print("✅ degradation: 3層の劣化理由が同時に戻り値へ残る")


def test_search_queries_are_reported():
    """どのクエリで引いた結果かを必ず併記する (書き換え暴走の切り分け用)"""
    result = tool_vault_hybrid_search(
        "元の質問", top_k=3,
        keyword_fn=lambda q: [_hit("a.md", 2)],
        semantic_fn=lambda q, k: [],
        llm_fn=_Spy('["改善 履歴"]'))
    assert "🔎 検索クエリ:" in result, result
    assert '"元の質問"' in result and '"改善 履歴"' in result, result
    print("✅ report: 実際に検索したクエリ変種が出力に残る")


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
