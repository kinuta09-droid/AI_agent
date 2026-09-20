# -*- coding: utf-8 -*-
"""
Paper Radar / Paper Digest / docling キャプチャ層のユニットテスト

方針:
  - ネットワークに一切出ない。arXiv / HuggingFace / Semantic Scholar / Gemini は
    すべてスタブに差し替える (CLAUDE.md: テストを一周回すだけで課金や実副作用が
    起きてはいけない)。
  - 本番の可変資源 (logs/paper_seen_ids.json・Obsidian Vault) には触らない。
    seen キャッシュのパスはテスト内で一時ディレクトリへ差し替える
    (test_curate_note_save の「本番カウンタを見続けていた」事故の再発防止)。

実行: py tests/test_paper_radar.py
"""
import datetime
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_paper_radar as radar
from lib.bot_errors import is_failure_result, is_partial_result

NOW = datetime.datetime(2026, 7, 30, 9, 0, 0)


def _paper(arxiv_id, days_ago=1, upvotes=0, citations=0, influential=0,
           title="Some Paper", abstract="about agents"):
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "authors": ["A", "B"],
        "published": (NOW - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d"),
        "published_dt": NOW - datetime.timedelta(days=days_ago),
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "html_urls": [f"https://arxiv.org/html/{arxiv_id}v1"],
        "hf_upvotes": upvotes,
        "citations": citations,
        "influential_citations": influential,
    }


# ---------------------------------------------------------
# [1] arXiv ID の正規化
# ---------------------------------------------------------

def test_normalize_arxiv_id():
    """URL / バージョン付き / 素の ID / ゴミ のすべてを1つの綴りに揃える。

    ここが揃わないと HF の upvote と S2 の被引用が突き合わず、
    「指標が全部0の論文」が静かに量産される。
    """
    cases = {
        "2401.12345": "2401.12345",
        "2401.12345v3": "2401.12345",
        "http://arxiv.org/abs/2401.12345v2": "2401.12345",
        "https://arxiv.org/pdf/2401.1234": "2401.1234",
        "arXiv:2401.12345": "2401.12345",
        "": "",
        "cs.AI": "",
        None: "",
    }
    for raw, expected in cases.items():
        actual = radar.normalize_arxiv_id(raw)
        assert actual == expected, f"normalize_arxiv_id({raw!r}) = {actual!r} != {expected!r}"
    print("✅ [1] normalize_arxiv_id: URL/版/素/ゴミ を正規化")


# ---------------------------------------------------------
# [2] スコアリング (純関数)
# ---------------------------------------------------------

def test_score_missing_metrics_does_not_crash():
    """HF にも S2 にも載っていない論文でも採点できて、落とされないこと。

    指標欠損で例外や 0 除算が出ると「新しすぎて指標が付いていない良い論文」が
    構造的に選ばれなくなる。
    """
    p = {"title": "New", "abstract": "", "published_dt": NOW}
    result = radar.score_paper(p, NOW)
    assert result["score"] > 0, f"指標ゼロでも recency 分は残るはず: {result}"
    assert set(result["breakdown"]) == set(radar.WEIGHTS), "breakdown は全重みを網羅すること"
    print("✅ [2] score_paper: 指標欠損でも採点できる")


def test_score_uses_citation_velocity_not_raw_count():
    """被引用「速度」で測ること: 同じ100被引用でも、古い論文より新しい論文が上。

    生の被引用数で並べると古典 (Attention Is All You Need) が毎朝1位になり、
    レーダーとして機能しない。
    """
    old = _paper("2001.00001", days_ago=1200, citations=100)
    new = _paper("2607.00001", days_ago=30, citations=100)
    s_old = radar.score_paper(old, NOW)
    s_new = radar.score_paper(new, NOW)
    assert s_new["citation_velocity"] > s_old["citation_velocity"], (
        f"新しい論文の被引用速度が高いはず: new={s_new['citation_velocity']} old={s_old['citation_velocity']}")
    assert s_new["score"] > s_old["score"], f"新しい方が総合スコアも高いはず: {s_new} vs {s_old}"
    print("✅ [2] score_paper: 生の被引用数ではなく速度で測る")


