# -*- coding: utf-8 -*-
"""
Engagement Signal Log — パーソナライズ改善 Phase 1 (収集のみ)

「何を見せて・何を選び・何を開いたか」を append-only の JSONL に記録する。
学習・ランキングは行わない (Phase 2/3)。

設計上の重要な決定:
  1. 重み (クリック+3 等) はログに書かない。イベント種別と文脈だけを事実として残し、
     重みは EVENT_WEIGHTS としてコード側に置く。重みを再調整するたびに
     過去ログを書き直す羽目になるのを避けるため。
  2. impression (提示したが選ばれなかった) を必ず記録する。これ無しには
     CTR = click / impression が原理的に計算できず、Phase 2 の学習信号が作れない。
  3. ログ機構が本体を落とすことは絶対に無い (全ての例外を握りつぶす)。
     tools/tool_skill_observer.py と同じ方針。

公開 API:
  log_event(event, item, source, ...)        → bool (成功したか。呼び出し元は無視してよい)
  log_impressions(items, source, ...)        → group_id (後続の click と突合するため)
  snapshot_open_files(vault_root=None)       → 実行結果テキスト (スケジューラ用)
  read_events(days=None)                     → list[dict]
  summarize(days=30)                         → 集計テキスト (CLI 確認用)
  summarize_by_query(source, days=30)        → クエリ/分野別 CTR (項目9: 収集クエリ動的チューニングの手動判断用)

CLI:
  py lib/engagement_log.py            # 直近30日のサマリを表示
  py lib/engagement_log.py 90         # 直近90日
  py lib/engagement_log.py --by-query paper_radar 30   # paper_radar のクエリ別CTR
"""

import os
import sys
import json
import uuid
import hashlib
import datetime

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:  # `py lib/engagement_log.py` 直実行を許すため
    sys.path.insert(0, _PROJECT_ROOT)

from lib.file_lock import FileLock, FileLockTimeout

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

LOG_PATH = os.path.join(_PROJECT_ROOT, "logs", "engagement.jsonl")
OPEN_STATE_PATH = os.path.join(_PROJECT_ROOT, "logs", "engagement_open_state.json")

TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Phase 2 のスコアラが参照する重み表。本フェーズでは未使用 (ログには書かない)。
EVENT_WEIGHTS = {
    "query":        5,   # 自由入力 = 最も強い顕示選好
    "click":        3,   # 提示の中から選んだ
    "edit":         3,   # 人手でノートを編集した
    "note_created": 0,   # 作られただけでは嗜好の証拠にならない (open との差分で評価)
    "ab_answer":    1,
    "open":         1,   # Obsidian で実際に開いた
    "impression":  -1,   # 提示したが選ばれなかった
    "reject":      -2,   # 提示一式を明示的に蹴った
}

# 明示指定が無い場合の item_type
_DEFAULT_ITEM_TYPE = {
    "impression":   "topic",
    "click":        "topic",
    "reject":       "topic",
    "ab_answer":    "preference",
    "query":        "query",
    "open":         "note",
    "note_created": "note",
    "edit":         "note",
}


# ---------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------

def _now_ts() -> str:
    return datetime.datetime.now().strftime(TS_FORMAT)


def build_entry(event: str, item: str, source: str, item_type: str = "",
                group_id: str = "", meta: dict = None, backfill: bool = False,
                ts: str = "", src_id: str = "") -> dict:
    """1イベント分の dict を組み立てる (I/O はしない)。"""
    entry = {
        "ts":        ts or _now_ts(),
        "event":     event,
        "source":    source,
        "item":      str(item)[:500],
        "item_type": item_type or _DEFAULT_ITEM_TYPE.get(event, ""),
        "group_id":  group_id,
        "meta":      meta or {},
    }
    if backfill:
        entry["backfill"] = True
    if src_id:
        entry["src_id"] = src_id
    return entry


