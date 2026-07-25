# -*- coding: utf-8 -*-
"""
過去データから engagement.jsonl の初期シグナルを生成する (Phase 1 バックフィル)。

投入元:
  1. 50_Archive/Chatlog/*.md     → query        (自由入力 = 最強の顕示選好)
  2. 30_Main/Knowledge_Base/, 30_Main/*.md → note_created
  3. .obsidian/workspace.json の lastOpenFiles → open (seed)

冪等: 各エントリに src_id (source+item+ts の sha1) を持たせ、
既に投入済みの src_id はスキップする。何度実行しても件数は増えない。

実行:
  py scripts/backfill_engagement.py --dry-run   # 件数のみ表示
  py scripts/backfill_engagement.py             # 実投入
"""

import os
import re
import sys
import json
import datetime

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.engagement_log import (
    LOG_PATH, OPEN_STATE_PATH,
    build_entry, log_events, make_src_id, existing_src_ids,
    parse_command_meta, read_events, snapshot_open_files,
)

# 「## 🤖 Discord Command (00:44)」「## 💬 Discord Chat (01:12)」から時刻を拾う
_HEADING_TIME_RE = re.compile(r"^##\s+.*\((\d{2}):(\d{2})\)\s*$")
_COMMAND_RE = re.compile(r"^\*\*Command\*\*:\s*(.+)$")
_USER_RE = re.compile(r"^\*\*User\*\*:\s*(.+)$")
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_FRONTMATTER_CREATED_RE = re.compile(r"^created:\s*['\"]?(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_TAG_RE = re.compile(r"^\s*-\s*((?:type|source|branch)/[\w\-]+)", re.MULTILINE)

# 嗜好を含まない機械生成ノートは対象外 (Knowledge_Base 等の人手起点だけを見る)
NOTE_TARGETS = [
    ("30_Main/Knowledge_Base", True),   # (相対ディレクトリ, 存在すれば全 .md)
    ("30_Main", False),                 # False = 直下の .md のみ (サブフォルダは辿らない)
]


# ---------------------------------------------------------
# 1. Chatlog → query
# ---------------------------------------------------------

def collect_chatlog_events(chatlog_dir: str, cutoff_ts: str = "") -> list:
    """Chatlog の Command / User 行を query イベントに変換する。

    cutoff_ts 以降のエントリは除外する。ライブ記録 (source=discord) が
    始まった後の Chatlog を取り込むと、同じ操作が discord と chatlog の
    2件になるため。ライブ側は src_id を持たず突合できないので、
    「ライブが始まった時刻より前」だけをバックフィルの担当範囲とする。
    """
    entries = []
    if not os.path.isdir(chatlog_dir):
        return entries

    for fname in sorted(os.listdir(chatlog_dir)):
        if not fname.endswith(".md"):
            continue
        m = _DATE_PREFIX_RE.match(fname)
        if not m:
            continue
        date_str = m.group(1)

        try:
            with open(os.path.join(chatlog_dir, fname), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            continue

        current_time = "00:00"
        for line in lines:
            hm = _HEADING_TIME_RE.match(line)
            if hm:
                current_time = f"{hm.group(1)}:{hm.group(2)}"
                continue

            cm = _COMMAND_RE.match(line)
            um = _USER_RE.match(line)
            if not (cm or um):
                continue

            text = (cm or um).group(1).strip()
            if not text:
                continue
            # /curate -> トピック名 はエージェント自身の行動ログ (クリックは
            # click イベントで記録される)。ユーザーの自由入力ではないため除外。
            if text.startswith("/curate ->"):
                continue

            log_type = "command" if cm else "chat"
            ts = f"{date_str}T{current_time}:00"
            if cutoff_ts and ts >= cutoff_ts:  # ISO 文字列は辞書順=時系列順
                continue
            entries.append(build_entry(
                "query", text, "chatlog",
                # ライブ記録と同じ関数で meta を作る (二重実装を避ける)
                meta=parse_command_meta(text, log_type),
                backfill=True,
                ts=ts,
                src_id=make_src_id("chatlog", text, ts),
            ))
    return entries


# ---------------------------------------------------------
# 2. ノート → note_created
# ---------------------------------------------------------

def _note_ts(path: str, fname: str, content: str) -> str:
    """frontmatter created → ファイル名の日付 → mtime の順で作成日時を決める。"""
    m = _FRONTMATTER_CREATED_RE.search(content[:600])
    if m:
        return f"{m.group(1)}T00:00:00"
    m = _DATE_PREFIX_RE.match(fname)
    if m:
        return f"{m.group(1)}T00:00:00"
    return datetime.datetime.fromtimestamp(
        os.path.getmtime(path)).strftime("%Y-%m-%dT%H:%M:%S")


def collect_note_events(vault_root: str) -> list:
    """Knowledge_Base と 30_Main 直下のノートを note_created に変換する。"""
    entries = []
    for rel_dir, recursive in NOTE_TARGETS:
        abs_dir = os.path.join(vault_root, *rel_dir.split("/"))
        if not os.path.isdir(abs_dir):
            continue

        for root, dirs, files in os.walk(abs_dir):
            if not recursive and root != abs_dir:
                dirs[:] = []
                continue
            for fname in sorted(files):
                if not fname.endswith(".md"):
                    continue
                path = os.path.join(root, fname)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    continue

                # lastOpenFiles と同じ「Vault相対・スラッシュ区切り」に揃える
                # (Phase 2 で open イベントと突合するため)
                item = os.path.relpath(path, vault_root).replace("\\", "/")
                ts = _note_ts(path, fname, content)
                tags = _TAG_RE.findall(content[:1500])
                entries.append(build_entry(
                    "note_created", item, "vault",
                    meta={"tags": tags, "dir": rel_dir},
                    backfill=True,
                    ts=ts,
                    src_id=make_src_id("vault", item, ts),
                ))
            if not recursive:
                dirs[:] = []
    return entries


# ---------------------------------------------------------
# 3. lastOpenFiles → open (seed)
# ---------------------------------------------------------

def collect_open_seed(vault_root: str, log_path: str = LOG_PATH) -> list:
    """現在の lastOpenFiles(46件ローリング) を初回 open として取り込む。

    実際に開かれた時刻は残っていないため ts は現在時刻・approx_ts=true を付ける。

    既に open イベントがある場合は何もしない: 2回目以降のシードは、
    スケジューラが実測で記録した open と同じファイルを approx_ts で
    二重計上してしまう (src_id が無い実測イベントとは突合できないため)。
    """
    if any(e.get("event") == "open" for e in read_events(log_path=log_path)):
        return []

    ws_path = os.path.join(vault_root, ".obsidian", "workspace.json")
    try:
        with open(ws_path, encoding="utf-8") as f:
            files = json.load(f).get("lastOpenFiles", [])
    except Exception:
        return []

    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    entries = []
    for i, item in enumerate(files):
        entries.append(build_entry(
            "open", item, "obsidian",
            meta={"seed": True, "approx_ts": True, "mru_index": i,
                  "ext": os.path.splitext(item)[1]},
            backfill=True,
            ts=now,
            src_id=make_src_id("obsidian_seed", item, "seed"),
        ))
    return entries


# ---------------------------------------------------------
# Runner
# ---------------------------------------------------------

def _live_query_cutoff(log_path: str) -> str:
    """ライブ記録 (source=discord) の最初の query の時刻を分単位で返す。

    まだライブ記録が無ければ空文字 (= Chatlog を全期間バックフィルしてよい)。
    """
    live = [e["ts"] for e in read_events(log_path=log_path)
            if e.get("event") == "query" and e.get("source") == "discord"]
    if not live:
        return ""
    return min(live)[:16] + ":00"  # 分単位に丸める (Chatlog の解像度に合わせる)


def backfill(vault_root: str, log_path: str = LOG_PATH,
             state_path: str = OPEN_STATE_PATH, dry_run: bool = False) -> str:
    chatlog_dir = os.path.join(vault_root, "50_Archive", "Chatlog")
    cutoff = _live_query_cutoff(log_path)

    groups = [
        ("query (Chatlog)", collect_chatlog_events(chatlog_dir, cutoff)),
        ("note_created (Vault)", collect_note_events(vault_root)),
        ("open (lastOpenFiles seed)", collect_open_seed(vault_root, log_path)),
    ]

    known = existing_src_ids(log_path=log_path)
    lines = ["## Backfill" + (" (dry-run)" if dry_run else "")]
    if cutoff:
        lines.append(f"- ライブ記録開始 {cutoff} 以降の Chatlog は対象外 (二重計上防止)")
    to_write = []

    for label, entries in groups:
        fresh = [e for e in entries if e.get("src_id") not in known]
        skipped = len(entries) - len(fresh)
        to_write.extend(fresh)
        known.update(e["src_id"] for e in fresh)
        lines.append(f"- {label}: {len(fresh)}件 投入 (既存スキップ {skipped}件)")

    if dry_run:
        lines.append(f"\n合計 {len(to_write)}件 (dry-run のため書き込みなし)")
        return "\n".join(lines)

    ok = log_events(to_write, log_path=log_path)
    lines.append(f"\n合計 {len(to_write)}件 書き込み: {'OK' if ok else '失敗'}")

    # seed した46件が次回スナップショットで二重計上されないよう状態を初期化する
    seed_result = snapshot_open_files(vault_root, log_path, state_path, seed_only=True)
    lines.append(f"- {seed_result}")
    return "\n".join(lines)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    vault = None
    if "--vault" in sys.argv:
        vault = sys.argv[sys.argv.index("--vault") + 1]
    if vault is None:
        from lib.lib_shared import VAULT_ROOT
        vault = VAULT_ROOT
    print(backfill(vault, dry_run=dry))