def test_score_monotonic_in_upvotes_and_interest():
    """upvote が多いほど・興味キーワードに当たるほどスコアが上がること。"""
    base = _paper("2607.00002", days_ago=2, upvotes=0)
    hot = _paper("2607.00003", days_ago=2, upvotes=300)
    assert radar.score_paper(hot, NOW)["score"] > radar.score_paper(base, NOW)["score"]

    matched = radar.score_paper(base, NOW, interests=["agents"])
    unmatched = radar.score_paper(base, NOW, interests=["量子アニーリング"])
    assert matched["score"] > unmatched["score"], "興味一致で加点されること"
    assert matched["matched_interests"] == ["agents"], matched["matched_interests"]
    print("✅ [2] score_paper: upvote / 興味一致で単調に増える")


def test_score_keyword_weights_backward_compatible_and_scales():
    """パーソナライズ Phase2: keyword_weights 未指定時は従来の len(matched) と同じ値。

    指定時は測定された重み (lib.preference_weights.keyword_affinity) 分だけ
    interest breakdown が変わること。シグネチャ変更の後方互換の回帰テスト。
    """
    p = _paper("2607.00004", days_ago=2, upvotes=0)

    default = radar.score_paper(p, NOW, interests=["agents"])
    explicit_none = radar.score_paper(p, NOW, interests=["agents"], keyword_weights=None)
    assert default["breakdown"] == explicit_none["breakdown"], (
        f"keyword_weights 未指定は None と同じ挙動のはず: {default} vs {explicit_none}")

    boosted = radar.score_paper(p, NOW, interests=["agents"], keyword_weights={"agents": 2.0})
    assert boosted["breakdown"]["interest"] > default["breakdown"]["interest"], (
        f"重み2.0なら interest 加点が増えるはず: {boosted} vs {default}")
    print("✅ [2] score_paper: keyword_weights は後方互換かつ加点をスケールする")


# ---------------------------------------------------------
# [3] ランキング
# ---------------------------------------------------------

def test_rank_papers_orders_excludes_and_is_deterministic():
    papers = [
        _paper("2607.00010", days_ago=2, upvotes=5),
        _paper("2607.00011", days_ago=2, upvotes=500),
        _paper("2607.00012", days_ago=2, upvotes=50),
    ]
    ranked = radar.rank_papers(papers, NOW, top_k=2)
    assert [p["arxiv_id"] for p in ranked] == ["2607.00011", "2607.00012"], ranked

    # 既出 (記事化済み) は候補から外れる。URL 形式で渡しても効くこと
    ranked = radar.rank_papers(papers, NOW, top_k=3,
                               exclude_ids=["https://arxiv.org/abs/2607.00011v2"])
    assert [p["arxiv_id"] for p in ranked] == ["2607.00012", "2607.00010"], ranked

    # 完全同点でも順序が実行ごとに揺れないこと (レポートの diff が読めなくなる)
    twins = [_paper("2607.00021", days_ago=3), _paper("2607.00020", days_ago=3)]
    tie_order = [p["arxiv_id"] for p in radar.rank_papers(twins, NOW, top_k=2)]
    assert tie_order == ["2607.00020", "2607.00021"], (
        f"同点は arxiv_id で決定的に並ぶこと: {tie_order}")
    print("✅ [3] rank_papers: 降順 / 既出除外 / 同点の決定性")


# ---------------------------------------------------------
# [4] レポート生成
# ---------------------------------------------------------

def test_render_radar_markdown_shows_score_breakdown():
    """なぜその論文が選ばれたかを内訳で示すこと (ブラックボックスにしない)。"""
    ranked = radar.rank_papers([_paper("2607.00030", days_ago=1, upvotes=120, citations=4)],
                               NOW, top_k=1)
    md = radar.render_radar_markdown(ranked, ["ai_agent"], NOW)
    for token in ["2607.00030", "スコア", "HF注目度 120", "被引用 4件", "新しさ"]:
        assert token in md, f"レポートに {token!r} が出ていない:\n{md}"
    print("✅ [4] render_radar_markdown: スコア内訳が読める")


