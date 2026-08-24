# -*- coding: utf-8 -*-
"""tests/test_resurfacing.py — 再浮上(Resurfacing)機構

検証する不変条件:
  [1] 1年前の今日のデイリーノートが存在すればパスを返す
  [2] 存在しなければ None
  [3] うるう年をまたいでも timedelta(days=365) の計算通りの日付になる
  [4] 高評価放置ノート: 被リンク2件・mtime90日超 -> 選ばれる
  [5] しきい値境界: 被リンク1件(MIN_BACKLINKS未満) -> 選ばれない
  [6] しきい値境界: mtime == cutoff ちょうど -> 対象外
  [7] NULL dst_path トラップの回帰: 未解決リンクが混ざっていても過大/過小評価しない
  [8] 複数stale候補から被リンク数最大が選ばれる
  [9] 被リンク数同率時、より古い(mtime小)ものが選ばれる
  [10] 両方該当なし -> _build_resurfacing_section が ""
  [11] 両方該当あり -> 2行とも含まれ見出しは1つ
  [12] 片方のみ該当 -> その1行のみ
  [13] DBファイル未構築でも例外を出さず "" を返す
  [14] source != 'vault' の行は候補から除外される
  [15] DB破損時、stale側は例外を吸収して "" 扱い・one_year_ago側の結果は道連れにされず生き残る
  [16] MOC (被リンク最大でも) は除外され、次点の通常ノートが選ばれる
  [17] cooldown中の候補は飛ばされ、次点の被リンク数のノートが選ばれる
  [18] cooldown_days 経過後は同じノートが再び選ばれる
  [19] 実際に出したノートは記録され、直後の再呼び出しでは出なくなる
  [20] Briefing/Crypto/XTrend/News配下・00_home.md は被リンク最大でも除外され、
       通常ノートが選ばれる

本番Vault・logs/vault_index.db には一切触れない (tempfile の DB/Vault path、seen.jsonも
tmp配下のみ使う)。
"""
import os
import sys
import gc
import datetime
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tool_vault_index import _connect, SOURCE_VAULT  # noqa: E402
from tools import tool_resurfacing as rs  # noqa: E402

NOW = 1_000_000_000.0
STALE_CUTOFF = NOW - rs.STALE_DAYS * 86400


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


def _make_daily_note(vault_root, date):
    daily_dir = os.path.join(vault_root, "01_Daily")
    os.makedirs(daily_dir, exist_ok=True)
    with open(os.path.join(daily_dir, f"{date.isoformat()}.md"), "w", encoding="utf-8") as f:
        f.write("# Daily\n")


# ---------------------------------------------------------
# [1][2] 1年前の今日
# ---------------------------------------------------------

def test_find_one_year_ago_note_exists():
    with tempfile.TemporaryDirectory() as tmp:
        today = datetime.date(2026, 8, 22)
        _make_daily_note(tmp, today - datetime.timedelta(days=365))
        result = rs._find_one_year_ago_note(today=today, vault_root=tmp)
        assert result == "01_Daily/2025-08-22.md", result
    print("✅ [1] 1年前の今日のデイリーノートが存在すればパスを返す")


def test_find_one_year_ago_note_missing():
    with tempfile.TemporaryDirectory() as tmp:
        today = datetime.date(2026, 8, 22)
        result = rs._find_one_year_ago_note(today=today, vault_root=tmp)
        assert result is None, result
    print("✅ [2] 1年前の今日のデイリーノートが無ければ None")


def test_find_one_year_ago_note_leap_year_boundary():
    with tempfile.TemporaryDirectory() as tmp:
        today = datetime.date(2028, 3, 1)  # 2028年はうるう年
        expected = today - datetime.timedelta(days=365)
        _make_daily_note(tmp, expected)
        result = rs._find_one_year_ago_note(today=today, vault_root=tmp)
        assert result == f"01_Daily/{expected.isoformat()}.md", result
    print(f"✅ [3] うるう年境界でも timedelta(days=365) 通りの日付 ({expected.isoformat()}) になる")


# ---------------------------------------------------------
# [4] 高評価放置ノート: 基本ケース
# ---------------------------------------------------------

