# -*- coding: utf-8 -*-
"""
tools/tool_resurfacing.py — 再浮上(Resurfacing)機構

朝のブリーフィングに「1年前の今日のデイリーノート」「高評価(被リンク数)だったのに
90日間更新の無いノート」を差し込む固定セクションを生成する。

tool_vault_health.py と同じ流儀: tool_vault_index.py (900行超) に機能を足さず、
DB_PATH/SOURCE_VAULT/_connect だけを越境インポートして使う。
"""
import json
import os
import sys
import time
import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.paths import VAULT_ROOT, LOGS_DIR
from lib.lib_obsidian_utils import format_wikilink
from lib.lib_shared import save_json
from tools.tool_vault_index import DB_PATH, SOURCE_VAULT, _connect

STALE_DAYS = 90
MIN_BACKLINKS = 2
# MOCは「被リンクが多いまま更新されない」のが構造上の正常状態 (ハブノートの役割)
# であり、「昔重要だったが放置された」ノートとは意味が違う。除外しないと
# 被リンク最大は常にMOCが独占し、他のノートが一生浮上しない。
_MOC_PREFIX = "moc_"
SEEN_PATH = os.path.join(LOGS_DIR, "resurfacing_seen.json")
STALE_COOLDOWN_DAYS = 30
# cooldown中の候補を読み飛ばして次点を拾うための候補プール数。
CANDIDATE_POOL = 20


def _find_one_year_ago_note(today: datetime.date = None, vault_root: str = VAULT_ROOT) -> str | None:
    """01_Daily/{365日前}.md の存在チェックのみ。存在すれば Vault相対パスを返す。"""
    today = today or datetime.date.today()
    target = today - datetime.timedelta(days=365)
    rel_path = f"01_Daily/{target.isoformat()}.md"
    if os.path.exists(os.path.join(vault_root, rel_path)):
        return rel_path
    return None


def _load_seen(seen_path: str = SEEN_PATH) -> dict:
    if not os.path.exists(seen_path):
        return {}
    try:
        with open(seen_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_shown(filepath: str, now: datetime.datetime = None,
                   seen_path: str = SEEN_PATH) -> None:
    now = now or datetime.datetime.now()
    seen = _load_seen(seen_path)
    seen[filepath] = {"shown_at": now.isoformat()}
    save_json(seen_path, seen)


def _find_stale_popular_note(db_path: str = DB_PATH, now: float = None,
                              stale_days: int = STALE_DAYS,
                              min_backlinks: int = MIN_BACKLINKS,
                              seen: dict = None,
                              cooldown_days: int = STALE_COOLDOWN_DAYS) -> dict | None:
    """被リンク数最大 かつ mtime が stale_days 日以上前のノートを1件返す。

    `dst_path IS NOT NULL` はON句としては冗長 (NULL = filepath は3値論理で自然に
    falseになりJOINされない) だが、tool_vault_health.py の既知の落とし穴
    (NOT INでの三値論理事故) を踏まえ意図を明文化するために残す。

    MOC (被リンクが多いまま更新されないのが正常なハブノート) と、直近
    cooldown_days 日以内に既出の候補は除外し、候補プールの中から次点を拾う
    (実測: フィルタ無しだと被リンク最大のMOCが毎朝固定で出続けていた)。
    """
    now_ts = now if now is not None else time.time()
    cutoff = now_ts - stale_days * 86400
    seen = seen if seen is not None else _load_seen()
    cooldown_cutoff = datetime.datetime.fromtimestamp(now_ts) - datetime.timedelta(days=cooldown_days)

    conn = _connect(db_path)
    try:
        rows = conn.execute(
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
            LIMIT :pool
            """,
            {"source": SOURCE_VAULT, "cutoff": cutoff, "min_backlinks": min_backlinks,
             "pool": CANDIDATE_POOL},
        ).fetchall()
    finally:
        conn.close()

    for filepath, backlinks, mtime in rows:
        basename = os.path.splitext(os.path.basename(filepath))[0]
        if basename.lower().startswith(_MOC_PREFIX):
            continue
        shown_at = seen.get(filepath, {}).get("shown_at", "")
        try:
            if shown_at and datetime.datetime.fromisoformat(shown_at) > cooldown_cutoff:
                continue
        except ValueError:
            pass
        return {"filepath": filepath, "backlinks": backlinks, "mtime": mtime}
    return None


def _build_resurfacing_section(today: datetime.date = None, vault_root: str = VAULT_ROOT,
                                db_path: str = DB_PATH, now: float = None,
                                seen_path: str = SEEN_PATH) -> str:
    """1年前ノート・高評価放置ノートを集め、XTrendと同形式のMarkdownセクションを返す。
    両方無くてもエラーにせず空文字を返す(ブリーフィング全体を落とさない)。
    """
    try:
        one_year_ago = _find_one_year_ago_note(today=today, vault_root=vault_root)
    except Exception as e:
        print(f"[Resurfacing] Error (one_year_ago): {e}")
        one_year_ago = None

    try:
        seen = _load_seen(seen_path)
        stale_popular = _find_stale_popular_note(db_path=db_path, now=now, seen=seen)
        if stale_popular:
            # 実際に出したときだけ記録する (cooldown_days 以内の再提示を防ぐ)。
            # 呼ぶたびに書くと毎日同じ更新時刻が積み上がるので、選ばれた回だけでよい。
            shown_at = datetime.datetime.fromtimestamp(now) if now is not None else None
            _record_shown(stale_popular["filepath"], now=shown_at, seen_path=seen_path)
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