def _append_lines(entries: list, log_path: str) -> bool:
    """複数エントリを1回のロックでまとめて追記する。例外は外へ出さない。"""
    if not entries:
        return True
    try:
        directory = os.path.dirname(log_path)
        if directory:  # 相対ファイル名だけ渡された場合 makedirs("") は失敗する
            os.makedirs(directory, exist_ok=True)
        payload = "".join(
            json.dumps(e, ensure_ascii=False) + "\n" for e in entries
        )
        try:
            with FileLock(log_path, timeout=5.0):
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(payload)
        except FileLockTimeout:
            # ロックが取れなくてもシグナルは捨てない。小サイズの追記は
            # Windows CRT の "a" モードでも実質原子的に振る舞うため、
            # 「取りこぼす」より「稀に順序が乱れる」方が害が小さい。
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(payload)
        return True
    except Exception:
        return False  # ログ失敗で呼び出し元を止めない


def parse_command_meta(text: str, log_type: str = "command") -> dict:
    """Discord の入力文字列から query イベントの meta を組み立てる。

    ライブ記録 (bot_discord._log_to_daily_note) とバックフィル
    (scripts/backfill_engagement) の両方から呼ぶこと。ここが二重実装だと
    「同じ入力なのに meta が違う」データが混ざり、Phase 2 の学習が壊れる。
    """
    command, _, arg = text.partition(" ")
    is_slash = command.startswith("/")
    return {
        "log_type": log_type,
        "command": command if is_slash else "",
        # スラッシュコマンドは引数の有無で嗜好シグナルの強さが変わる
        # (/crypto BTC は具体的、/news は定型)。自由文は常に引数ありとみなす。
        "has_arg": bool(arg.strip()) if is_slash else True,
    }


def log_events(entries: list, log_path: str = LOG_PATH) -> bool:
    """build_entry() で組み立て済みのエントリ群を一括追記する (バックフィル用)。"""
    return _append_lines(entries, log_path)


def make_src_id(source: str, item: str, ts: str) -> str:
    """バックフィルの冪等性キー (source+item+ts の sha1 先頭16桁)。"""
    raw = f"{source}|{item}|{ts}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# ---------------------------------------------------------
# Public API — 書き込み
# ---------------------------------------------------------

def log_event(event: str, item: str, source: str, item_type: str = "",
              group_id: str = "", meta: dict = None, backfill: bool = False,
              ts: str = "", src_id: str = "", log_path: str = LOG_PATH) -> bool:
    """1イベントを追記する。失敗しても例外を出さず False を返す。"""
    entry = build_entry(event, item, source, item_type, group_id,
                        meta, backfill, ts, src_id)
    return _append_lines([entry], log_path)


def log_impressions(items: list, source: str, meta: dict = None,
                    item_type: str = "", log_path: str = LOG_PATH) -> str:
    """提示した候補群を同一 group_id で一括記録し、その group_id を返す。

    呼び出し元は返り値を保持し、後続の click/reject に同じ group_id を渡すこと。
    これにより「3件見せて1件選んだ (CTR 33%)」が復元できる。
    """
    group_id = uuid.uuid4().hex[:8]
    entries = []
    for i, item in enumerate(items):
        item_meta = dict(meta or {})
        item_meta["index"] = i
        if isinstance(item, dict):
            title = item.get("title") or item.get("feature_name", "")
            for k in ("branch", "source_note", "field", "query", "source_query"):
                if item.get(k):
                    item_meta[k] = item[k]
        else:
            title = item
        entries.append(build_entry(
            "impression", title, source, item_type, group_id, item_meta
        ))
    _append_lines(entries, log_path)
    return group_id


# ---------------------------------------------------------
# Public API — Obsidian 開閲スナップショット
# ---------------------------------------------------------

def _load_open_state(state_path: str) -> list:
    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("last_open_files", [])
        return files if isinstance(files, list) else []
    except Exception:
        return []


def _save_open_state(state_path: str, files: list) -> None:
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        tmp = f"{state_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"last_open_files": files, "updated": _now_ts()},
                f, ensure_ascii=False,
            )
        os.replace(tmp, state_path)  # 原子的に差し替え
    except Exception:
        pass