def test_find_stale_popular_note_basic():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("Old.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[("X.md", "Old", "Old.md"), ("Y.md", "Old", "Old.md")])

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result is not None
        assert result["filepath"] == "Old.md", result
        assert result["backlinks"] == 2, result
    print("✅ [4] 被リンク2件・mtime90日超のノートが選ばれる")


# ---------------------------------------------------------
# [5] しきい値境界: 被リンク数
# ---------------------------------------------------------

def test_find_stale_popular_note_below_min_backlinks():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("Old.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[("X.md", "Old", "Old.md")])  # 被リンク1件のみ

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result is None, result
    print("✅ [5] 被リンク1件 (MIN_BACKLINKS未満) は選ばれない")


# ---------------------------------------------------------
# [6] しきい値境界: mtime
# ---------------------------------------------------------

def test_find_stale_popular_note_boundary_mtime_equals_cutoff():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("Old.md", STALE_CUTOFF, SOURCE_VAULT),  # ちょうど cutoff
        ], links=[("X.md", "Old", "Old.md"), ("Y.md", "Old", "Old.md")])

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result is None, (
            f"mtime==cutoff ちょうどは対象外のはず: {result}")
    print("✅ [6] 境界値: mtime == cutoff ちょうどは対象外")


# ---------------------------------------------------------
# [7] NULL dst_path トラップの回帰
# ---------------------------------------------------------

def test_find_stale_popular_note_survives_null_dst_path():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("Old.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[
            ("X.md", "Old", "Old.md"),
            ("Y.md", "Old", "Old.md"),
            ("Z.md", "未作成ノート", None),  # 未解決リンク (dst_path=NULL)
        ])

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result is not None and result["backlinks"] == 2, (
            f"NULL dst_path混入で被リンク数が誤集計されている: {result}")
    print("✅ [7] NULL dst_path が混ざっていても被リンク数が正しく集計される")


# ---------------------------------------------------------
# [8] 複数候補から被リンク数最大が選ばれる
# ---------------------------------------------------------

def test_find_stale_popular_note_picks_max_backlinks():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("Low.md", STALE_CUTOFF - 100, SOURCE_VAULT),
            ("High.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[
            ("X.md", "Low", "Low.md"),
            ("X.md", "High", "High.md"),
            ("Y.md", "High", "High.md"),
            ("Z.md", "High", "High.md"),
        ])

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result["filepath"] == "High.md", result
        assert result["backlinks"] == 3, result
    print("✅ [8] 複数stale候補から被リンク数最大のものが選ばれる")


# ---------------------------------------------------------
# [9] 同率タイブレーク
# ---------------------------------------------------------

def test_find_stale_popular_note_tie_break_by_older_mtime():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("Newer.md", STALE_CUTOFF - 100, SOURCE_VAULT),
            ("Older.md", STALE_CUTOFF - 5000, SOURCE_VAULT),  # より古い
        ], links=[
            ("X.md", "Newer", "Newer.md"),
            ("Y.md", "Newer", "Newer.md"),
            ("X.md", "Older", "Older.md"),
            ("Y.md", "Older", "Older.md"),
        ])

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result["filepath"] == "Older.md", result
    print("✅ [9] 被リンク数同率時、より古い(mtime小)ものが選ばれる")


# ---------------------------------------------------------
# [10][11][12] _build_resurfacing_section の組み立て
# ---------------------------------------------------------

def test_build_resurfacing_section_both_missing():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _connect(db).close()
        today = datetime.date(2026, 8, 22)

        section = rs._build_resurfacing_section(today=today, vault_root=tmp, db_path=db, now=NOW,
                                          seen_path=os.path.join(tmp, "seen.json"))
        assert section == "", section
    print("✅ [10] 両方該当なしは空文字")


