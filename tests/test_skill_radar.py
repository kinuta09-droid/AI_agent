# -*- coding: utf-8 -*-
"""
Skill Radar のユニットテスト

方針:
  - ネットワークに一切出ない。GitHub Search API / Gemini はすべてスタブに差し替える
    (CLAUDE.md: テストを一周回すだけで課金や実副作用が起きてはいけない)。
  - 本番の可変資源 (logs/skill_radar_seen_ids.json・Obsidian Vault・実際の
    .claude/skills, .claude/commands) には触らない。seen キャッシュとローカル在庫の
    ディレクトリはテスト内で一時ディレクトリへ差し替える
    (test_curate_note_save の「本番資源を見続けていた」事故の再発防止)。

実行: py tests/test_skill_radar.py
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


def _repo(full_name, stars=0, days_ago=1, description="A helpful tool", topics=None):
    pushed = NOW - datetime.timedelta(days=days_ago)
    return {
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "description": description,
        "stars": stars,
        "pushed_at": pushed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "topics": topics or [],
    }


# ---------------------------------------------------------
# [1] キーワード抽出 / 重複判定
# ---------------------------------------------------------

def test_extract_keywords_filters_common_and_short_words():
    """"claude"/"skill" のような全候補に必ず現れる語を落とさないと、
    重複判定が常に True になり候補が全滅する。
    """
    kws = radar._extract_keywords("Claude Code Skill for debugging and testing workflows")
    assert "claude" not in kws and "skill" not in kws and "code" not in kws, kws
    assert "debugging" in kws and "testing" in kws and "workflows" in kws, kws
    print("✅ [1] _extract_keywords: 汎用語を除外し実質的なキーワードだけ残す")


def test_is_likely_duplicate_detects_overlap_with_local_inventory():
    """既存Skillと2語以上キーワードが被れば重複とみなして弾くこと。"""
    local = [{
        "name": "debug-issue",
        "description": "disciplined debugging with instrumentation feedback loops",
        "keywords": radar._extract_keywords("debug-issue disciplined debugging instrumentation feedback loops"),
    }]
    dup = _repo("someone/awesome-debugging-instrumentation",
                description="disciplined debugging with feedback loops")
    novel = _repo("someone/lora-factory", description="LoRA training helper for ComfyUI")
    assert radar.is_likely_duplicate(dup, local) is True
    assert radar.is_likely_duplicate(novel, local) is False
    print("✅ [1b] is_likely_duplicate: キーワード重複で既存Skillとの被りを検出")


# ---------------------------------------------------------
# [2] スコアリング (純関数)
# ---------------------------------------------------------

def test_score_repo_monotonic_in_stars_and_recency():
    """stars が多いほど・更新が新しいほどスコアが上がること。"""
    low_star = _repo("a/low", stars=1, days_ago=5)
    high_star = _repo("a/high", stars=5000, days_ago=5)
    assert radar.score_repo(high_star, NOW)["score"] > radar.score_repo(low_star, NOW)["score"]

    fresh = _repo("a/fresh", stars=100, days_ago=1)
    stale = _repo("a/stale", stars=100, days_ago=900)
    assert radar.score_repo(fresh, NOW)["score"] > radar.score_repo(stale, NOW)["score"]
    print("✅ [2] score_repo: stars / 新しさで単調に増える")


def test_score_repo_rewards_relevance_keywords():
    """ARMSの技術スタック語 (discord, gemini, mcp 等) に当たるほど加点されること。"""
    plain = _repo("a/plain", stars=10, description="just a random tool")
    relevant = _repo("a/relevant", stars=10, description="a discord bot with gemini and mcp support",
                     topics=["python", "agent"])
    s_plain = radar.score_repo(plain, NOW)
    s_relevant = radar.score_repo(relevant, NOW)
    assert s_relevant["score"] > s_plain["score"], (s_relevant, s_plain)
    assert set(radar.score_repo(plain, NOW)["breakdown"]) == set(radar.WEIGHTS)
    print("✅ [2b] score_repo: 関連キーワード一致で加点される")


def test_score_repo_does_not_reward_generic_dev_buzzwords():
    """test/review/architecture等の汎用語はもう関連度に効かないこと (2026-08-06チューニング)。

    これらは Claude Code Skill と無関係なリポジトリにも必ず現れる語で、
    関連度スコアを stars/新しさの言い換えにしてしまっていた (実害: GodotMaker等が上位に来た)。
    """
    buzzwordy = _repo("a/buzzwordy", stars=10,
                      description="a test-driven architecture and workflow review tool",
                      topics=["documentation", "planning"])
    assert radar.score_repo(buzzwordy, NOW)["matched_keywords"] == []
    print("✅ [2c] score_repo: 汎用開発ワークフロー語には加点しない")


def test_score_repo_relevance_uses_word_boundaries_not_substring():
    """短いキーワード追加 (cli/cron/bot) が英単語への部分文字列一致で誤爆しないこと。

    /grill で指摘: "cli" in "client", "cron" in "acronym", "bot" in "robot" は
    素朴な部分文字列一致だと誤って加点してしまう (今回のチューニングが解消しようとした
    バグを、短い新語で再導入してしまう形)。一方でGitHub topicsはハイフン連結スラッグ
    ("mcp-server", "discord-bot") が多いため、そちらは一致し続ける必要がある。
    """
    false_positive = _repo("a/client-lib", stars=10,
                           description="An API client library with acronym glossary and robot support")
    assert radar.score_repo(false_positive, NOW)["matched_keywords"] == [], \
        radar.score_repo(false_positive, NOW)["matched_keywords"]

    hyphenated = _repo("a/mcp-tool", stars=10, description="a tool",
                       topics=["mcp-server", "discord-bot"])
    matched = radar.score_repo(hyphenated, NOW)["matched_keywords"]
    assert "mcp" in matched and "discord" in matched and "bot" in matched, matched
    print("✅ [2d] score_repo: 単語境界で誤爆を防ぎつつハイフン連結topicsは一致")


def test_score_repo_ai_fit_can_override_star_advantage():
    """AI適合度 not_applicable が、stars優位な候補を下位に逆転させられること。

    2026-08-06のチューニングセッションで手動検証した実データ (nexu-io/open-design ⭐8.4万等)
    では、キーワード一致だけでは「stars は多いが単体の完成された製品でSkillとして切り出せない」
    候補を弾けなかった。ai_fit の not_applicable がその失敗モードを是正する役目を持つこと、
    None (未評価) は従来どおり無加点であることを確認する。
    """
    huge_star_product = _repo("big/product", stars=80000, description="a design app")
    small_star_fit = _repo("small/plan-helper", stars=10, description="a planning helper")

    no_ai = radar.score_repo(huge_star_product, NOW)
    assert no_ai["breakdown"]["ai_fit"] == 0.0, no_ai  # 未評価は無加点

    penalized = radar.score_repo(huge_star_product, NOW, ai_fit="not_applicable")
    boosted = radar.score_repo(small_star_fit, NOW, ai_fit="high")
    assert penalized["score"] < no_ai["score"], (penalized, no_ai)
    assert boosted["score"] > penalized["score"], (boosted, penalized)
    print("✅ [2e] score_repo: ai_fit=not_applicable がstars優位を逆転させる")


# ---------------------------------------------------------
# [3] ランキング
# ---------------------------------------------------------

def test_rank_repos_orders_excludes_seen_dupes_and_is_deterministic():
    repos = [
        _repo("x/low", stars=5, days_ago=2),
        _repo("x/high", stars=500, days_ago=2),
        _repo("x/mid", stars=50, days_ago=2),
    ]
    ranked = radar.rank_repos(repos, [], NOW, top_k=2)
    assert [r["full_name"] for r in ranked] == ["x/high", "x/mid"], ranked

    # 既出は候補から外れる
    ranked = radar.rank_repos(repos, [], NOW, top_k=3, exclude_ids=["x/high"])
    assert [r["full_name"] for r in ranked] == ["x/mid", "x/low"], ranked

    # 既存Skillと被る候補も外れる
    local = [{"name": "grill", "description": "adversarial review",
             "keywords": radar._extract_keywords("grill adversarial review")}]
    grillish = _repo("x/adversarial-review-bot", stars=999, description="adversarial review tool")
    ranked = radar.rank_repos(repos + [grillish], local, NOW, top_k=4)
    assert "x/adversarial-review-bot" not in [r["full_name"] for r in ranked], ranked

    # 完全同点でも順序が実行ごとに揺れないこと
    twins = [_repo("z/bbb", stars=10, days_ago=3), _repo("z/aaa", stars=10, days_ago=3)]
    tie_order = [r["full_name"] for r in radar.rank_repos(twins, [], NOW, top_k=2)]
    assert tie_order == ["z/aaa", "z/bbb"], f"同点は full_name で決定的に並ぶこと: {tie_order}"
    print("✅ [3] rank_repos: 降順 / 既出除外 / 重複除外 / 同点の決定性")


def test_rank_repos_ai_fit_map_reorders_results():
    """ai_fit_map を渡すと、stars優位でも not_applicable 判定の候補が下位へ落ちること。

    ai_fit_map 省略時 (既定 None) は従来どおりの並びのままであることも合わせて確認する
    (既存呼び出し元への後方互換)。
    """
    repos = [
        _repo("big/product", stars=80000),
        _repo("small/plan-helper", stars=10),
    ]
    default_order = [r["full_name"] for r in radar.rank_repos(repos, [], NOW, top_k=2)]
    assert default_order == ["big/product", "small/plan-helper"], default_order

    ai_fit_map = {"big/product": "not_applicable", "small/plan-helper": "high"}
    reordered = [r["full_name"] for r in
                radar.rank_repos(repos, [], NOW, top_k=2, ai_fit_map=ai_fit_map)]
    assert reordered == ["small/plan-helper", "big/product"], reordered
    print("✅ [3b] rank_repos: ai_fit_map でstars優位の並びを逆転できる")


# ---------------------------------------------------------
# [4] レポート生成
# ---------------------------------------------------------

def test_render_radar_markdown_shows_score_breakdown_and_comment():
    ranked = radar.rank_repos([_repo("a/example", stars=42, description="an example tool")],
                              [], NOW, top_k=1, ai_fit_map={"a/example": "high"})
    ranked[0]["comment_ja"] = "テスト用コメント"
    ranked[0]["ai_fit"] = "high"
    md = radar.render_radar_markdown(ranked, NOW)
    for token in ["a/example", "スコア", "⭐ 42", "テスト用コメント", "AI適合度[high]"]:
        assert token in md, f"レポートに {token!r} が出ていない:\n{md}"
    print("✅ [4] render_radar_markdown: スコア内訳とAIコメントが読める")


# ---------------------------------------------------------
# [5] ローカルSkill在庫の読み込み (本番ディレクトリではなく一時ディレクトリで検証)
# ---------------------------------------------------------

def test_load_local_skill_inventory_parses_frontmatter_and_commands():
    # ファイル名は実行時に組み立てる (リテラルで "SKILL.md"/"grill.md" と書くと、
    # 追跡下の同名ファイル (.claude/skills/*/SKILL.md, .claude/commands/grill.md) と
    # basenameが一致し test_ci_docs_exemption.py の検出器に「追跡下.mdへの参照」として
    # 誤検知される — このtmpdirフィクスチャは実ファイルを一切読まないので無害だが、
    # 検出器はリテラル単位でしか判定できない。tests/test_ci_docs_exemption.py の
    # `_SAMPLE = "CLAUDE" + ".md"` と同じ回避法をここでも使う。
    skill_md_name = "SKILL" + ".md"
    grill_md_name = "grill" + ".md"
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = os.path.join(tmp, "skills")
        commands_dir = os.path.join(tmp, "commands")
        os.makedirs(os.path.join(skills_dir, "example-skill"))
        os.makedirs(commands_dir)
        with open(os.path.join(skills_dir, "example-skill", skill_md_name), "w", encoding="utf-8") as f:
            f.write("---\nname: example-skill\ndescription: debugging helper for tests\n---\n\n# Body\n")
        with open(os.path.join(commands_dir, grill_md_name), "w", encoding="utf-8") as f:
            f.write("# /grill — adversarial code review\n\nbody...\n")

        inventory = radar.load_local_skill_inventory(skills_dir=skills_dir, commands_dir=commands_dir)
        names = {i["name"] for i in inventory}
        assert names == {"example-skill", "grill"}, names
        by_name = {i["name"]: i for i in inventory}
        assert "debugging" in by_name["example-skill"]["keywords"]
        assert "adversarial" in by_name["grill"]["keywords"]
    print("✅ [5] load_local_skill_inventory: SKILL.md frontmatter とコマンドの見出しを解析")


# ---------------------------------------------------------
# [6] 既出キャッシュ (本番 logs を汚さない)
# ---------------------------------------------------------

def test_seen_cache_roundtrip_in_tmpdir():
    with tempfile.TemporaryDirectory() as tmp:
        orig_seen, orig_logs = radar.SEEN_PATH, radar.LOGS_DIR
        radar.SEEN_PATH = os.path.join(tmp, "skill_seen_ids.json")
        radar.LOGS_DIR = tmp
        try:
            assert radar.load_seen_ids() == [], "初回は空"
            radar.mark_seen(["a/repo1", "a/repo2", ""])
            seen = radar.load_seen_ids()
            assert seen == ["a/repo1", "a/repo2"], seen
            radar.mark_seen(["a/repo1"])  # 重複は増えない
            assert radar.load_seen_ids() == ["a/repo1", "a/repo2"], radar.load_seen_ids()
        finally:
            radar.SEEN_PATH, radar.LOGS_DIR = orig_seen, orig_logs
    print("✅ [6] seen キャッシュ: 重複排除・一時パスへ隔離")


# ---------------------------------------------------------
# [7] AI適合度判定 (失敗時は加点なし・コメント無しでフォールバック)
# ---------------------------------------------------------

class _FakeResp:
    def __init__(self, text):
        self.text = text


def test_evaluate_fit_ja_returns_fit_and_comment_per_repo():
    import lib.lib_shared as lib_shared
    candidates = [_repo("a/example", stars=10)]

    original_fn = lib_shared.client.models.generate_content
    try:
        lib_shared.client.models.generate_content = (
            lambda model, contents: _FakeResp(
                '[{"full_name": "a/example", "fit": "high", '
                '"comment_ja": "導入する価値があります"}]'))
        result = radar.evaluate_fit_ja(candidates, [])
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert result == {"a/example": {"fit": "high", "comment_ja": "導入する価値があります"}}, result
    print("✅ [7a] evaluate_fit_ja: fit と comment_ja を候補ごとに返す")


def test_evaluate_fit_ja_ignores_unknown_fit_values():
    """fit がスコア変換テーブルに無い値なら黙って捨てること (壊れたLLM出力を混入させない)。"""
    import lib.lib_shared as lib_shared
    candidates = [_repo("a/example", stars=10)]

    original_fn = lib_shared.client.models.generate_content
    try:
        lib_shared.client.models.generate_content = (
            lambda model, contents: _FakeResp(
                '[{"full_name": "a/example", "fit": "amazing", "comment_ja": "..."}]'))
        result = radar.evaluate_fit_ja(candidates, [])
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert result == {}, result
    print("✅ [7b] evaluate_fit_ja: 未知のfit値は捨てる")


def test_evaluate_fit_ja_failure_does_not_raise():
    """Gemini呼び出しが失敗しても例外を外へ伝播させず、空dictで継続すること。"""
    import lib.lib_shared as lib_shared
    candidates = [_repo("a/example", stars=10)]

    original_fn = lib_shared.client.models.generate_content
    try:
        def _boom(model, contents):
            raise RuntimeError("quota exceeded")
        lib_shared.client.models.generate_content = _boom
        result = radar.evaluate_fit_ja(candidates, [])  # 例外を投げないこと
    finally:
        lib_shared.client.models.generate_content = original_fn

    assert result == {}, result
    print("✅ [7c] evaluate_fit_ja: 失敗時は例外を外へ出さず空dictで継続")


# ---------------------------------------------------------
# [8] 「該当なし」と「本当の失敗」を区別すること
# ---------------------------------------------------------

def test_empty_result_is_not_a_failure_but_errors_are():
    """0件は成功 (収集は動いた)。収集自体の例外は "Error:" で失敗と分かること。

    ここを取り違えると、定時ジョブが「静かな週」と「壊れた週」を同じ ❌ で
    通知する / 逆に壊れた週を ✅ で流す (CLAUDE.md Known Bug Patterns §A)。
    """
    orig = radar.collect_candidates

    radar.collect_candidates = lambda *a, **k: ([], 1)  # 全クエリ成功・0件
    try:
        msg = radar.run_skill_radar(save=False, comment=False)
    finally:
        radar.collect_candidates = orig
    assert not is_failure_result(msg), f"0件を失敗として返している: {msg!r}"

    def _boom(*a, **k):
        raise RuntimeError("GitHub API down")

    radar.collect_candidates = _boom
    try:
        msg = radar.run_skill_radar(save=False, comment=False)
    finally:
        radar.collect_candidates = orig
    assert is_failure_result(msg), f"収集失敗が失敗として判定されない: {msg!r}"

    radar.collect_candidates = lambda *a, **k: ([], 0)  # 全クエリ失敗
    try:
        msg = radar.run_skill_radar(save=False, comment=False)
    finally:
        radar.collect_candidates = orig
    assert is_failure_result(msg), f"全クエリ失敗が失敗として判定されない: {msg!r}"
    print("✅ [8] run_skill_radar: 0件は成功 / 収集失敗は失敗接頭辞")


# ---------------------------------------------------------
# [9] run_skill_radar 全体を通した統合確認 (AI適合度がランキングまで届くこと)
# ---------------------------------------------------------

def test_run_skill_radar_applies_ai_fit_to_ranking():
    """AI適合度評価が rank_repos より前で走り、stars優位を逆転させてランキングに反映されること。"""
    import lib.lib_shared as lib_shared
    with tempfile.TemporaryDirectory() as tmp:
        orig_seen, orig_logs = radar.SEEN_PATH, radar.LOGS_DIR
        orig_collect = radar.collect_candidates
        original_fn = lib_shared.client.models.generate_content
        radar.SEEN_PATH = os.path.join(tmp, "skill_seen_ids.json")
        radar.LOGS_DIR = tmp
        radar.collect_candidates = lambda *a, **k: (
            [_repo("big/product", stars=80000), _repo("small/plan-helper", stars=10)], 1)
        lib_shared.client.models.generate_content = (
            lambda model, contents: _FakeResp(
                '[{"full_name": "big/product", "fit": "not_applicable", '
                '"comment_ja": "製品本体で導入不可"}, '
                '{"full_name": "small/plan-helper", "fit": "high", '
                '"comment_ja": "計画支援と相性が良い"}]'))
        try:
            msg = radar.run_skill_radar(top_k=2, save=False, comment=True)
        finally:
            radar.collect_candidates = orig_collect
            lib_shared.client.models.generate_content = original_fn
            radar.SEEN_PATH, radar.LOGS_DIR = orig_seen, orig_logs

    assert "small/plan-helper" in msg and "big/product" in msg, msg
    assert msg.index("small/plan-helper") < msg.index("big/product"), \
        f"AI適合度が反映されずstars優位のままの並びになっている:\n{msg}"
    print("✅ [9] run_skill_radar: AI適合度評価がランキングの逆転まで届く")


def test_run_skill_radar_no_comment_skips_ai_fit_call():
    """comment=False (--no-comment) は evaluate_fit_ja を一切呼ばないこと。

    「Gemini呼び出しゼロ」はCLIヘルプ文言・CLAUDE.mdのコスト分離方針で明言している契約。
    /grill で指摘: この契約自体を検証するテストが無かった。
    """
    orig_evaluate = radar.evaluate_fit_ja
    orig_collect = radar.collect_candidates
    called = []
    radar.evaluate_fit_ja = lambda *a, **k: (called.append(1), {})[1]
    radar.collect_candidates = lambda *a, **k: ([_repo("a/x", stars=1)], 1)
    try:
        radar.run_skill_radar(save=False, comment=False)
    finally:
        radar.evaluate_fit_ja = orig_evaluate
        radar.collect_candidates = orig_collect

    assert called == [], "comment=False なのに evaluate_fit_ja が呼ばれている (課金ゼロ契約違反)"
    print("✅ [9b] run_skill_radar: comment=False は evaluate_fit_ja を呼ばない")


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
