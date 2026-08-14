# -*- coding: utf-8 -*-
"""tests/test_vault_health.py — Vault Health (plans/vault-health-and-contradictions.md Feature 1,
plans/obsidian-agent-native-notes.md status欠落率メトリクス)

検証する不変条件:
  [1] 基本分類: 新しい/古いノート × 被リンク有無の組み合わせで unused が正しく数えられる
  [2] NULL dst_path トラップの回帰: 未解決リンクが混ざっていても unused が過小評価(常に0)にならない
  [3] source != 'vault' の行は集計から除外される
  [4] 空DBは total==0 でノートを作成しない
  [5] 追記のみ・ヘッダー重複なし (2回連続実行)
  [6] 境界値: mtime == cutoff ちょうどはカウントしない
  [7] status欠落率: frontmatterにstatus:が無いノートを正しく数える
  [8] status欠落率: 対象ディレクトリ不在は total==0 (空Vault相当、例外を出さない)
  [9] check_vault_health の追記行にstatus欠落率の列が含まれる

本番Vault・logs/vault_index.db には一切触れない (tempfile の DB/note path のみ使う)。
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tool_vault_index import _connect, SOURCE_VAULT  # noqa: E402
from tools import tool_vault_health as health  # noqa: E402

NOW = 1_000_000_000.0
STALE_CUTOFF = NOW - health.STALE_DAYS * 86400


def _make_db(db_path, files, links=()):
    """files: [(filepath, mtime, source), ...] / links: [(src, dst_name, dst_path), ...]"""
    conn = _connect(db_path)
    for filepath, mtime, source in files:
        conn.execute(
            "INSERT INTO files(filepath, mtime, chunk_count, source) VALUES (?, ?, 1, ?)",
            (filepath, mtime, source))
    for src, dst_name, dst_path in links:
        conn.execute(
            "INSERT INTO links(src_path, dst_name, dst_path) VALUES (?, ?, ?)",
            (src, dst_name, dst_path))
    conn.commit()
    conn.close()


# ---------------------------------------------------------
# [1] 基本分類
# ---------------------------------------------------------

def test_compute_unused_stats_basic_classification():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("A.md", STALE_CUTOFF - 100, SOURCE_VAULT),  # 古い・被リンク無し -> unused
            ("B.md", STALE_CUTOFF - 100, SOURCE_VAULT),  # 古い・被リンク有り -> used
            ("C.md", NOW, SOURCE_VAULT),                  # 新しい -> used
        ], links=[("X.md", "B", "B.md")])

        stats = health._compute_unused_stats(db_path=db, now=NOW)
        assert stats["total"] == 3, stats
        assert stats["unused"] == 1, stats
        assert stats["pct"] == round(100 / 3, 1), stats
    print("✅ [1] 基本分類: 古い×被リンク無しのみ unused")


# ---------------------------------------------------------
# [2] NULL dst_path トラップの回帰
# ---------------------------------------------------------

def test_compute_unused_stats_survives_null_dst_path():
    """未解決リンク (dst_path IS NULL) が混ざっていても unused が過小評価されない。

    サブクエリの `WHERE dst_path IS NOT NULL` が抜けると、SQL の三値論理で
    NOT IN が全行NULL評価になり unused が常に0になる (実際の落とし穴)。
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("A.md", STALE_CUTOFF - 100, SOURCE_VAULT),  # 古い・被リンク無し -> unused
        ], links=[("X.md", "未作成ノート", None)])  # 未解決リンク (dst_path=NULL)

        stats = health._compute_unused_stats(db_path=db, now=NOW)
        assert stats["unused"] == 1, (
            f"NULL dst_path混入でunusedが過小評価されている: {stats}")
    print("✅ [2] NULL dst_path が混ざっていても unused の過小評価(常に0)が起きない")


# ---------------------------------------------------------
# [3] source != 'vault' は除外
# ---------------------------------------------------------

def test_compute_unused_stats_excludes_other_sources():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("A.md", STALE_CUTOFF - 100, SOURCE_VAULT),
            ("crypto_log_1", STALE_CUTOFF - 100, "crypto"),  # 行動ログ (別source)
        ])
        stats = health._compute_unused_stats(db_path=db, now=NOW)
        assert stats["total"] == 1, stats
        assert stats["unused"] == 1, stats
    print("✅ [3] source != 'vault' の行が集計から除外される")


# ---------------------------------------------------------
# [4] 空DB
# ---------------------------------------------------------

