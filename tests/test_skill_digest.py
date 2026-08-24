# -*- coding: utf-8 -*-
"""
Skill Digest — Skill Radar候補のSNS/note下書き生成のユニットテスト

方針:
  - ネットワークに一切出ない。build_sns_post/build_note_article (Gemini呼び出しを
    内包する) はすべてスタブに差し替える (CLAUDE.md: テストを一周回すだけで
    課金や実副作用が起きてはいけない)。
  - 本番の可変資源 (logs/skill_radar_seen_ids.json・logs/skill_radar_latest.json・
    Obsidian Vault) には触らない。一時ディレクトリへ差し替える。

実行: py tests/test_skill_digest.py
"""
import datetime
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_skill_radar as radar
from lib.bot_errors import is_failure_result

NOW = datetime.datetime(2026, 8, 9, 10, 0, 0)


def _repo(full_name, description="A helpful tool", topics=None, comment_ja="", ai_fit=None, stars=10):
    return {
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "description": description,
        "stars": stars,
        "topics": topics or [],
        "comment_ja": comment_ja,
        "ai_fit": ai_fit,
        "score": 1.0,
    }


# ---------------------------------------------------------
# [1] _skill_adapter / _build_skill_digest (純関数)
# ---------------------------------------------------------

def test_skill_adapter_maps_repo_to_paper_shape():
    import tools.tool_skill_digest as sdigest

    repo = _repo("owner/name")
    adapter = sdigest._skill_adapter(repo)
    assert adapter["title"] == "owner/name"
    assert adapter["abs_url"] == "https://github.com/owner/name"
    assert adapter["subject_label"] == "Skill"
    assert adapter["source_label"] == "リポジトリ"
    assert adapter["fallback_fields"] == [("説明", "description")]
    print("✅ [1a] _skill_adapter: paper形のキーへ写像")


def test_build_skill_digest_ignores_comment_ja_uses_description_then_name():
    """comment_ja (ARMS適合度判定の内部コメント) は外部公開下書きに混入させない
    ため one_liner には絶対に使わない — ARMSコマンドスキルにおける情報汚染
    (2026-08-23) の回帰テスト。"""
    import tools.tool_skill_digest as sdigest

    with_comment = _repo("a/x", description="desc", comment_ja="ARMSのプラン実行に合う")
    digest = sdigest._build_skill_digest(with_comment)
    assert digest["one_liner"] == "desc"
    assert "comment_ja" not in digest
    assert "why_fit" not in digest
    assert "ARMS" not in str(digest)

    no_comment = _repo("a/y", description="desc", comment_ja="")
    assert sdigest._build_skill_digest(no_comment)["one_liner"] == "desc"

    bare = _repo("a/z", description="", comment_ja="")
    assert sdigest._build_skill_digest(bare)["one_liner"] == "a/z"
    print("✅ [1b] _build_skill_digest: one_liner は description→full_name の順、comment_jaは無視")


def test_build_skill_digest_uses_topics_as_keywords():
    import tools.tool_skill_digest as sdigest

    repo = _repo("a/x", topics=["mcp", "agent", "cli", "python", "discord", "extra"])
    digest = sdigest._build_skill_digest(repo)
    assert digest["keywords"] == ["mcp", "agent", "cli", "python", "discord"], digest["keywords"]
    print("✅ [1c] _build_skill_digest: topicsを5件までkeywordsに使う")


def test_build_skill_digest_falls_back_when_ai_fit_missing():
    import tools.tool_skill_digest as sdigest

    repo = {"full_name": "a/x", "html_url": "https://github.com/a/x", "description": "desc",
            "stars": 1, "topics": []}  # --no-comment 週を模した、ai_fit/comment_ja キー自体が無いdict
    digest = sdigest._build_skill_digest(repo)
    assert digest["one_liner"] == "desc"
    print("✅ [1d] _build_skill_digest: ai_fit/comment_ja欠如でも例外にならない")


# ---------------------------------------------------------
# [2] _resolve_skill
# ---------------------------------------------------------

def test_resolve_skill_by_index_and_range_check():
    import tools.tool_skill_digest as sdigest

    repos = [_repo("a/1"), _repo("a/2"), _repo("a/3")]
    with tempfile.TemporaryDirectory() as tmp:
        orig_logs = radar.LOGS_DIR
        orig_latest = radar.LATEST_JSON
        radar.LOGS_DIR = tmp
        radar.LATEST_JSON = os.path.join(tmp, "skill_radar_latest.json")
        try:
            radar.save_latest_candidates(repos)
            assert sdigest._resolve_skill(index=2)["full_name"] == "a/2"
            try:
                sdigest._resolve_skill(index=0)
                assert False, "index=0 は ValueError になるはず"
            except ValueError:
                pass
            try:
                sdigest._resolve_skill(index=99)
                assert False, "範囲外index は ValueError になるはず"
            except ValueError:
                pass
        finally:
            radar.LOGS_DIR, radar.LATEST_JSON = orig_logs, orig_latest
    print("✅ [2a] _resolve_skill: index解決と範囲チェック")