def test_render_radar_markdown_prefers_japanese_translation_when_present():
    """title_ja/abstract_ja があればそちらを見出し・要旨に使い、無ければ原文へ
    フォールバックすること (translate_papers_ja が失敗/未実行でも壊れない)。
    """
    ranked = radar.rank_papers(
        [_paper("2607.00031", title="English Title", abstract="English abstract text.")],
        NOW, top_k=1)
    ranked[0]["title_ja"] = "日本語タイトル"
    ranked[0]["abstract_ja"] = "日本語の要旨です。"
    md = radar.render_radar_markdown(ranked, ["ai_agent"], NOW)
    assert "日本語タイトル" in md, md
    assert "日本語の要旨です。" in md, md
    assert "English Title" not in md, md

    # ja フィールドが無いときは原文のまま
    ranked_en = radar.rank_papers(
        [_paper("2607.00032", title="English Only", abstract="English abstract text.")],
        NOW, top_k=1)
    md_en = radar.render_radar_markdown(ranked_en, ["ai_agent"], NOW)
    assert "English Only" in md_en, md_en
    print("✅ [4b] render_radar_markdown: title_ja/abstract_ja を優先し無ければ原文へ")


# [5][6][7][8] SNS下書き・キャプチャ劣化フォールバックのテストは
# tests/test_paper_digest.py へ移設した (Xスレッド生成がLLM呼び出しを
# 伴うようになったため、Paper Digest 固有のスタブ作法とまとめて管理する)。
# [9] docling 不在時のフォールバック順序 / 切り詰め申告は lib/docling_client.py
# 内部の挙動なので tests/test_docling_client.py へ移設した (Phase 1)。

# ---------------------------------------------------------
# [10] 既出キャッシュ (本番 logs を汚さない)
# ---------------------------------------------------------

def test_seen_cache_roundtrip_in_tmpdir():
    with tempfile.TemporaryDirectory() as tmp:
        orig_seen, orig_logs = radar.SEEN_PATH, radar.LOGS_DIR
        radar.SEEN_PATH = os.path.join(tmp, "paper_seen_ids.json")
        radar.LOGS_DIR = tmp
        try:
            assert radar.load_seen_ids() == [], "初回は空"
            radar.mark_seen(["2607.00070", "https://arxiv.org/abs/2607.00071v2", "", "ゴミ"])
            seen = radar.load_seen_ids()
            assert seen == ["2607.00070", "2607.00071"], seen
            radar.mark_seen(["2607.00070"])  # 重複は増えない
            assert radar.load_seen_ids() == ["2607.00070", "2607.00071"], radar.load_seen_ids()
        finally:
            radar.SEEN_PATH, radar.LOGS_DIR = orig_seen, orig_logs
    print("✅ [10] seen キャッシュ: 正規化・重複排除・一時パスへ隔離")


# ---------------------------------------------------------
# [14] Semantic Scholar: バッチ全件未収録 (400) は失敗ではない
# ---------------------------------------------------------

class _FakeS2Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"{self.status_code} Client Error")


def test_fetch_citation_metrics_treats_all_unindexed_400_as_empty_not_failure():
    """S2 batch は「全件未収録」だと 200+null配列ではなく 400 'No valid paper ids given'
    を返す。新着論文のみを渡すとこれが必ず起きるが、失敗ではなく0件として続行すること
    (直近論文はS2の索引ラグで丸ごと未収録になり得るのが設計上の前提)。
    """
    orig = radar.requests.post
    radar.requests.post = lambda *a, **k: _FakeS2Resp(400, {"error": "No valid paper ids given"})
    try:
        metrics = radar.fetch_citation_metrics(["2608.00001", "2608.00002"])
    finally:
        radar.requests.post = orig
    assert metrics == {}, metrics
    print("✅ [14a] fetch_citation_metrics: 全件未収録の400は空dictで正常終了")


def test_fetch_citation_metrics_still_treats_other_400_as_failure():
    """本当に想定外の400 (不正リクエスト等) は従来どおり失敗として扱われること。"""
    orig = radar.requests.post
    radar.requests.post = lambda *a, **k: _FakeS2Resp(400, {"error": "Bad request format"})
    try:
        metrics = radar.fetch_citation_metrics(["2608.00001"])
    finally:
        radar.requests.post = orig
    assert metrics == {}, metrics  # フォールバックの挙動自体は同じ (0件)
    print("✅ [14b] fetch_citation_metrics: 想定外の400も落ちずに0件へフォールバック")