def diff_open_files(current: list, previous: list) -> list:
    """前回スナップショットとの差分から「開いた」とみなすファイルを返す。

    lastOpenFiles は MRU 順の46件ローリングリストなので、
      (a) 前回に無かったもの               → 新規オープン
      (b) 先頭が入れ替わった               → 既存ファイルの再オープン
    の2条件で判定する。(b) が無いと再オープンを丸ごと取りこぼす。
    """
    prev_set = set(previous)
    opened = [p for p in current if p not in prev_set]
    if current and current[0] not in opened:
        if not previous or previous[0] != current[0]:
            opened.insert(0, current[0])
    return opened


def snapshot_open_files(vault_root: str = None, log_path: str = LOG_PATH,
                        state_path: str = OPEN_STATE_PATH,
                        seed_only: bool = False) -> str:
    """.obsidian/workspace.json の lastOpenFiles を読み、差分を open として記録する。

    seed_only=True の場合はイベントを書かず状態ファイルだけ初期化する
    (バックフィル直後に46件を二重計上しないため)。
    """
    if vault_root is None:
        from lib.lib_shared import VAULT_ROOT  # 遅延 import (テストは DI で回避)
        vault_root = VAULT_ROOT

    workspace_path = os.path.join(vault_root, ".obsidian", "workspace.json")
    try:
        with open(workspace_path, encoding="utf-8") as f:
            workspace = json.load(f)
    except Exception as e:
        return f"Engagement Snapshot: skipped (workspace.json 読み込み失敗: {e})"

    current = workspace.get("lastOpenFiles", [])
    if not isinstance(current, list):
        return "Engagement Snapshot: skipped (lastOpenFiles が不正な形式)"

    current = [str(p) for p in current]  # workspace.json 側の型崩れで落ちないように
    previous = _load_open_state(state_path)
    opened = [] if seed_only else diff_open_files(current, previous)

    entries = [
        build_entry("open", p, "obsidian", meta={"ext": os.path.splitext(p)[1]})
        for p in opened
    ]
    if not _append_lines(entries, log_path):
        # 追記に失敗した状態で状態ファイルを進めると、この差分は永久に
        # 再検出されず取りこぼす。状態は据え置き、次回に再試行させる。
        return f"Engagement Snapshot: 追記に失敗 ({len(opened)}件) — 状態を更新せず次回再試行"

    _save_open_state(state_path, current)

    if seed_only:
        return f"Engagement Snapshot: state seeded ({len(current)} files)"
    return f"Engagement Snapshot: {len(opened)} new opens ({len(current)} tracked)"


# ---------------------------------------------------------
# Public API — 読み出し・集計
# ---------------------------------------------------------

def read_events(days: int = None, log_path: str = LOG_PATH) -> list:
    """JSONL を読み、(任意で) 直近 days 日に絞って返す。壊れた行は無視する。"""
    if not os.path.exists(log_path):
        return []

    cutoff = None
    if days:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)

    events = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cutoff:
                    try:
                        ts = datetime.datetime.strptime(entry.get("ts", ""), TS_FORMAT)
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                events.append(entry)
    except Exception:
        return events
    return events


def existing_src_ids(log_path: str = LOG_PATH) -> set:
    """既に投入済みの src_id 集合 (バックフィルの冪等性チェック用)。"""
    return {e["src_id"] for e in read_events(log_path=log_path) if e.get("src_id")}