def test_resolve_skill_by_repo_name_case_insensitive():
    import tools.tool_skill_digest as sdigest

    repos = [_repo("Owner/Name")]
    with tempfile.TemporaryDirectory() as tmp:
        orig_logs = radar.LOGS_DIR
        orig_latest = radar.LATEST_JSON
        radar.LOGS_DIR = tmp
        radar.LATEST_JSON = os.path.join(tmp, "skill_radar_latest.json")
        try:
            radar.save_latest_candidates(repos)
            assert sdigest._resolve_skill(repo="owner/name")["full_name"] == "Owner/Name"
            try:
                sdigest._resolve_skill(repo="nope/nope")
                assert False, "候補に無い名前は ValueError になるはず"
            except ValueError:
                pass
        finally:
            radar.LOGS_DIR, radar.LATEST_JSON = orig_logs, orig_latest
    print("✅ [2b] _resolve_skill: --repo は大文字小文字無視でマッチ")


def test_resolve_skill_backfills_via_run_skill_radar_when_empty():
    import tools.tool_skill_digest as sdigest

    called = []

    def _fake_run_skill_radar(**kwargs):
        called.append(kwargs)
        return "Skill Radar: 該当するリポジトリが見つかりませんでした"

    with tempfile.TemporaryDirectory() as tmp:
        orig_logs = radar.LOGS_DIR
        orig_latest = radar.LATEST_JSON
        orig_run = sdigest.run_skill_radar
        radar.LOGS_DIR = tmp
        radar.LATEST_JSON = os.path.join(tmp, "skill_radar_latest.json")
        sdigest.run_skill_radar = _fake_run_skill_radar
        try:
            try:
                sdigest._resolve_skill(index=1)
                assert False, "候補ゼロ件のままなら ValueError になるはず"
            except ValueError:
                pass
            assert len(called) == 1, "候補が空ならバックフィルで run_skill_radar を呼ぶはず"
        finally:
            radar.LOGS_DIR, radar.LATEST_JSON = orig_logs, orig_latest
            sdigest.run_skill_radar = orig_run
    print("✅ [2c] _resolve_skill: 候補ゼロ件は run_skill_radar でバックフィル")


# ---------------------------------------------------------
# [3] run_skill_digest 統合 (Gemini呼び出しは全てスタブ)
# ---------------------------------------------------------