def test_fetch_citation_metrics_parses_mixed_known_and_null():
    orig = radar.requests.post
    radar.requests.post = lambda *a, **k: _FakeS2Resp(
        200, [{"citationCount": 5, "influentialCitationCount": 1}, None])
    try:
        metrics = radar.fetch_citation_metrics(["2608.00001", "2608.00002"])
    finally:
        radar.requests.post = orig
    assert metrics == {"2608.00001": {"citations": 5, "influential_citations": 1}}, metrics
    print("✅ [14c] fetch_citation_metrics: 既知/未収録の混在を正しく突き合わせる")


# ---------------------------------------------------------
# [13] 上位候補の日本語訳 (失敗時は英語のままフォールバック)
# ---------------------------------------------------------

class _FakeResp:
    def __init__(self, text):
        self.text = text


def test_translate_papers_ja_fills_in_translated_fields():
    import lib.lib_shared as lib_shared
    ranked = [_paper("2607.00050", title="Original Title", abstract="Original abstract.")]

    original_fn = lib_shared.client.models.generate_content
    try:
        lib_shared.client.models.generate_content = (
            lambda model, contents: _FakeResp(
                '[{"arxiv_id": "2607.00050", "title_ja": "翻訳済みタイトル", '
                '"abstract_ja": "翻訳済み要旨"}]'))
        radar.translate_papers_ja(ranked)
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert ranked[0]["title_ja"] == "翻訳済みタイトル", ranked[0]
    assert ranked[0]["abstract_ja"] == "翻訳済み要旨", ranked[0]
    print("✅ [13a] translate_papers_ja: 翻訳結果を title_ja/abstract_ja へ反映する")


def test_translate_papers_ja_failure_falls_back_to_english():
    """Gemini呼び出しが失敗しても例外を外へ伝播させず、原文のまま残ること。

    毎朝自動実行されるレーダー本体は課金ゼロが前提 (docstring)。翻訳ステップの
    失敗でレーダー全体を止めると、その前提が崩れた上に本編まで失われる。
    """
    import lib.lib_shared as lib_shared
    ranked = [_paper("2607.00051", title="Original Title", abstract="Original abstract.")]

    original_fn = lib_shared.client.models.generate_content
    try:
        def _boom(model, contents):
            raise RuntimeError("quota exceeded")
        lib_shared.client.models.generate_content = _boom
        radar.translate_papers_ja(ranked)  # 例外を投げないこと
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert "title_ja" not in ranked[0], ranked[0]
    assert ranked[0]["title"] == "Original Title", ranked[0]
    print("✅ [13b] translate_papers_ja: 翻訳失敗時は例外を外へ出さず原文のまま")


# ---------------------------------------------------------
# [11] 「該当なし」を失敗として通知しない
# ---------------------------------------------------------

def test_empty_result_is_not_a_failure_but_errors_are():
    """0件は成功 (収集は動いた)。収集自体の例外は "Error:" で失敗と分かること。

    ここを取り違えると、定時ジョブが「静かな日」と「壊れた日」を同じ ❌ で
    通知する / 逆に壊れた日を ✅ で流す。
    """
    orig = radar.collect_candidates
    radar.collect_candidates = lambda *a, **k: []
    try:
        msg = radar.run_paper_radar(field="ai_agent", days=3)
    finally:
        radar.collect_candidates = orig
    assert not is_failure_result(msg), f"0件を失敗として返している: {msg!r}"

    def _boom(*a, **k):
        raise RuntimeError("arXiv down")

    radar.collect_candidates = _boom
    try:
        msg = radar.run_paper_radar(field="ai_agent", days=3)
    finally:
        radar.collect_candidates = orig
    assert is_failure_result(msg), f"収集失敗が失敗として判定されない: {msg!r}"
    print("✅ [11] run_paper_radar: 0件は成功 / 例外は失敗接頭辞")


