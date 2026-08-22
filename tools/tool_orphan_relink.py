# -*- coding: utf-8 -*-
"""
tools/tool_orphan_relink.py — 孤児ノート(被リンク0件)検出+関連ノートリンク提案の週次自動化

30_Main/40_Report 配下で被リンクが1件も無いノートを週次で検出し、
tool_find_related_notes で関連ノート候補を検索、Discord の承認/却下ボタン経由で
人間が tool_confirm_related_notes によるリンク挿入を判断できるようにする。

tools/tool_note_promotion.py (ノート昇格パイプライン) と同型のアーキテクチャ:
  _compute_orphan_notes()          — SQLで孤児ノートを検出 (Gemini/Ollama呼び出しなし)
  run_weekly_orphan_relink_job()   — 夜間バッチ本体 (検出→フィルタ→候補検索→Discord通知)
  tool_approve_orphan_relink()     — 承認ボタンから呼ぶ (関連ノートへのリンク挿入)
  tool_reject_orphan_relink()      — 却下ボタンから呼ぶ (seenキャッシュに記録のみ、ファイル不変)

孤児検出SQLは tools/tool_vault_health.py の _compute_unused_stats と同じ
DB_PATH/SOURCE_VAULT/_connect 越境importの流儀 (tool_vault_index.py 900行超に
機能を足さない)。
"""
import datetime
import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.paths import LOGS_DIR
from lib.lib_shared import WORK_DIR, save_json
from lib.bot_errors import is_failure_result
from lib.file_lock import FileLock
from lib.lib_obsidian_utils import extract_note_query
from tools.tool_vault_index import DB_PATH, SOURCE_VAULT, _connect
from tools.tool_vault_hybrid import tool_find_related_notes
from tools.tool_obsidian import tool_confirm_related_notes

TARGET_DIRS = ("30_Main", "40_Report")
ORPHAN_RELINK_SLOTS = 5
PENDING_PATH = os.path.join(LOGS_DIR, "orphan_relink_pending.json")
SEEN_PATH = os.path.join(LOGS_DIR, "orphan_relink_seen.json")
PENDING_LOCK_TIMEOUT = 60.0
READ_NOTE_MAX_CHARS = 3000
# tool_find_related_notes の既定 top_k=8 は「チェック式選択でユーザーが関係の薄い
# 候補を個別に外せる」ことを前提に広めに設定されている (tool_vault_hybrid.py
# のdocstring参照)。本機能は承認/却下の2ボタンのみ(部分選択不可)で「全部リンクする」
# 一択のため、スコア閾値なしの候補を広く出すとノイズごと一括挿入されるリスクが高い。
# 間引き手段が無い分、検索側で候補数を絞る (grill指摘、2026-08-22)。
CANDIDATE_TOP_K = 3
# 候補0件が何回連続したら再提案をやめるか。
# ponytail: 固定リトライ回数。実運用で早すぎ/遅すぎが実害になったら .env 上書き可能な
# 値に格上げする (今は決め打ちで十分)。
NO_CANDIDATE_RETRY_BUDGET = 3
_PERMANENT_DECISIONS = {"linked", "rejected", "no_candidates_exhausted"}


# ---------------------------------------------------------
# 孤児ノート検出 (SQL、Gemini/Ollama呼び出しなし)
# ---------------------------------------------------------

def _compute_orphan_notes(db_path: str = DB_PATH, target_dirs: tuple = TARGET_DIRS) -> list:
    """target_dirs配下・被リンク0件のノートをmtime昇順(長く放置された順)で返す。

    tool_vault_health._compute_unused_stats と同じ落とし穴: サブクエリの
    `WHERE dst_path IS NOT NULL` は必須。links.dst_path は未解決リンクで
    常にNULLになり得るため、これを外すとSQLの三値論理でNOT INが全行NULL(=false)
    評価になり、孤児が常に0件という無言のメトリクス破壊が起きる。
    """
    clause = " OR ".join(["filepath LIKE ?"] * len(target_dirs))
    params = [SOURCE_VAULT] + [f"{d}/%" for d in target_dirs]
    sql = (
        "SELECT filepath, mtime FROM files "
        f"WHERE source = ? AND ({clause}) "
        "AND filepath NOT IN (SELECT dst_path FROM links WHERE dst_path IS NOT NULL) "
        "ORDER BY mtime ASC"
    )
    conn = _connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [{"filepath": fp, "mtime": mt} for fp, mt in rows]


# ---------------------------------------------------------
# pending (審議中) / seen (判断済み) キャッシュ
# ---------------------------------------------------------
# tool_note_promotion.py の _load_json_safe と同じ安全弁 (壊れたキャッシュは常に「空」)。

