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

import lib.docling_client as docling_client
import tools.tool_paper_radar as radar
from lib.bot_errors import is_failure_result

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
    for token in ["2607.00030", "スコア", "HF upvote 120", "被引用 4件", "新しさ"]:
        assert token in md, f"レポートに {token!r} が出ていない:\n{md}"
    print("✅ [4] render_radar_markdown: スコア内訳が読める")


# ---------------------------------------------------------
# [5][6] SNS 下書き (純関数)
# ---------------------------------------------------------

def test_trim_post_enforces_limit():
    import tools.tool_paper_digest as digest
    assert digest.trim_post("あ" * 200, 140) == "あ" * 139 + "…"
    assert digest.trim_post("短い", 140) == "短い"
    assert digest.trim_post("", 140) == ""
    print("✅ [5] trim_post: 文字数を機械で担保する")


def test_build_sns_thread_respects_limit_and_ends_with_link():
    """LLM の「140字以内で」を信用せず、最終的に機械で切ってあること。

    投稿が上限を超えると X 側で弾かれる = 下書きとして使えない。
    """
    import tools.tool_paper_digest as digest
    d = {
        "one_liner": "あ" * 300,
        "problem": "い" * 300,
        "method": "う" * 300,
        "results": "え" * 300,
        "implication": "お" * 300,
        "keywords": ["LLM", "エージェント", "捨てられるべき3つ目"],
    }
    paper = _paper("2607.00040", title="T")
    thread = digest.build_sns_thread(d, paper, limit=140, max_posts=5)

    assert len(thread) <= 5, f"投稿数が上限を超えている: {len(thread)}"
    for i, post in enumerate(thread[:-1], 1):
        assert len(post) <= 140, f"{i}投稿目が {len(post)} 字 (上限140): {post!r}"
    last = thread[-1]
    assert paper["abs_url"] in last, f"最後の投稿に原論文リンクが無い: {last!r}"
    body_len = len(last.split("\n")[0])
    assert body_len <= 140 - 23, f"URL(23字換算)を足すと超過する: 本文{body_len}字"
    print("✅ [6] build_sns_thread: 全投稿が上限内 / 末尾にリンク")


# ---------------------------------------------------------
# [7][8] キャプチャの劣化を「黙って」やらない
# ---------------------------------------------------------

def test_capture_paper_falls_back_to_abstract_with_explicit_backend():
    """本文が取れなかったとき backend='abstract' と警告を必ず立てること。

    ここで黙って要旨を返すと、以降の要約・記事・SNS 投稿が「全文を読んだ体」で
    出る (読者にも自分にも嘘をつく)。CLAUDE.md 所見2「無言の死」の同型。
    """
    import tools.tool_paper_digest as digest

    def _boom(*args, **kwargs):
        raise docling_client.CaptureError("stub: all backends failed")

    original = digest.capture_document
    digest.capture_document = _boom
    try:
        result = digest.capture_paper(_paper("2607.00050", abstract="要旨テキスト"))
    finally:
        digest.capture_document = original

    assert result["backend"] == "abstract", result
    assert result["text"] == "要旨テキスト", result
    assert result["warnings"], "フォールバックしたのに警告が空"
    print("✅ [7] capture_paper: 要旨フォールバックを backend で明示する")


def test_digest_markdown_marks_abstract_only_capture():
    """要旨のみで書いた要約ノートには、その旨が本文に出ること。"""
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要約", "problem": "課題", "method": "手法", "novelty": "新規性",
         "results": "結果", "limitations": "限界", "implication": "示唆", "keywords": ["k"]}
    capture = {"backend": "abstract", "chars": 100, "warnings": ["本文取得に失敗"]}
    md = digest.render_digest_markdown(d, _paper("2607.00051"), capture)
    assert "要旨のみ" in md, f"要旨のみである注記が無い:\n{md}"
    assert "本文取得に失敗" in md, f"取得時の注記が落ちている:\n{md}"

    full = digest.render_digest_markdown(
        d, _paper("2607.00051"), {"backend": "docling", "chars": 40000, "warnings": []})
    assert "docling で全文PDFを構造化" in full, full
    assert "要旨のみ" not in full, "全文で読んだのに要旨扱いの注記が出ている"
    print("✅ [8] render_digest_markdown: 取得経路が本文に必ず出る")


# ---------------------------------------------------------
# [9] docling 不在時のフォールバック順序
# ---------------------------------------------------------