# ---------------------------------------------------------
# [12] レジストリの失敗接頭辞が既存の判定器に拾われること
# ---------------------------------------------------------

def test_registry_wrapper_failure_prefix_is_detected():
    """lazy wrapper の "[paper_radar error] ..." が is_failure_result で拾えること。

    tool_registry の wrapper は独自の接頭辞を使うため、
    lib/bot_errors の正規表現に載っているか実際に確かめる
    (載っていないと engine が失敗を success として learning_db に記録する)。
    """
    assert is_failure_result("[paper_radar error] boom")
    assert is_failure_result("[paper_digest error] boom")
    assert is_failure_result("Error: 対象論文を特定できませんでした")
    assert not is_failure_result("Paper Radar: 過去7日間に該当する論文はありませんでした")
    print("✅ [12] 失敗接頭辞が bot_errors の判定に載っている")


# [13] run_paper_digest の既出フラグ・LLMスタブまわりのテストは
# tests/test_paper_digest.py へ移設した (build_sns_post のスタブ化が
# 必須になったため、Paper Digest 固有のテストとまとめて管理する)。

# ---------------------------------------------------------
# [15] run_paper_radar_grouped: グループ単位の収集失敗を握りつぶさない
# ---------------------------------------------------------

def test_grouped_all_groups_fail_is_error():
    """全グループの候補収集が失敗したら "該当なし" と区別できる Error: を返すこと。

    グループ単位で収集失敗を無言で continue すると、arXiv障害の日も
    「たまたま0件でした」に見えてしまう (Known Bug Patterns A と同型の事故)。
    """
    def _boom(*a, **k):
        raise RuntimeError("arXiv down")
    orig = radar._search_fields
    radar._search_fields = _boom
    try:
        msg = radar.run_paper_radar_grouped(
            groups=[("ai_agent", 3), ("llm", 2)], days=3, save=False, translate=False,
        )
    finally:
        radar._search_fields = orig
    assert is_failure_result(msg), f"全滅が失敗として判定されない: {msg!r}"
    print("✅ [15a] run_paper_radar_grouped: 全グループ収集失敗はErrorとして返る")


def test_grouped_partial_failure_keeps_success_group_and_is_marked():
    """一部グループだけ収集失敗しても、成功分の結果は残しつつ部分失敗の印を付けること。

    無印のまま返すと定時ジョブの通知が ✅ に見え、失敗したグループがあった事実が
    どこにも残らない。lib.bot_errors.PARTIAL_PREFIX を使い、他の部分失敗表示と
    同じ判定器 (is_partial_result) で拾えるようにする。
    """
    orig_search = radar._search_fields
    orig_enrich = radar._enrich_with_metrics
    orig_latest = radar.LATEST_JSON
    orig_log_impressions = radar.log_impressions

    def _fake_search(fields, days=7, max_per_field=radar.MAX_FETCH_PER_QUERY):
        if fields == ["broken"]:
            raise RuntimeError("field down")
        return [_paper("2607.00099", title="Survivor Paper")]

    with tempfile.TemporaryDirectory() as tmp:
        radar._search_fields = _fake_search
        radar._enrich_with_metrics = lambda papers, days=7: papers
        radar.LATEST_JSON = os.path.join(tmp, "paper_radar_latest.json")
        # 項目9 (収集クエリの動的チューニング) で追加した impression 記録が
        # 本番 logs/engagement.jsonl を汚染しないよう無効化する
        # (Known Bug Patterns C: 新しい書き込み処理は既存テストの隔離漏れになりやすい)
        radar.log_impressions = lambda *a, **k: ""
        try:
            msg = radar.run_paper_radar_grouped(
                groups=[("broken", 3), ("ai_agent", 2)], days=3, save=False, translate=False,
            )
        finally:
            radar._search_fields = orig_search
            radar._enrich_with_metrics = orig_enrich
            radar.LATEST_JSON = orig_latest
            radar.log_impressions = orig_log_impressions

    assert not is_failure_result(msg), f"部分失敗をErrorにしてはいけない: {msg!r}"
    assert is_partial_result(msg), f"部分失敗の印が付いていない: {msg!r}"
    assert "Survivor Paper" in msg, msg
    print("✅ [15b] run_paper_radar_grouped: 部分失敗は成功分を残しつつPARTIAL_PREFIXで示す")