def _load_json_safe(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_pending(pending_path: str = PENDING_PATH) -> dict:
    return _load_json_safe(pending_path)


def _save_pending(data: dict, pending_path: str = PENDING_PATH) -> None:
    save_json(pending_path, data)


def _load_seen(seen_path: str = SEEN_PATH) -> dict:
    return _load_json_safe(seen_path)


def _save_seen(data: dict, seen_path: str = SEEN_PATH) -> None:
    save_json(seen_path, data)


def _filter_new_orphans(orphans: list, pending: dict, seen: dict) -> list:
    """pending中(審議中)・seenが恒久決着済みのfilepathを除外する。"""
    pending_paths = {v["filepath"] for v in pending.values()}
    return [
        o for o in orphans
        if o["filepath"] not in pending_paths
        and seen.get(o["filepath"], {}).get("decision") not in _PERMANENT_DECISIONS
    ]


def _record_no_candidate_attempt(seen: dict, filepath: str, now: datetime.datetime) -> None:
    """候補0件だったノートを記録する。

    mtime昇順で処理する都合上、これをしないと同じ「一生候補が出ない」ノートが
    毎週上位スロットを独占し続け、後続の救えるノートが浮上しなくなる。
    NO_CANDIDATE_RETRY_BUDGET 回連続で候補が出なければ恒久除外に昇格する。
    """
    prev = seen.get(filepath)
    attempts = prev.get("attempts", 0) + 1 if prev and prev.get("decision") == "no_candidates" else 1
    decision = "no_candidates_exhausted" if attempts >= NO_CANDIDATE_RETRY_BUDGET else "no_candidates"
    seen[filepath] = {"decision": decision, "at": now.isoformat(), "attempts": attempts}


def _read_note_body(filepath: str, work_dir: str = WORK_DIR,
                     max_chars: int = READ_NOTE_MAX_CHARS) -> str:
    """filepath は _compute_orphan_notes が返す形式 (Vault相対パス、例 "30_Main/xxx.md")。"""
    path = os.path.join(work_dir, filepath)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(max_chars)
    except (OSError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------
# Discord通知
# ---------------------------------------------------------

def _notify_orphan(slot: int, orphan: dict, candidates: list, notify_fn) -> None:
    """note_promotionの_notify_clusterと同型。承認/却下ボタン付きでDiscordへ通知する。"""
    from agent_scheduler import _button_row
    title = os.path.splitext(os.path.basename(orphan["filepath"]))[0]
    links = "\n".join(f"- [[{os.path.splitext(c['filename'])[0]}]]" for c in candidates)
    message = f"🔗 **[[{title}]]** に関連しそうなノートが見つかりました:\n{links}"
    if len(message) > 1900:
        message = message[:1900] + "…"
    components = _button_row([
        ("✅ 全部リンクする", f"orphan_relink_approve:{slot}"),
        ("❌ 却下", f"orphan_relink_reject:{slot}"),
    ])
    notify_fn(message, components)


def _default_notify(message: str, components=None) -> None:
    from agent_scheduler import send_discord_notification
    send_discord_notification(message, components=components)


# ---------------------------------------------------------
# 夜間ジョブ本体
# ---------------------------------------------------------

def run_weekly_orphan_relink_job(
    db_path: str = DB_PATH,
    pending_path: str = PENDING_PATH,
    seen_path: str = SEEN_PATH,
    work_dir: str = WORK_DIR,
    target_dirs: tuple = TARGET_DIRS,
    slots: int = ORPHAN_RELINK_SLOTS,
    top_k: int = CANDIDATE_TOP_K,
    find_related_fn=tool_find_related_notes,
    notify_fn=_default_notify,
) -> str:
    """夜間ジョブ本体。silentタスク前提 (agent_scheduler側の定型✅/❌通知を経由しない)。

    find_related_fn (tool_find_related_notes → _hybrid_hits → Gemini rerank) は
    generate_promotion_draft と違いcatch-allが無く、GeminiBudgetExceeded等の例外を
    投げうる。ループ内で個別にtry/exceptし、1件の失敗で既に通知済みの他slotの
    _save_pendingを巻き込まないようにする。
    """
    orphans = _compute_orphan_notes(db_path=db_path, target_dirs=target_dirs)
    if not orphans:
        return "Orphan Relink: 孤児ノートなし"

    with FileLock(pending_path, timeout=PENDING_LOCK_TIMEOUT):
        pending = _load_pending(pending_path)
        seen = _load_seen(seen_path)
        fresh = _filter_new_orphans(orphans, pending, seen)
        if not fresh:
            return "Orphan Relink: 新規孤児ノートなし (審議中/既知のみ)"

        used_slots = {int(k) for k in pending}
        free_slots = [i for i in range(slots) if i not in used_slots]
        to_process = fresh[: len(free_slots)]

        now = datetime.datetime.now()
        sent, search_failed, no_candidates = 0, 0, 0
        for orphan, slot in zip(to_process, free_slots):
            filepath = orphan["filepath"]
            body = _read_note_body(filepath, work_dir=work_dir)
            query = extract_note_query(body)

            try:
                candidates = find_related_fn(
                    query, exclude_filename=os.path.basename(filepath), top_k=top_k
                )
            except Exception as e:
                print(f"[OrphanRelink] ⚠️ 関連ノート検索に失敗: {filepath}: {e}")
                search_failed += 1
                continue

            if not candidates:
                no_candidates += 1
                _record_no_candidate_attempt(seen, filepath, now)
                continue

            pending[str(slot)] = {
                "filepath": filepath,
                "candidates": [c["filename"] for c in candidates],
            }
            _notify_orphan(slot, orphan, candidates, notify_fn)
            sent += 1

        _save_pending(pending, pending_path)
        _save_seen(seen, seen_path)

    remaining = len(fresh) - len(to_process)
    return (f"Orphan Relink: {sent}件通知 (候補なし{no_candidates}件, "
            f"検索失敗{search_failed}件, 翌週持ち越し{remaining}件)")


# ---------------------------------------------------------
# 承認 / 却下
# ---------------------------------------------------------

def tool_approve_orphan_relink(slot: int, pending_path: str = PENDING_PATH,
                                seen_path: str = SEEN_PATH,
                                confirm_fn=tool_confirm_related_notes) -> str:
    """承認ボタンから呼ぶ。pop確定をFileLockで保護し、以降の処理はロック外で行う
    (tool_promote_clusterと同じ判断: pop さえ確定すれば、他プロセスがこのslotを
    触ることは無い)。
    """
    with FileLock(pending_path, timeout=PENDING_LOCK_TIMEOUT):
        pending = _load_pending(pending_path)
        entry = pending.pop(str(slot), None)
        if entry is not None:
            _save_pending(pending, pending_path)
    if entry is None:
        return "Error: この提案は既に処理済みか見つかりません"

    filepath = entry["filepath"]
    result = confirm_fn(os.path.basename(filepath), entry["candidates"])
    if is_failure_result(result):
        return result  # seenに書かない = 次週また拾える

    with FileLock(seen_path, timeout=PENDING_LOCK_TIMEOUT):
        seen = _load_seen(seen_path)
        seen[filepath] = {"decision": "linked", "at": datetime.datetime.now().isoformat()}
        _save_seen(seen, seen_path)
    return result


def tool_reject_orphan_relink(slot: int, pending_path: str = PENDING_PATH,
                               seen_path: str = SEEN_PATH) -> str:
    """却下ボタンから呼ぶ。ファイルは一切変更せず、seenキャッシュに記録するのみ。"""
    with FileLock(pending_path, timeout=PENDING_LOCK_TIMEOUT):
        pending = _load_pending(pending_path)
        entry = pending.pop(str(slot), None)
        if entry is not None:
            _save_pending(pending, pending_path)
    if entry is None:
        return "Error: この提案は既に処理済みか見つかりません"

    with FileLock(seen_path, timeout=PENDING_LOCK_TIMEOUT):
        seen = _load_seen(seen_path)
        seen[entry["filepath"]] = {"decision": "rejected", "at": datetime.datetime.now().isoformat()}
        _save_seen(seen, seen_path)
    title = os.path.splitext(os.path.basename(entry["filepath"]))[0]
    return f"却下しました ([[{title}]])"


if __name__ == "__main__":
    # ponytail: 恒久除外ロジックの最小セルフチェック。フルテストは
    # tests/test_orphan_relink.py 側。
    _now = datetime.datetime.now()
    _orphans = [{"filepath": "30_Main/a.md", "mtime": 1.0}, {"filepath": "30_Main/b.md", "mtime": 2.0}]
    _pending = {"0": {"filepath": "30_Main/a.md"}}
    assert _filter_new_orphans(_orphans, _pending, {}) == [_orphans[1]], "pending中は除外"

    _seen_linked = {"30_Main/a.md": {"decision": "linked"}}
    assert _filter_new_orphans(_orphans, {}, _seen_linked) == [_orphans[1]], "linkedは恒久除外"

    _seen = {}
    for _ in range(NO_CANDIDATE_RETRY_BUDGET - 1):
        _record_no_candidate_attempt(_seen, "30_Main/a.md", _now)
    assert _seen["30_Main/a.md"]["decision"] == "no_candidates", "猶予中はno_candidates"
    _record_no_candidate_attempt(_seen, "30_Main/a.md", _now)
    assert _seen["30_Main/a.md"]["decision"] == "no_candidates_exhausted", \
        f"{NO_CANDIDATE_RETRY_BUDGET}回目で恒久除外に昇格すること"

    print("OK: tool_orphan_relink self-check passed")
