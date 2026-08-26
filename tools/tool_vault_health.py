# -*- coding: utf-8 -*-
"""
tools/tool_vault_health.py — Vault ヘルスメトリクス (週次・LLM呼び出しなし)

plans/vault-health-and-contradictions.md Feature 1。
「集めるだけで満足」失敗モードを検知する数値指標として、14日以上更新が無く
被リンクも無い (=誰にも参照されていない) ノートの比率を週次で
00_System/Vault_Health.md に追記する。

tool_crypto_stats.py が tool_crypto.py のストアを読むだけの集計モジュールとして
分離されているのと同じ流儀: tool_vault_index.py (900行超) に機能を足さず、
DB_PATH/SOURCE_VAULT/_connect だけを越境インポートして使う
(tool_activity_index.py が同じ手法で "private" ヘルパーを使う前例あり)。
"""
import os
import sys
import time
import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.paths import VAULT_ROOT
from lib.lib_obsidian_utils import format_wikilink, read_frontmatter_field
from tools.tool_vault_index import DB_PATH, SOURCE_VAULT, _connect

STALE_DAYS = 14
HEALTH_NOTE_PATH = os.path.join(VAULT_ROOT, "00_System", "Vault_Health.md")

# plans/obsidian-agent-native-notes.md: status frontmatter欠落率の集計対象。
# 30_Main のみが対象 (記事の言う「決定/知見」に一番近いノート種別)。
# 01_Daily/02_News/03_Crypto/40_Report は生成物でありstatus概念が無いので対象外、
# 00_inbox の Fleeting Note は既に status を持つ (tool_list_open_ideas) ので対象外。
STATUS_GAP_TARGET_SUBDIR = "30_Main"

# 30_Main配下でも youtube_Source は人間が書く「決定/知見」ノートではなく
# 01_Daily等と同じ生成物・クリップなので集計対象から除外する。
# (Briefingは2026-08-26のBriefing/Dailynote統合で30_Main/Briefingから01_Dailyへ
# 生成先ごと移行したため、このリストからも除外した)
STATUS_GAP_EXCLUDED_DIRS = {"youtube_Source"}

_HEADER = (
    "# Vault Health\n\n"
    f"週次で自動追記される、未使用ノート率 ({STALE_DAYS}日以上更新が無く被リンクも無いノートの割合) と"
    f"{STATUS_GAP_TARGET_SUBDIR}のstatus欠落率の推移。\n\n"
    "| 日付 | 未使用率 | 未使用件数/総ノート数 | status欠落率 | 欠落件数/総ノート数 |\n"
    "|---|---|---|---|---|\n"
)


def _compute_unused_stats(db_path: str = DB_PATH, now: float = None) -> dict:
    """未使用ノート統計を SQL で集計する。

    「未使用」= mtime が cutoff より古い AND どのノートからもリンクされていない。
    総数0は正常系 (空Vault/未構築DB) として扱い、呼び出し側が判断する。

    **落とし穴**: サブクエリの `WHERE dst_path IS NOT NULL` は必須。
    links.dst_path は未解決リンクで常に NULL になり得るため、これを外すと
    SQL の三値論理で NOT IN が全行 NULL(=false) 評価になり、unused が常に0という
    無言のメトリクス破壊が起きる。
    """
    cutoff = (now if now is not None else time.time()) - STALE_DAYS * 86400
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE WHEN mtime < :cutoff
                         AND filepath NOT IN (
                             SELECT dst_path FROM links WHERE dst_path IS NOT NULL
                         )
                    THEN 1 ELSE 0 END
                ) AS unused
            FROM files
            WHERE source = :source
            """,
            {"cutoff": cutoff, "source": SOURCE_VAULT},
        ).fetchone()
    finally:
        conn.close()
    total = row[0] or 0
    unused = row[1] or 0
    pct = round(100.0 * unused / total, 1) if total else 0.0
    return {"total": total, "unused": unused, "pct": pct,
            "stale_days": STALE_DAYS, "cutoff": cutoff}


def _compute_frontmatter_gap_stats(vault_root: str = VAULT_ROOT,
                                    target_subdir: str = STATUS_GAP_TARGET_SUBDIR) -> dict:
    """target_subdir 配下のノートで status: frontmatter が欠落している率を集計する。

    vault_index.db の files テーブルは filepath/mtime/chunk_count/source のみで
    frontmatter内容を保持しないため、tools/tool_obsidian.py:_keyword_hits と同じ
    os.walk 方式でファイルを直接読む (LLM不使用)。対象ディレクトリが無ければ
    total=0 として返す (空Vault/未作成フォルダは正常系)。
    """
    target_dir = os.path.join(vault_root, target_subdir)
    total = 0
    missing = 0
    if os.path.isdir(target_dir):
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if d not in STATUS_GAP_EXCLUDED_DIRS]
            for file in files:
                if not file.endswith(".md"):
                    continue
                total += 1
                path = os.path.join(root, file)
                if read_frontmatter_field(path, "status") is None:
                    missing += 1
    pct = round(100.0 * missing / total, 1) if total else 0.0
    return {"total": total, "missing_status": missing, "pct": pct,
            "target_subdir": target_subdir}


def _format_health_entry(stats: dict, today: str = None, gap_stats: dict = None) -> str:
    """追記する1行 (純関数)。gap_stats が無ければ従来どおり3列 (後方互換用)。"""
    today = today or datetime.date.today().isoformat()
    base = f"| {today} | {stats['pct']}% | {stats['unused']}/{stats['total']}件 |"
    if gap_stats is None:
        return base + "\n"
    return (base + f" {gap_stats['pct']}% |"
            f" {gap_stats['missing_status']}/{gap_stats['total']}件 |\n")


def check_vault_health(db_path: str = DB_PATH, note_path: str = HEALTH_NOTE_PATH,
                        now: float = None, today: str = None,
                        vault_root: str = VAULT_ROOT) -> str:
    """スケジューラ/手動エントリポイント。例外は握りつぶさず f"Error: {e}" を返す。"""
    try:
        stats = _compute_unused_stats(db_path=db_path, now=now)
        if stats["total"] == 0:
            return "Vault Health: インデックス未構築または空のため計測をスキップしました。"

        gap_stats = _compute_frontmatter_gap_stats(vault_root=vault_root)

        os.makedirs(os.path.dirname(note_path), exist_ok=True)
        if not os.path.exists(note_path):
            with open(note_path, "w", encoding="utf-8") as f:
                f.write(_HEADER)
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(_format_health_entry(stats, today=today, gap_stats=gap_stats))

        gap_note = (f", {gap_stats['target_subdir']}のstatus欠落率 {gap_stats['pct']}% "
                    f"({gap_stats['missing_status']}/{gap_stats['total']}件)"
                    if gap_stats["total"] else "")
        return (f"Vault Health: 未使用率 {stats['pct']}% "
                f"({stats['unused']}/{stats['total']}件, {STALE_DAYS}日以上更新なし＆被リンク0)"
                f"{gap_note} → {format_wikilink('Vault_Health')}")
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(check_vault_health())