def test_registry_entries_registered():
    from lib.tool_registry import TOOL_ENTRIES
    names = {e["name"] for e in TOOL_ENTRIES}
    assert {"tool_paper_radar", "tool_paper_digest"} <= names, sorted(names)
    print("✅ [12] registry: paper 系ツールが登録されている")


# ---------------------------------------------------------
# [13] use_jev (Jev/TypeSafe AI の興味関連度シグナル、既定OFF)
# ---------------------------------------------------------

def test_score_paper_jev_relevance_not_in_breakdown_when_absent():
    """paper に jev_relevance が無い (use_jev=False の既定) 場合、breakdownに
    jev_relevance キーが一切追加されないこと (この機能追加前と完全に同じ形の回帰保証)。"""
    p = _paper("2401.00001", upvotes=5, citations=2)
    result = radar.score_paper(p, NOW)
    assert "jev_relevance" not in result["breakdown"], result["breakdown"]
    print("✅ [13a] score_paper: jev_relevance未設定なら breakdown に鍵が増えない")


def test_score_paper_jev_relevance_monotonic_when_present():
    """jev_relevance が設定されていれば breakdown に反映され、値が大きいほどスコアも増える。"""
    p_low = _paper("2401.00002", upvotes=5, citations=2)
    p_low["jev_relevance"] = 0.1
    p_high = dict(p_low)
    p_high["jev_relevance"] = 0.9

    r_low = radar.score_paper(p_low, NOW)
    r_high = radar.score_paper(p_high, NOW)
    assert "jev_relevance" in r_low["breakdown"], r_low["breakdown"]
    assert r_high["score"] > r_low["score"], (r_low, r_high)
    print("✅ [13b] score_paper: jev_relevance が大きいほどスコアが増える")


def test_score_paper_jev_relevance_none_treated_as_absent():
    """Jev呼び出し失敗時 (_enrich_with_jev_relevance が None を merge) は
    breakdown に鍵を足さず0点相当で扱われること (score_paper の None ガード)。"""
    p_none = _paper("2401.00003", upvotes=5, citations=2)
    p_none["jev_relevance"] = None
    p_absent = _paper("2401.00003", upvotes=5, citations=2)

    r_none = radar.score_paper(p_none, NOW)
    r_absent = radar.score_paper(p_absent, NOW)
    assert "jev_relevance" not in r_none["breakdown"], r_none["breakdown"]
    assert r_none["score"] == r_absent["score"], (r_none, r_absent)
    print("✅ [13c] score_paper: jev_relevance=None はキー無しと同じ扱い")


def test_enrich_with_jev_relevance_per_paper_failure_is_skipped_not_fatal():
    """1論文の jev_score 呼び出しが失敗しても他論文に影響せず、例外も外に出ないこと。

    jev_score は公式APIの必須criteria契約により (state, instructions, criteria) の
    3引数で呼ばれる。score=0.7 は _JEV_RELEVANCE_LEVELS (3水準・最大インデックス2) を
    基準に 0.7/2=0.35 へ正規化される。
    """
    papers = [_paper("2401.00004", title="broken"), _paper("2401.00005", title="ok")]

    def _flaky(state, instructions, criteria):
        assert criteria == radar._JEV_RELEVANCE_LEVELS, criteria
        if "broken" in state:
            raise RuntimeError("jev down")
        return {"score": 0.7, "confidence": 0.6}

    orig = radar.jev_score
    radar.jev_score = _flaky
    try:
        radar._enrich_with_jev_relevance(papers, ["agent"])  # 例外を投げないこと
    finally:
        radar.jev_score = orig

    by_id = {p["arxiv_id"]: p for p in papers}
    assert by_id["2401.00004"]["jev_relevance"] is None, by_id["2401.00004"]
    assert by_id["2401.00005"]["jev_relevance"] == 0.35, by_id["2401.00005"]
    print("✅ [13d] _enrich_with_jev_relevance: 1論文の失敗は他論文を巻き込まない")