def test_capture_document_falls_back_to_html_then_raises():
    """docling が無ければ HTML、HTML も駄目なら CaptureError。

    「docling が無い環境」が既定なので、この経路が黙って壊れると
    論文機能そのものが無言で死ぬ。
    """
    calls = []

    class _Resp:
        status_code = 200
        encoding = "utf-8"
        text = "<html><article>" + ("本文です。" * 200) + "</article></html>"

        def raise_for_status(self):
            return None

    def _fake_get(url, **kwargs):
        calls.append(url)
        return _Resp()

    orig_available, orig_get = docling_client.is_available, docling_client.requests.get
    docling_client.is_available = lambda: False
    docling_client.requests.get = _fake_get
    try:
        result = docling_client.capture_document(
            "https://arxiv.org/pdf/2607.00060",
            ["https://arxiv.org/html/2607.00060v1"],
            use_cache=False,
        )
        assert result["backend"] == "html", result
        assert calls == ["https://arxiv.org/html/2607.00060v1"], calls
        assert any("docling が未インストール" in w for w in result["warnings"]), result["warnings"]

        # HTML も無い → 例外 (静かに空文字を返さない)
        raised = False
        try:
            docling_client.capture_document("https://arxiv.org/pdf/x", [], use_cache=False)
        except docling_client.CaptureError:
            raised = True
        assert raised, "全経路失敗なのに CaptureError が飛んでいない"
    finally:
        docling_client.is_available = orig_available
        docling_client.requests.get = orig_get
    print("✅ [9] capture_document: docling→HTML→例外 の順に劣化する")


def test_capture_document_truncates_and_reports():
    """max_chars で切ったことを truncated と warnings で申告すること。"""
    orig_available = docling_client.is_available
    orig_convert = docling_client.convert_with_docling
    docling_client.is_available = lambda: True
    docling_client.convert_with_docling = lambda source: "x" * 5000
    try:
        result = docling_client.capture_document("dummy.pdf", [], max_chars=1000, use_cache=False)
    finally:
        docling_client.is_available = orig_available
        docling_client.convert_with_docling = orig_convert
    assert result["truncated"] is True and len(result["text"]) == 1000, result
    assert result["chars"] == 5000, "chars は切る前の全長であること"
    assert any("先頭 1000 文字" in w for w in result["warnings"]), result["warnings"]
    print("✅ [9] capture_document: 切り詰めを申告する")


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


def test_digest_marks_seen_only_when_saved():
    """--no-save の試し打ちで既出フラグを立てないこと。

    立ててしまうと「1文字も保存していない論文」がレーダーから永久に消える。
    Vault にも本番 logs にも触らないよう、保存先と seen パスを一時パスへ逃がす。
    """
    import tools.tool_paper_digest as digest

    paper = _paper("2607.00080", title="Stub Paper")
    stub_digest = {"one_liner": "要約", "problem": "課題", "method": "手法",
                   "novelty": "新規性", "results": "結果", "limitations": "限界",
                   "implication": "示唆", "keywords": ["k1"]}
    stub_capture = {"text": "本文", "backend": "docling", "chars": 4,
                    "truncated": False, "source": "", "warnings": []}

    with tempfile.TemporaryDirectory() as tmp:
        originals = (digest._resolve_paper, digest.capture_paper, digest.summarize_paper,
                     digest.PAPERS_DIR, radar.SEEN_PATH, radar.LOGS_DIR)
        digest._resolve_paper = lambda **kwargs: paper
        digest.capture_paper = lambda p, use_docling=True: stub_capture
        digest.summarize_paper = lambda p, body: stub_digest
        digest.PAPERS_DIR = os.path.join(tmp, "Papers")
        radar.SEEN_PATH = os.path.join(tmp, "seen.json")
        radar.LOGS_DIR = tmp
        try:
            msg = digest.run_paper_digest(mode="sns", save=False)
            assert not is_failure_result(msg), msg
            assert radar.load_seen_ids() == [], "save=False で既出にしてはいけない"
            assert not os.path.exists(digest.PAPERS_DIR), "save=False でファイルを書いている"

            msg = digest.run_paper_digest(mode="sns", save=True)
            assert not is_failure_result(msg), msg
            assert radar.load_seen_ids() == ["2607.00080"], radar.load_seen_ids()
            written = sorted(os.listdir(digest.PAPERS_DIR))
            assert len(written) == 2, f"要約ノートと下書きの2本が出るはず: {written}"
            assert any(f.endswith("_draft.md") for f in written), written
        finally:
            (digest._resolve_paper, digest.capture_paper, digest.summarize_paper,
             digest.PAPERS_DIR, radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [13] run_paper_digest: 保存したときだけ既出にする")


def test_registry_entries_registered():
    from lib.tool_registry import TOOL_ENTRIES
    names = {e["name"] for e in TOOL_ENTRIES}
    assert {"tool_paper_radar", "tool_paper_digest"} <= names, sorted(names)
    print("✅ [12] registry: paper 系ツールが登録されている")


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