def test_check_vault_health_skips_when_empty():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        note = os.path.join(tmp, "Vault_Health.md")
        _connect(db).close()  # スキーマだけ作る (0件)

        result = health.check_vault_health(db_path=db, note_path=note, now=NOW)
        assert "スキップ" in result, result
        assert not os.path.exists(note), "空DBなのにノートが作成された"
    print("✅ [4] 空DBは total==0 でノート未作成")


# ---------------------------------------------------------
# [5] 追記のみ・ヘッダー重複なし
# ---------------------------------------------------------

def test_check_vault_health_appends_without_duplicate_header():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        note = os.path.join(tmp, "System", "Vault_Health.md")
        _make_db(db, files=[("A.md", STALE_CUTOFF - 100, SOURCE_VAULT)])

        r1 = health.check_vault_health(db_path=db, note_path=note, now=NOW, today="2026-08-01")
        assert "未使用率" in r1, r1
        r2 = health.check_vault_health(db_path=db, note_path=note, now=NOW, today="2026-08-02")
        assert "未使用率" in r2, r2

        with open(note, encoding="utf-8") as f:
            content = f.read()
        assert content.count("# Vault Health") == 1, content
        assert "2026-08-01" in content and "2026-08-02" in content, content
    print("✅ [5] 追記のみ・ヘッダー重複なし (2回連続実行)")


# ---------------------------------------------------------
# [6] 境界値
# ---------------------------------------------------------

def test_compute_unused_stats_boundary_mtime_equals_cutoff():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[("A.md", STALE_CUTOFF, SOURCE_VAULT)])  # ちょうど cutoff
        stats = health._compute_unused_stats(db_path=db, now=NOW)
        assert stats["unused"] == 0, (
            f"mtime==cutoff ちょうどはカウントしないはず: {stats}")
    print("✅ [6] 境界値: mtime == cutoff ちょうどはカウントしない")


# ---------------------------------------------------------
# [7] status欠落率: 基本集計
# ---------------------------------------------------------

def test_compute_frontmatter_gap_stats_basic():
    with tempfile.TemporaryDirectory() as tmp:
        main_dir = os.path.join(tmp, "30_Main")
        os.makedirs(main_dir)
        with open(os.path.join(main_dir, "A.md"), "w", encoding="utf-8") as f:
            f.write("---\nstatus: active\n---\n\nBody\n")
        with open(os.path.join(main_dir, "B.md"), "w", encoding="utf-8") as f:
            f.write("# No frontmatter\n\nBody\n")
        with open(os.path.join(main_dir, "C.txt"), "w", encoding="utf-8") as f:
            f.write("not markdown, should be ignored\n")

        stats = health._compute_frontmatter_gap_stats(vault_root=tmp)
        assert stats["total"] == 2, stats  # .md のみ (C.txt は除外)
        assert stats["missing_status"] == 1, stats  # B.md のみ欠落
        assert stats["pct"] == 50.0, stats
    print("✅ [7] status欠落率: .mdのみ対象、statusあり/なしを正しく数える")


# ---------------------------------------------------------
# [8] 対象ディレクトリ不在
# ---------------------------------------------------------

def test_compute_frontmatter_gap_stats_missing_dir():
    with tempfile.TemporaryDirectory() as tmp:
        stats = health._compute_frontmatter_gap_stats(vault_root=tmp)
        assert stats == {"total": 0, "missing_status": 0, "pct": 0.0,
                          "target_subdir": health.STATUS_GAP_TARGET_SUBDIR}, stats
    print("✅ [8] 30_Main不在は total==0 (例外を出さない)")


# ---------------------------------------------------------
# [9] check_vault_health の追記行にstatus欠落率が含まれる
# ---------------------------------------------------------

def test_check_vault_health_includes_frontmatter_gap_column():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        note = os.path.join(tmp, "Vault_Health.md")
        main_dir = os.path.join(tmp, "30_Main")
        os.makedirs(main_dir)
        with open(os.path.join(main_dir, "A.md"), "w", encoding="utf-8") as f:
            f.write("# No frontmatter\n\nBody\n")
        _make_db(db, files=[("A.md", STALE_CUTOFF - 100, SOURCE_VAULT)])

        result = health.check_vault_health(db_path=db, note_path=note, now=NOW,
                                            today="2026-08-01", vault_root=tmp)
        assert "status欠落率" in result, result
        with open(note, encoding="utf-8") as f:
            content = f.read()
        assert "100.0%" in content, content  # A.md 1件が丸ごと欠落 -> 100.0%
    print("✅ [9] check_vault_healthの追記行にstatus欠落率が含まれる")


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