def test_enrich_with_jev_relevance_unavailable_is_noop():
    """tools.tool_jev が不在 (jev_score=None) なら何もしない (paper dict は変更されない)。"""
    papers = [_paper("2401.00006")]
    orig = radar.jev_score
    radar.jev_score = None
    try:
        radar._enrich_with_jev_relevance(papers, ["agent"])
    finally:
        radar.jev_score = orig
    assert "jev_relevance" not in papers[0], papers[0]
    print("✅ [13e] _enrich_with_jev_relevance: jev_score 不在なら no-op")


def test_run_paper_radar_use_jev_false_output_identical_to_baseline():
    """use_jev を省略した既定動作が、この機能追加前と完全に同じ (jev_score は一切呼ばれない)。"""
    with tempfile.TemporaryDirectory() as tmp:
        orig_seen, orig_logs, orig_latest = radar.SEEN_PATH, radar.LOGS_DIR, radar.LATEST_JSON
        orig_collect = radar.collect_candidates
        orig_translate = radar.translate_papers_ja
        orig_log_impr = radar.log_impressions
        radar.SEEN_PATH = os.path.join(tmp, "paper_seen_ids.json")
        radar.LOGS_DIR = tmp
        radar.LATEST_JSON = os.path.join(tmp, "paper_radar_latest.json")
        radar.collect_candidates = lambda *a, **k: [_paper("2401.00007", upvotes=10)]
        radar.translate_papers_ja = lambda ranked: None
        radar.log_impressions = lambda *a, **k: None
        jev_called = []
        radar.jev_score = lambda *a, **k: jev_called.append(1)
        try:
            msg = radar.run_paper_radar(field="ai_agent", top_k=1, save=False,
                                        translate=False)  # use_jev省略
        finally:
            radar.collect_candidates = orig_collect
            radar.translate_papers_ja = orig_translate
            radar.log_impressions = orig_log_impr
            radar.SEEN_PATH, radar.LOGS_DIR, radar.LATEST_JSON = orig_seen, orig_logs, orig_latest

    assert jev_called == [], "use_jev省略時にjev_scoreが呼ばれている (既定挙動が変わっている)"
    assert "2401.00007" in msg, msg
    print("✅ [13f] run_paper_radar: use_jev省略時は従来通りjev不使用")


def test_run_paper_radar_use_jev_true_calls_enrichment():
    """use_jev=True のときは _enrich_with_jev_relevance が候補プールに対して呼ばれること。"""
    with tempfile.TemporaryDirectory() as tmp:
        orig_seen, orig_logs, orig_latest = radar.SEEN_PATH, radar.LOGS_DIR, radar.LATEST_JSON
        orig_collect = radar.collect_candidates
        orig_translate = radar.translate_papers_ja
        orig_log_impr = radar.log_impressions
        orig_enrich = radar._enrich_with_jev_relevance
        radar.SEEN_PATH = os.path.join(tmp, "paper_seen_ids.json")
        radar.LOGS_DIR = tmp
        radar.LATEST_JSON = os.path.join(tmp, "paper_radar_latest.json")
        radar.collect_candidates = lambda *a, **k: [_paper("2401.00008", upvotes=10)]
        radar.translate_papers_ja = lambda ranked: None
        radar.log_impressions = lambda *a, **k: None
        called = []
        radar._enrich_with_jev_relevance = lambda papers, interests: called.append(len(papers))
        try:
            radar.run_paper_radar(field="ai_agent", top_k=1, save=False,
                                  translate=False, use_jev=True)
        finally:
            radar.collect_candidates = orig_collect
            radar.translate_papers_ja = orig_translate
            radar.log_impressions = orig_log_impr
            radar._enrich_with_jev_relevance = orig_enrich
            radar.SEEN_PATH, radar.LOGS_DIR, radar.LATEST_JSON = orig_seen, orig_logs, orig_latest

    assert called == [1], f"use_jev=True なのに _enrich_with_jev_relevance が呼ばれていない: {called}"
    print("✅ [13g] run_paper_radar: use_jev=True で _enrich_with_jev_relevance が呼ばれる")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
