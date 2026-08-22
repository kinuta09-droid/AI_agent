# -*- coding: utf-8 -*-
"""
tools/tool_resurfacing.py — 再浮上(Resurfacing)機構

朝のブリーフィングに「1年前の今日のデイリーノート」「高評価(被リンク数)だったのに
90日間更新の無いノート」を差し込む固定セクションを生成する。

tool_vault_health.py と同じ流儀: tool_vault_index.py (900行超) に機能を足さず、
DB_PATH/SOURCE_VAULT/_connect だけを越境インポートして使う。
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

STALE_DAYS = 90
MIN_BACKLINKS = 2


def _find_one_year_ago_note(today: datetime.date = None, vault_root: str = VAULT_ROOT) -> str | None:
    """01_Daily/{365日前}.md の存在チェックのみ。存在すれば Vault相対パスを返す。"""
    today = today or datetime.date.today()
    target = today - datetime.timedelta(days=365)
    rel_path = f"01_Daily/{target.isoformat()}.md"
    if os.path.exists(os.path.join(vault_root, rel_path)):
        return rel_path
    return None


def _find_stale_popular_note(db_path: str = DB_PATH, now: float = None,
                              stale_days: int = STALE_DAYS,
                              min_backlinks: int = MIN_BACKLINKS) -> dict | None:
    """被リンク数最大 かつ mtime が stale_days 日以上前のノートを1件返す。

    `dst_path IS NOT NULL` はON句としては冗長 (NULL = filepath は3値論理で自然に
    falseになりJOINされない) だが、tool_vault_health.py の既知の落とし穴
    (NOT INでの三値論理事故) を踏まえ意図を明文化するために残す。
    """
    cutoff = (now if now is not None else time.time()) - stale_days * 86400
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT f.filepath, COUNT(l.src_path) AS backlinks, f.mtime
            FROM files f
            LEFT JOIN links l
                ON l.dst_path = f.filepath AND l.dst_path IS NOT NULL
            WHERE f.source = :source
              AND f.mtime < :cutoff
            GROUP BY f.filepath
            HAVING COUNT(l.src_path) >= :min_backlinks
            ORDER BY backlinks DESC, f.mtime ASC
            LIMIT 1
            """,
            {"source": SOURCE_VAULT, "cutoff": cutoff, "min_backlinks": min_backlinks},
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"filepath": row[0], "backlinks": row[1], "mtime": row[2]}


def _build_resurfacing_section(today: datetime.date = None, vault_root: str = VAULT_ROOT,
                                db_path: str = DB_PATH, now: float = None) -> str:
    """1年前ノート・高評価放置ノートを集め、XTrendと同形式のMarkdownセクションを返す。
    両方無くてもエラーにせず空文字を返す(ブリーフィング全体を落とさない)。
    """
    try:
        one_year_ago = _find_one_year_ago_note(today=today, vault_root=vault_root)
    except Exception as e:
        print(f"[Resurfacing] Error (one_year_ago): {e}")
        one_year_ago = None

    try:
        stale_popular = _find_stale_popular_note(db_path=db_path, now=now)
    except Exception as e:
        print(f"[Resurfacing] Error (stale_popular): {e}")
        stale_popular = None

    if not one_year_ago and not stale_popular:
        return ""

    lines = ["", "---", "", "## 🔁 再浮上ノート", ""]
    if one_year_ago:
        title = os.path.splitext(os.path.basename(one_year_ago))[0]
        lines.append(f"- 📅 1年前の今日: {format_wikilink(title)}")
    if stale_popular:
        title = os.path.splitext(os.path.basename(stale_popular["filepath"]))[0]
        lines.append(
            f"- ⭐ {STALE_DAYS}日間更新なしの高評価ノート "
            f"(被リンク{stale_popular['backlinks']}件): {format_wikilink(title)}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(_build_resurfacing_section() or "(該当ノートなし)")