def test_run_skill_digest_marks_seen_only_when_saved():
    import tools.tool_skill_digest as sdigest

    repo = _repo("owner/stub-skill", comment_ja="面白いツール")
    with tempfile.TemporaryDirectory() as tmp:
        originals = (sdigest._resolve_skill, sdigest.build_sns_post, sdigest.SKILLS_REPORT_DIR,
                     radar.SEEN_PATH, radar.LOGS_DIR)
        sdigest._resolve_skill = lambda **kwargs: repo
        sdigest.build_sns_post = lambda d, p: f"stub post\n{p['abs_url']}"
        sdigest.SKILLS_REPORT_DIR = os.path.join(tmp, "Skills")
        radar.SEEN_PATH = os.path.join(tmp, "seen.json")
        radar.LOGS_DIR = tmp
        try:
            msg = sdigest.run_skill_digest(mode="sns", save=False)
            assert not is_failure_result(msg), msg
            assert radar.load_seen_ids() == [], "save=False で既出にしてはいけない"
            assert not os.path.exists(sdigest.SKILLS_REPORT_DIR), "save=False でファイルを書いている"

            msg = sdigest.run_skill_digest(mode="sns", save=True)
            assert not is_failure_result(msg), msg
            assert radar.load_seen_ids() == ["owner/stub-skill"], radar.load_seen_ids()
            written = sorted(os.listdir(sdigest.SKILLS_REPORT_DIR))
            assert len(written) == 1 and written[0].endswith("_draft.md"), written
        finally:
            (sdigest._resolve_skill, sdigest.build_sns_post, sdigest.SKILLS_REPORT_DIR,
             radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [3a] run_skill_digest: 保存したときだけ既出にする")


def test_run_skill_digest_both_mode_includes_note_and_x_articles_sections():
    import tools.tool_skill_digest as sdigest

    repo = _repo("owner/both-skill", comment_ja="両方生成する候補")
    with tempfile.TemporaryDirectory() as tmp:
        originals = (sdigest._resolve_skill, sdigest.build_sns_post, sdigest.build_note_article,
                     sdigest.SKILLS_REPORT_DIR, radar.SEEN_PATH, radar.LOGS_DIR)
        sdigest._resolve_skill = lambda **kwargs: repo
        sdigest.build_sns_post = lambda d, p: "stub sns post"
        sdigest.build_note_article = lambda d, p, c: {"title": "記事タイトル", "body": "記事本文"}
        sdigest.SKILLS_REPORT_DIR = os.path.join(tmp, "Skills")
        radar.SEEN_PATH = os.path.join(tmp, "seen.json")
        radar.LOGS_DIR = tmp
        try:
            msg = sdigest.run_skill_digest(mode="both", save=True)
            assert not is_failure_result(msg), msg
            written = os.listdir(sdigest.SKILLS_REPORT_DIR)
            assert len(written) == 1
            with open(os.path.join(sdigest.SKILLS_REPORT_DIR, written[0]), encoding="utf-8") as f:
                body = f.read()
            assert "## X (Twitter) 投稿" in body
            assert "## note.com 記事下書き" in body
            assert "## X Articles 下書き" in body
        finally:
            (sdigest._resolve_skill, sdigest.build_sns_post, sdigest.build_note_article,
             sdigest.SKILLS_REPORT_DIR, radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [3b] run_skill_digest: mode=both はSNS/note/X Articlesの3セクション")


def test_run_skill_digest_propagates_sns_generation_failure():
    import tools.tool_skill_digest as sdigest

    repo = _repo("owner/boom-skill")

    def _boom(d, p):
        raise RuntimeError("gemini down")

    with tempfile.TemporaryDirectory() as tmp:
        originals = (sdigest._resolve_skill, sdigest.build_sns_post, sdigest.SKILLS_REPORT_DIR,
                     radar.SEEN_PATH, radar.LOGS_DIR)
        sdigest._resolve_skill = lambda **kwargs: repo
        sdigest.build_sns_post = _boom
        sdigest.SKILLS_REPORT_DIR = os.path.join(tmp, "Skills")
        radar.SEEN_PATH = os.path.join(tmp, "seen.json")
        radar.LOGS_DIR = tmp
        try:
            msg = sdigest.run_skill_digest(mode="sns", save=True)
            assert is_failure_result(msg), msg
            assert radar.load_seen_ids() == [], "生成失敗時に既出にしてはいけない"
        finally:
            (sdigest._resolve_skill, sdigest.build_sns_post, sdigest.SKILLS_REPORT_DIR,
             radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [3c] run_skill_digest: SNS生成失敗はError:接頭辞で伝播")


def test_run_skill_digest_slug_handles_slash_in_full_name():
    import tools.tool_skill_digest as sdigest

    repo = _repo("owner/repo-name", comment_ja="スラッシュテスト")
    with tempfile.TemporaryDirectory() as tmp:
        originals = (sdigest._resolve_skill, sdigest.build_sns_post, sdigest.SKILLS_REPORT_DIR,
                     radar.SEEN_PATH, radar.LOGS_DIR)
        sdigest._resolve_skill = lambda **kwargs: repo
        sdigest.build_sns_post = lambda d, p: "stub post"
        sdigest.SKILLS_REPORT_DIR = os.path.join(tmp, "Skills")
        radar.SEEN_PATH = os.path.join(tmp, "seen.json")
        radar.LOGS_DIR = tmp
        try:
            sdigest.run_skill_digest(mode="sns", save=True)
            written = os.listdir(sdigest.SKILLS_REPORT_DIR)
            assert len(written) == 1
            assert "/" not in written[0] and "owner_repo-name" in written[0], written
        finally:
            (sdigest._resolve_skill, sdigest.build_sns_post, sdigest.SKILLS_REPORT_DIR,
             radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [3d] run_skill_digest: full_nameの'/'はファイル名で安全に処理される")


def test_run_skill_digest_mode_none_saves_nothing():
    """mode=noneはsave=Trueでも成果物ゼロ — /grillで指摘された既知の不整合の回帰テスト:

    Skill Digest には paper_digest の「要約ノートは save=True なら常に書く」に相当する
    成果物が無いため、mark_seenをsave引数だけで判定すると、1文字も保存していない
    候補が既出になり次回Radarから消えてしまう。saved_path(実際に書いたかどうか)で
    判定することを確認する。
    """
    import tools.tool_skill_digest as sdigest

    repo = _repo("owner/none-skill")
    with tempfile.TemporaryDirectory() as tmp:
        originals = (sdigest._resolve_skill, sdigest.SKILLS_REPORT_DIR, radar.SEEN_PATH, radar.LOGS_DIR)
        sdigest._resolve_skill = lambda **kwargs: repo
        sdigest.SKILLS_REPORT_DIR = os.path.join(tmp, "Skills")
        radar.SEEN_PATH = os.path.join(tmp, "seen.json")
        radar.LOGS_DIR = tmp
        try:
            msg = sdigest.run_skill_digest(mode="none", save=True)
            assert not is_failure_result(msg), msg
            assert "保存先" not in msg
            assert not os.path.exists(sdigest.SKILLS_REPORT_DIR)
            assert radar.load_seen_ids() == [], "何も保存していないのに既出にしてはいけない"
        finally:
            (sdigest._resolve_skill, sdigest.SKILLS_REPORT_DIR, radar.SEEN_PATH, radar.LOGS_DIR) = originals
    print("✅ [3e] run_skill_digest: mode=noneは何も保存せず既出にもしない")


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