def test_build_resurfacing_section_both_present():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        today = datetime.date(2026, 8, 22)
        _make_daily_note(tmp, today - datetime.timedelta(days=365))
        _make_db(db, files=[
            ("Old.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[("X.md", "Old", "Old.md"), ("Y.md", "Old", "Old.md")])

        section = rs._build_resurfacing_section(today=today, vault_root=tmp, db_path=db, now=NOW,
                                          seen_path=os.path.join(tmp, "seen.json"))
        assert section.count("## 🔁 再浮上ノート") == 1, section
        assert "2025-08-22" in section, section
        assert "Old" in section and "被リンク2件" in section, section
    print("✅ [11] 両方該当ありは2行とも含まれ見出しは1つ")


def test_build_resurfacing_section_only_one_year_ago():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        today = datetime.date(2026, 8, 22)
        _make_daily_note(tmp, today - datetime.timedelta(days=365))
        _connect(db).close()  # stale候補なし

        section = rs._build_resurfacing_section(today=today, vault_root=tmp, db_path=db, now=NOW,
                                          seen_path=os.path.join(tmp, "seen.json"))
        assert "2025-08-22" in section, section
        assert "被リンク" not in section, section
    print("✅ [12] 片方のみ該当ならその1行のみ")


# ---------------------------------------------------------
# [13] DBファイル未構築
# ---------------------------------------------------------

def test_build_resurfacing_section_missing_db_file():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "does_not_exist", "index.db")
        today = datetime.date(2026, 8, 22)

        section = rs._build_resurfacing_section(today=today, vault_root=tmp, db_path=db, now=NOW,
                                          seen_path=os.path.join(tmp, "seen.json"))
        assert section == "", section
    print("✅ [13] DBファイル未構築でも例外を出さず空文字を返す")


# ---------------------------------------------------------
# [14] source != 'vault' の除外
# ---------------------------------------------------------

def test_find_stale_popular_note_excludes_other_sources():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("Crypto.md", STALE_CUTOFF - 100, "crypto"),
        ], links=[("X.md", "Crypto", "Crypto.md"), ("Y.md", "Crypto", "Crypto.md")])

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result is None, result
    print("✅ [14] source != 'vault' の行は候補から除外される")


# ---------------------------------------------------------
# [16] MOC除外
# ---------------------------------------------------------

def test_find_stale_popular_note_excludes_moc():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("MOC_AI.md", STALE_CUTOFF - 100, SOURCE_VAULT),   # 被リンク最大だがMOC
            ("Old.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[
            ("X.md", "MOC_AI", "MOC_AI.md"),
            ("Y.md", "MOC_AI", "MOC_AI.md"),
            ("Z.md", "MOC_AI", "MOC_AI.md"),
            ("X.md", "Old", "Old.md"),
            ("Y.md", "Old", "Old.md"),
        ])

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result["filepath"] == "Old.md", (
            f"MOCはハブノートが正常にstaleなだけで対象外のはず: {result}")
    print("✅ [16] MOC (被リンク最大でも) は除外され、次点の通常ノートが選ばれる")


# ---------------------------------------------------------
# [20] 自動生成レポート系 (Briefing/Crypto/XTrend/News) + 00_home.md の除外
# ---------------------------------------------------------

def test_find_stale_popular_note_excludes_auto_generated_reports():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        excluded = [
            "30_Main/Briefing/Briefing_2026-03-14.md",
            "50_Archive/2026-W07/03_Crypto/Crypto_BTC_USDT_2026-02-16.md",
            "50_Archive/2026-W10/40_Report_XTrend/2026-03-14_xtrend.md",
            # 大小文字が揺れても検出できることの確認 (MOC判定と同じlower正規化)
            "50_Archive/2026-W09/02_NEWS/News_2026-03-04.md",
            "00_Home.md",
        ]
        files = [(p, STALE_CUTOFF - 100, SOURCE_VAULT) for p in excluded]
        files.append(("Old.md", STALE_CUTOFF - 100, SOURCE_VAULT))
        links = []
        # 除外対象ほど被リンクを多く積み、除外ロジックが無ければ確実にこれらが選ばれる状態にする
        for i, p in enumerate(excluded):
            for j in range(6 - i):
                links.append((f"src{i}_{j}.md", "x", p))
        links.append(("X.md", "Old", "Old.md"))
        links.append(("Y.md", "Old", "Old.md"))
        _make_db(db, files=files, links=links)

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen={})
        assert result["filepath"] == "Old.md", (
            f"Briefing/Crypto/XTrend/News/00_homeは除外され通常ノートが選ばれるはず: {result}")
    print("✅ [20] Briefing/Crypto/XTrend/News配下・00_home.mdは被リンク最大でも除外される")