def summarize(days: int = 30, log_path: str = LOG_PATH) -> str:
    """収集状況をテキストで返す (CLI 確認用。Discord 露出は Phase 2 の /taste)。"""
    events = read_events(days=days, log_path=log_path)
    if not events:
        return f"エンゲージメントログにエントリがありません ({log_path})"

    by_event = {}
    by_source = {}
    impressions = {}
    clicks = {}
    queries = {}
    opens = {}

    for e in events:
        ev = e.get("event", "?")
        src = e.get("source", "?")
        by_event[ev] = by_event.get(ev, 0) + 1
        by_source[src] = by_source.get(src, 0) + 1
        if ev == "impression":
            impressions[src] = impressions.get(src, 0) + 1
        elif ev == "click":
            clicks[src] = clicks.get(src, 0) + 1
        elif ev == "query":
            item = e.get("item", "")
            queries[item] = queries.get(item, 0) + 1
        elif ev == "open":
            item = e.get("item", "")
            opens[item] = opens.get(item, 0) + 1

    lines = [f"## Engagement Summary (直近{days}日 / {len(events)}件)\n"]

    lines.append("### イベント種別")
    for ev, n in sorted(by_event.items(), key=lambda x: -x[1]):
        lines.append(f"- {ev}: {n}")

    lines.append("\n### ソース別")
    for src, n in sorted(by_source.items(), key=lambda x: -x[1]):
        lines.append(f"- {src}: {n}")

    lines.append("\n### CTR (click / impression)")
    if impressions:
        for src, imp in sorted(impressions.items(), key=lambda x: -x[1]):
            clk = clicks.get(src, 0)
            lines.append(f"- {src}: {clk}/{imp} = {clk / imp * 100:.0f}%")
    else:
        lines.append("- (impression がまだありません)")

    if queries:
        lines.append("\n### 頻出クエリ TOP10")
        for item, n in sorted(queries.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"- ({n}) {item[:80]}")

    if opens:
        lines.append("\n### よく開くノート TOP10")
        for item, n in sorted(opens.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"- ({n}) {item[:80]}")

    return "\n".join(lines)


def _query_key(meta: dict) -> str:
    """meta からクエリ/分野識別子を取り出す (field=Paper Radar, query/source_query=XTrend)。"""
    return meta.get("field") or meta.get("query") or meta.get("source_query") or "(不明)"


def summarize_by_query(source: str, days: int = 30, log_path: str = LOG_PATH,
                        protected: set = frozenset()) -> str:
    """収集ツール1本について、クエリ/分野ごとの impression・click・CTR を集計する。

    項目9 (収集クエリの動的チューニング) の Phase 0: 自動反映はせず、
    月次で人間が読んで判断するためのレポートを返すだけ。

    protected: 意図的に低頻度で残しているクエリ/分野の集合 (例: Paper Radar の
    PROTECTED_FIELDS)。ここに循環 import を避けるため呼び出し元から渡してもらう
    (engagement_log は各収集ツールから import される共通基盤なので、
    逆方向に tool_paper_radar 等を import できない)。
    """
    events = [e for e in read_events(days=days, log_path=log_path) if e.get("source") == source]
    if not events:
        return f"## {source} クエリ別サマリ (直近{days}日)\n\nエントリがありません"

    impressions = {}
    clicks = {}
    for e in events:
        key = _query_key(e.get("meta", {}))
        if e.get("event") == "impression":
            impressions[key] = impressions.get(key, 0) + 1
        elif e.get("event") == "click":
            clicks[key] = clicks.get(key, 0) + 1

    lines = [f"## {source} クエリ別サマリ (直近{days}日)\n"]
    for key, imp in sorted(impressions.items(), key=lambda x: -x[1]):
        clk = clicks.get(key, 0)
        note = " ※意図的に低頻度で残している分野。件数だけで判断しないこと" if key in protected else ""
        lines.append(f"- {key}: {clk}/{imp} = {clk / imp * 100:.0f}% (impression {imp} / click {clk}){note}")
    # impression が無いのに click だけあるキー (index範囲外の手動指定等) も漏らさず出す
    for key, clk in clicks.items():
        if key not in impressions:
            lines.append(f"- {key}: click {clk} (impression 記録なし)")
    return "\n".join(lines)


if __name__ == "__main__":
    if "--by-query" in sys.argv:
        i = sys.argv.index("--by-query")
        _source = sys.argv[i + 1]
        _days = int(sys.argv[i + 2]) if len(sys.argv) > i + 2 else 30
        _protected = set()
        if _source == "paper_radar":
            try:
                from tools.tool_paper_radar import PROTECTED_FIELDS
                _protected = PROTECTED_FIELDS
            except Exception:
                pass
        print(summarize_by_query(_source, days=_days, protected=_protected))
    else:
        _days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
        print(summarize(days=_days))
