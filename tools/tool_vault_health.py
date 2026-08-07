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
from lib.lib_obsidian_utils import format_wikilink
from tools.tool_vault_index import DB_PATH, SOURCE_VAULT, _connect

STALE_DAYS = 14
HEALTH_NOTE_PATH = os.path.join(VAULT_ROOT, "00_System", "Vault_Health.md")

_HEADER = (
    "# Vault Health\n\n"
    f"週次で自動追記される、未使用ノート率 ({STALE_DAYS}日以上更新が無く被リンクも無いノートの割合) の推移。\n\n"
    "| 日付 | 未使用率 | 未使用件数/総ノート数 |\n"
    "|---|---|---|\n"
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


def _format_health_entry(stats: dict, today: str = None) -> str:
    """追記する1行 (純関数)。"""
    today = today or datetime.date.today().isoformat()
    return f"| {today} | {stats['pct']}% | {stats['unused']}/{stats['total']}件 |\n"


def check_vault_health(db_path: str = DB_PATH, note_path: str = HEALTH_NOTE_PATH,
                        now: float = None, today: str = None) -> str:
    """スケジューラ/手動エントリポイント。例外は握りつぶさず f"Error: {e}" を返す。"""
    try:
        stats = _compute_unused_stats(db_path=db_path, now=now)
        if stats["total"] == 0:
            return "Vault Health: インデックス未構築または空のため計測をスキップしました。"

        os.makedirs(os.path.dirname(note_path), exist_ok=True)
        if not os.path.exists(note_path):
            with open(note_path, "w", encoding="utf-8") as f:
                f.write(_HEADER)
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(_format_health_entry(stats, today=today))

        return (f"Vault Health: 未使用率 {stats['pct']}% "
                f"({stats['unused']}/{stats['total']}件, {STALE_DAYS}日以上更新なし＆被リンク0) "
                f"→ {format_wikilink('Vault_Health')}")
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(check_vault_health())