# ---------------------------------------------------------
# [17][18] cooldown: 直近提示済みは次点に譲り、期間経過後は復活する
# ---------------------------------------------------------

def test_find_stale_popular_note_cooldown_skips_recently_shown():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("High.md", STALE_CUTOFF - 100, SOURCE_VAULT),
            ("Low.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[
            ("X.md", "High", "High.md"),
            ("Y.md", "High", "High.md"),
            ("Z.md", "High", "High.md"),
            ("X.md", "Low", "Low.md"),
            ("Y.md", "Low", "Low.md"),
        ])
        recently = datetime.datetime.fromtimestamp(NOW) - datetime.timedelta(days=5)
        seen = {"High.md": {"shown_at": recently.isoformat()}}

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen=seen)
        assert result["filepath"] == "Low.md", (
            f"cooldown中のHigh.mdを飛ばして次点が選ばれるはず: {result}")
    print("✅ [17] cooldown中の候補は飛ばされ、次点の被リンク数のノートが選ばれる")


def test_find_stale_popular_note_cooldown_expires():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("High.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[("X.md", "High", "High.md"), ("Y.md", "High", "High.md")])
        long_ago = (datetime.datetime.fromtimestamp(NOW)
                    - datetime.timedelta(days=rs.STALE_COOLDOWN_DAYS + 1))
        seen = {"High.md": {"shown_at": long_ago.isoformat()}}

        result = rs._find_stale_popular_note(db_path=db, now=NOW, seen=seen)
        assert result is not None and result["filepath"] == "High.md", (
            f"cooldown経過後は再び選ばれるはず: {result}")
    print("✅ [18] cooldown_days 経過後は同じノートが再び選ばれる")


# ---------------------------------------------------------
# [19] _build_resurfacing_section が実際に出した回だけ記録する
# ---------------------------------------------------------

def test_build_resurfacing_section_records_shown_note():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        today = datetime.date(2026, 8, 22)
        _make_db(db, files=[
            ("Old.md", STALE_CUTOFF - 100, SOURCE_VAULT),
        ], links=[("X.md", "Old", "Old.md"), ("Y.md", "Old", "Old.md")])
        seen_path = os.path.join(tmp, "seen.json")

        section1 = rs._build_resurfacing_section(
            today=today, vault_root=tmp, db_path=db, now=NOW, seen_path=seen_path)
        assert "Old" in section1, section1

        # 直後にもう一度呼んでも (cooldown内なので) 同じノートは出ない
        section2 = rs._build_resurfacing_section(
            today=today, vault_root=tmp, db_path=db, now=NOW, seen_path=seen_path)
        assert "被リンク" not in section2, (
            f"直後の再呼び出しではcooldownで同じノートが出ないはず: {section2!r}")
    print("✅ [19] 実際に出したノートは記録され、直後の再呼び出しでは出なくなる")


# ---------------------------------------------------------
# [15] DB破損時に one_year_ago 側が道連れにされない
# ---------------------------------------------------------

def test_build_resurfacing_section_survives_broken_db_keeps_one_year_ago():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        with open(db, "w", encoding="utf-8") as f:
            f.write("not a valid sqlite database")  # 壊れたDBファイル
        today = datetime.date(2026, 8, 22)
        _make_daily_note(tmp, today - datetime.timedelta(days=365))

        section = rs._build_resurfacing_section(today=today, vault_root=tmp, db_path=db, now=NOW,
                                          seen_path=os.path.join(tmp, "seen.json"))
        # tool_vault_index._connect() は executescript 失敗時に conn を close せず例外を
        # 伝播させる (例外オブジェクトのトレースバックがフレームを保持しconnが残る)。
        # Windowsではこの未close分がTemporaryDirectory削除時にWinError 32を誘発するため
        # 明示的にGCしてから一時ディレクトリを抜ける。
        gc.collect()
        assert "2025-08-22" in section, (
            f"DB破損の道連れでone_year_ago側まで消えている: {section!r}")
        assert "被リンク" not in section, section
    print("✅ [15] DB破損時もone_year_ago側の結果は道連れにされず生き残る")


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
