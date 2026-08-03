# -*- coding: utf-8 -*-
"""
Paper Digest — SNS/note 下書き生成のユニットテスト

方針:
  - ネットワークに一切出ない。Gemini 呼び出しはすべてスタブに差し替える
    (CLAUDE.md: テストを一周回すだけで課金や実副作用が起きてはいけない)。
    特に build_sns_post / build_note_article は Gemini を呼ぶため、
    これらを直接 or 間接に呼ぶテストは必ずスタブを噛ませること。
  - 本番の可変資源 (logs/paper_seen_ids.json・Obsidian Vault) には触らない。

実行: py tests/test_paper_digest.py
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


class _FakeResp:
    def __init__(self, text):
        self.text = text


# ---------------------------------------------------------
# [1] trim_post / 機械組み立て (純関数)
# ---------------------------------------------------------

def test_trim_post_enforces_limit():
    import tools.tool_paper_digest as digest
    assert digest.trim_post("あ" * 200, 140) == "あ" * 139 + "…"
    assert digest.trim_post("短い", 140) == "短い"
    assert digest.trim_post("", 140) == ""
    print("✅ [1] trim_post: 文字数を機械で担保する")


def test_mechanical_hook_post_uses_digest_labels():
    """LLM 完全不通時のフォールバックは、digest の値をそのまま出すだけであること。"""
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要旨1文", "problem": "課題A", "method": "手法B",
         "results": "結果C", "implication": "示唆D"}
    paper = _paper("2607.00041", title="T")
    post = digest._mechanical_hook_post(d, paper)
    assert post.startswith("📄 要旨1文"), post
    assert "課題: 課題A" in post, post
    assert "手法: 手法B" in post, post
    print("✅ [1b] _mechanical_hook_post: digest の値をそのまま出す")


# ---------------------------------------------------------
# [2] build_sns_post (LLM + 機械処理、Xプレミアム長文1投稿)
# ---------------------------------------------------------

def test_build_sns_post_respects_limit_and_ends_with_link():
    """LLM の文字数指示を信用せず、本文部分は最終的に機械で切ってあること。

    末尾のタグ+リンクの予算配分は X の実効カウント (URL は t.co 換算23字固定) を
    前提にしているため、Python の実文字列長では合計が limit を超えうる
    (実URLは23字より長い) — これは意図した挙動であり、検証すべきは
    「本文部分が機械的に切られているか」であって「合計len()が limit 以下か」ではない。
    draft_sns_hook_post はスタブに差し替え、Gemini を呼ばせない。
    """
    import tools.tool_paper_digest as digest
    d = {"one_liner": "あ" * 300, "keywords": ["LLM", "強化学習", "third"]}
    paper = _paper("2607.00040", title="T")

    original = digest.draft_sns_hook_post
    digest.draft_sns_hook_post = lambda dd, pp: "い" * 2000
    try:
        post = digest.build_sns_post(d, paper, limit=200)
    finally:
        digest.draft_sns_hook_post = original

    body_part = post.split("\n\n")[0]
    assert len(body_part) < 2000, f"本文が機械で切られていない: {len(body_part)}字"
    assert body_part.endswith("…"), f"上限超過時は省略記号で終わるはず: {body_part[-10:]!r}"
    assert paper["abs_url"] in post, f"原論文リンクが無い: {post!r}"
    assert "#LLM" in post and "#強化学習" in post, post
    assert "📝 全文はこちら" in post, f"本編誘導プレースホルダーが無い: {post!r}"
    assert "note: " in post and "X Articles: " in post, post
    print("✅ [2a] build_sns_post: 本文が機械的に上限内へ切られ、末尾に本編誘導+リンクがある")


def test_build_sns_post_falls_back_to_mechanical_when_llm_returns_nothing():
    """draft_sns_hook_post が形式不正で "" を返したとき、機械組み立てへ落ちること。

    tail (本編誘導プレースホルダー+タグ+リンク) の実文字数は build_sns_post の
    予算計算式に依存するため、それをマジックナンバーとしてここへ複製しない。
    代わりに「機械組み立ての本文がそのまま (未トリムで) 先頭に出ること」と
    「本編誘導プレースホルダーが末尾に付くこと」を検証する。
    """
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要旨", "problem": "課題A"}
    paper = _paper("2607.00043", title="T")

    original = digest.draft_sns_hook_post
    digest.draft_sns_hook_post = lambda dd, pp: ""
    try:
        post = digest.build_sns_post(d, paper, limit=1500)
        expected_body = digest._mechanical_hook_post(d, paper)
    finally:
        digest.draft_sns_hook_post = original

    assert post.startswith(expected_body), (
        f"機械組み立ての本文がそのまま使われていない: {post!r}")
    assert "📝 全文はこちら" in post, f"本編誘導プレースホルダーが無い: {post!r}"
    assert "note: " in post and "X Articles: " in post, post
    print("✅ [2b] build_sns_post: LLMが空を返したら機械組み立てにフォールバック")


def test_build_sns_post_propagates_llm_exceptions():
    """Gemini 呼び出し自体の失敗 (ネットワーク断・予算超過) は握りつぶさず伝播させる。

    障害を「品質の低い下書き」に見せかけない (summarize_paper と同じ方針)。
    """
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要旨"}
    paper = _paper("2607.00044", title="T")

    def _boom(dd, pp):
        raise RuntimeError("gemini down")

    original = digest.draft_sns_hook_post
    digest.draft_sns_hook_post = _boom
    try:
        raised = False
        try:
            digest.build_sns_post(d, paper)
        except RuntimeError:
            raised = True
        assert raised, "LLM呼び出しの例外が握りつぶされている"
    finally:
        digest.draft_sns_hook_post = original
    print("✅ [2c] build_sns_post: LLM呼び出し失敗は例外のまま伝播する")


def test_draft_sns_hook_post_falls_back_to_default_label_when_missing():
    """LLM が label キーを省略/空文字列で返しても、既定ラベルへ機械的に落ちること。

    プロンプト指示 (「ラベルは省略せず必ず考えること」) だけでは実測上
    複数回に1回ラベルが省略される事象を確認したため、JSON出力を label/post に
    分離し機械側で結合する設計にした。この保証が壊れると、視覚的な目立たせ役
    (【衝撃】等) が再び消える。
    """
    import lib.lib_shared as lib_shared
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要旨"}
    paper = _paper("2607.00045", title="T")

    original_fn = lib_shared.client.models.generate_content
    try:
        lib_shared.client.models.generate_content = (
            lambda model, contents: _FakeResp('{"post": "本文のみ、labelキーなし"}'))
        result = digest.draft_sns_hook_post(d, paper)
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert result.startswith("【"), f"既定ラベルへフォールバックしていない: {result!r}"
    assert "本文のみ、labelキーなし" in result, result
    print("✅ [2d] draft_sns_hook_post: labelが無ければ既定ラベルへフォールバックする")


def test_build_sns_post_omits_broken_link_when_no_url_or_arxiv_id():
    """abs_url も arxiv_id も無い paper では、壊れた 'https://arxiv.org/abs/' を
    組み立てないこと (回帰テスト)。

    以前は `paper.get("abs_url") or f"https://arxiv.org/abs/{paper.get('arxiv_id','')}"`
    と書いており、両方とも無い paper (tool_document.py が渡す任意文書など、
    恒久的な公開URLを持たないケース) では末尾のリンクが壊れたURLになっていた。
    修正後はリンク行自体を省く。draft_sns_hook_post はスタブし Gemini を呼ばせない。
    """
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要旨"}
    paper = {"title": "T"}  # abs_url も arxiv_id も無い

    original = digest.draft_sns_hook_post
    digest.draft_sns_hook_post = lambda dd, pp: "スタブ投稿本文"
    try:
        post = digest.build_sns_post(d, paper, limit=500)
    finally:
        digest.draft_sns_hook_post = original

    assert "https://arxiv.org/abs/" not in post, f"壊れたURLが残っている: {post!r}"
    assert "arxiv.org" not in post, f"リンクが無いはずなのに arxiv.org が含まれる: {post!r}"
    print("✅ [2e] build_sns_post: abs_url/arxiv_id が無ければ壊れたリンクを組み立てない")


def test_build_sns_post_prompt_uses_custom_subject_label():
    """paper['subject_label'] を渡すと draft_sns_hook_post のプロンプト文中の
    「論文」が差し替わること (tool_document.py が "文書" を渡すケースの回帰)。
    """
    import lib.lib_shared as lib_shared
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要旨"}
    paper = {"title": "T", "subject_label": "文書"}

    captured = {}

    def _fake_generate(model, contents):
        captured["contents"] = contents
        return _FakeResp('{"post": "本文"}')

    original_fn = lib_shared.client.models.generate_content
    try:
        lib_shared.client.models.generate_content = _fake_generate
        digest.draft_sns_hook_post(d, paper)
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert "文書要約" in captured["contents"], captured["contents"][:500]
    assert "論文要約" not in captured["contents"], "既定の「論文」語彙が残っている"
    print("✅ [2f] draft_sns_hook_post: subject_label がプロンプトへ反映される")


# ---------------------------------------------------------
# [3] build_note_article (タイトル抽出)
# ---------------------------------------------------------

def test_build_note_article_extracts_title_line():
    import lib.lib_shared as lib_shared
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要約"}
    paper = _paper("2607.00090", title="Original Title")
    capture = {"backend": "docling"}

    original_fn = lib_shared.client.models.generate_content
    try:
        lib_shared.client.models.generate_content = (
            lambda model, contents: _FakeResp("# 新タイトル\n\n本文です。"))
        result = digest.build_note_article(d, paper, capture)
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert result["title"] == "新タイトル", result
    assert result["body"] == "本文です。", result
    print("✅ [3a] build_note_article: 1行目のタイトルを抽出する")


def test_build_note_article_falls_back_when_no_title_line():
    import lib.lib_shared as lib_shared
    import tools.tool_paper_digest as digest
    d = {"one_liner": "要約"}
    paper = _paper("2607.00091", title="Original Title")
    capture = {"backend": "docling"}

    original_fn = lib_shared.client.models.generate_content
    try:
        lib_shared.client.models.generate_content = (
            lambda model, contents: _FakeResp("本文だけ、タイトル行なし。"))
        result = digest.build_note_article(d, paper, capture)
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert result["title"] == "", result
    assert result["body"] == "本文だけ、タイトル行なし。", result
    print("✅ [3b] build_note_article: タイトル行が無ければ title=\"\" (呼び出し側でpaper titleへフォールバック)")


# ---------------------------------------------------------
# [4] キャプチャの劣化を「黙って」やらない
# ---------------------------------------------------------

def test_capture_paper_falls_back_to_abstract_with_explicit_backend():
    """本文が取れなかったとき backend='abstract' と警告を必ず立てること。

    ここで黙って要旨を返すと、以降の要約・記事・SNS 投稿が「全文を読んだ体」で
    出る (読者にも自分にも嘘をつく)。CLAUDE.md 所見2「無言の死」の同型。
    """
    import lib.docling_client as docling_client
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
    print("✅ [4a] capture_paper: 要旨フォールバックを backend で明示する")


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
    print("✅ [4b] render_digest_markdown: 取得経路が本文に必ず出る")


# ---------------------------------------------------------
# [5] run_paper_digest 統合 (LLM呼び出しは全てスタブ)
# ---------------------------------------------------------

def test_digest_marks_seen_only_when_saved():
    """--no-save の試し打ちで既出フラグを立てないこと。

    立ててしまうと「1文字も保存していない論文」がレーダーから永久に消える。
    Vault にも本番 logs にも触らないよう、保存先と seen パスを一時パスへ逃がす。
    build_sns_post は改修で Gemini を呼ぶようになったため、必ずスタブする。
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
                     digest.build_sns_post, digest.PAPERS_DIR, radar.SEEN_PATH, radar.LOGS_DIR)
        digest._resolve_paper = lambda **kwargs: paper
        digest.capture_paper = lambda p, use_docling=True: stub_capture
        digest.summarize_paper = lambda p, body: stub_digest
        digest.build_sns_post = lambda d, p: f"stub post\n{paper['abs_url']}"
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
             digest.build_sns_post, digest.PAPERS_DIR, radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [5a] run_paper_digest: 保存したときだけ既出にする")


def test_run_paper_digest_returns_error_when_sns_post_fails():
    """build_sns_post が例外を投げたら Error: プレフィックスで返すこと。

    LLM障害が「静かに機械組み立てへ劣化」ではなく「はっきり失敗」として
    呼び出し側 (Discord bot 等) に伝わることを保証する。
    """
    import tools.tool_paper_digest as digest

    paper = _paper("2607.00081", title="Stub Paper 2")
    stub_digest = {"one_liner": "要約", "keywords": ["k1"]}
    stub_capture = {"text": "本文", "backend": "docling", "chars": 4,
                    "truncated": False, "source": "", "warnings": []}

    def _boom(d, p):
        raise RuntimeError("gemini down")

    with tempfile.TemporaryDirectory() as tmp:
        originals = (digest._resolve_paper, digest.capture_paper, digest.summarize_paper,
                     digest.build_sns_post, digest.PAPERS_DIR, radar.SEEN_PATH, radar.LOGS_DIR)
        digest._resolve_paper = lambda **kwargs: paper
        digest.capture_paper = lambda p, use_docling=True: stub_capture
        digest.summarize_paper = lambda p, body: stub_digest
        digest.build_sns_post = _boom
        digest.PAPERS_DIR = os.path.join(tmp, "Papers")
        radar.SEEN_PATH = os.path.join(tmp, "seen.json")
        radar.LOGS_DIR = tmp
        try:
            msg = digest.run_paper_digest(mode="sns", save=True)
            assert is_failure_result(msg), f"X投稿生成失敗がErrorになっていない: {msg!r}"
            assert radar.load_seen_ids() == [], "失敗したのに既出にしてはいけない"
        finally:
            (digest._resolve_paper, digest.capture_paper, digest.summarize_paper,
             digest.build_sns_post, digest.PAPERS_DIR, radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [5b] run_paper_digest: X投稿生成失敗は Error: で返す")


def test_run_paper_digest_both_mode_includes_note_and_x_articles_sections():
    """mode='both' で note.com と X Articles の両セクションが draft に出ること。

    build_note_article は1回しか呼ばず、同一の記事を両プラットフォームへ
    転載する設計 (note.com/X Articles は構成が近く書き分ける実益が薄いため)。
    その前提が draft_lines 組み立てで壊れていないかを確認する。
    """
    import tools.tool_paper_digest as digest

    paper = _paper("2607.00082", title="Stub Paper 3")
    stub_digest = {"one_liner": "要約", "keywords": ["k1"]}
    stub_capture = {"text": "本文", "backend": "docling", "chars": 4,
                    "truncated": False, "source": "", "warnings": []}
    stub_article = {"title": "スタブ記事タイトル", "body": "スタブ記事本文です。"}

    with tempfile.TemporaryDirectory() as tmp:
        originals = (digest._resolve_paper, digest.capture_paper, digest.summarize_paper,
                     digest.build_sns_post, digest.build_note_article,
                     digest.PAPERS_DIR, radar.SEEN_PATH, radar.LOGS_DIR)
        digest._resolve_paper = lambda **kwargs: paper
        digest.capture_paper = lambda p, use_docling=True: stub_capture
        digest.summarize_paper = lambda p, body: stub_digest
        digest.build_sns_post = lambda d, p: f"stub teaser\n{paper['abs_url']}"
        digest.build_note_article = lambda d, p, c: stub_article
        digest.PAPERS_DIR = os.path.join(tmp, "Papers")
        radar.SEEN_PATH = os.path.join(tmp, "seen.json")
        radar.LOGS_DIR = tmp
        try:
            msg = digest.run_paper_digest(mode="both", save=True)
            assert not is_failure_result(msg), msg
            draft_name = next(f for f in os.listdir(digest.PAPERS_DIR) if f.endswith("_draft.md"))
            with open(os.path.join(digest.PAPERS_DIR, draft_name), encoding="utf-8") as f:
                draft_text = f.read()
            assert draft_text.count("スタブ記事本文です。") == 2, (
                f"note.com と X Articles の両方に同一本文が出るはず:\n{draft_text}")
            assert "## note.com 記事下書き" in draft_text, draft_text
            assert "## X Articles 下書き" in draft_text, draft_text
        finally:
            (digest._resolve_paper, digest.capture_paper, digest.summarize_paper,
             digest.build_sns_post, digest.build_note_article,
             digest.PAPERS_DIR, radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [5c] run_paper_digest: mode=both で note/X Articles 両セクションが出る")


def test_registry_entries_registered():
    from lib.tool_registry import TOOL_ENTRIES
    names = {e["name"] for e in TOOL_ENTRIES}
    assert {"tool_paper_radar", "tool_paper_digest"} <= names, sorted(names)
    print("✅ [6] registry: paper 系ツールが登録されている")


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
