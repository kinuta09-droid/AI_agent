# -*- coding: utf-8 -*-
"""
lib/engagement_log の回帰テスト (実Vault不要、全てDI)

実行: py tests/test_engagement_log.py
"""
import os
import sys
import json
import tempfile
import shutil

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.engagement_log import (
    log_event, log_impressions, read_events, summarize,
    snapshot_open_files, diff_open_files, make_src_id, existing_src_ids,
)

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _write_workspace(vault_root: str, files: list):
    ws_dir = os.path.join(vault_root, ".obsidian")
    os.makedirs(ws_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"lastOpenFiles": files}, f, ensure_ascii=False)


# ---------------------------------------------------------
# 1. 追記 → 読み出しラウンドトリップ (UTF-8 日本語保持)
# ---------------------------------------------------------

def test_roundtrip():
    print("\n[1] 追記→読み出しラウンドトリップ")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "logs", "engagement.jsonl")
        ok = log_event("query", "介護業界のDXについて教えて", "discord",
                       meta={"command": "/agent"}, log_path=log_path)
        check("log_event が True を返す", ok is True, f"got {ok}")

        events = read_events(log_path=log_path)
        check("1件読み出せる", len(events) == 1, f"got {len(events)}")
        e = events[0]
        check("日本語が保持される", e["item"] == "介護業界のDXについて教えて", e.get("item"))
        check("item_type が既定で query", e["item_type"] == "query", e.get("item_type"))
        check("meta が保持される", e["meta"].get("command") == "/agent", str(e.get("meta")))
        check("ts が ISO 形式", len(e["ts"]) == 19 and e["ts"][10] == "T", e.get("ts"))

        # 生ファイルが1行1JSONであること
        with open(log_path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        check("JSONL が1行1レコード", len(lines) == 1 and json.loads(lines[0])["event"] == "query")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 2. 書き込み不能でも例外を出さない (本体を落とさない)
# ---------------------------------------------------------

def test_never_raises():
    print("\n[2] 書き込み不能でも例外を出さない")
    tmp = tempfile.mkdtemp()
    try:
        # 親ディレクトリの位置に「ファイル」を置いて makedirs を失敗させる
        blocker = os.path.join(tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        bad_path = os.path.join(blocker, "logs", "engagement.jsonl")

        try:
            ok = log_event("click", "topic", "curate", log_path=bad_path)
            check("例外を投げない", True)
            check("失敗時は False を返す", ok is False, f"got {ok}")
        except Exception as e:
            check("例外を投げない", False, f"{type(e).__name__}: {e}")

        try:
            gid = log_impressions(["a", "b"], "curate", log_path=bad_path)
            check("log_impressions も例外を投げない", isinstance(gid, str) and len(gid) == 8)
        except Exception as e:
            check("log_impressions も例外を投げない", False, f"{type(e).__name__}: {e}")

        check("読み出しも例外を投げない", read_events(log_path=bad_path) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 3. group_id で impression と click が突合できる
# ---------------------------------------------------------

def test_group_id_join():
    print("\n[3] group_id による impression↔click 突合")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")
        topics = [
            {"title": "トピックA", "branch": "consistent"},
            {"title": "トピックB", "branch": "bridge"},
            {"title": "トピックC", "branch": "entropy"},
        ]
        gid = log_impressions(topics, "curate", meta={"reject_count": 0}, log_path=log_path)
        check("group_id が8桁", len(gid) == 8, gid)

        log_event("click", "トピックC", "curate", group_id=gid,
                  meta={"branch": "entropy"}, log_path=log_path)

        events = read_events(log_path=log_path)
        same_group = [e for e in events if e["group_id"] == gid]
        check("同一 group_id が4件", len(same_group) == 4, f"got {len(same_group)}")

        imps = [e for e in same_group if e["event"] == "impression"]
        clicks = [e for e in same_group if e["event"] == "click"]
        check("impression 3件 / click 1件", len(imps) == 3 and len(clicks) == 1,
              f"{len(imps)}/{len(clicks)}")
        check("branch が meta に載る",
              {e["meta"].get("branch") for e in imps} == {"consistent", "bridge", "entropy"},
              str([e["meta"] for e in imps]))
        check("index が連番", [e["meta"]["index"] for e in imps] == [0, 1, 2])
        check("未選択の2件が特定できる",
              {e["item"] for e in imps} - {c["item"] for c in clicks} == {"トピックA", "トピックB"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 4. workspace.json スナップショットの差分検出
# ---------------------------------------------------------

def test_snapshot():
    print("\n[4] lastOpenFiles スナップショット差分")
    tmp = tempfile.mkdtemp()
    try:
        vault = os.path.join(tmp, "vault")
        log_path = os.path.join(tmp, "engagement.jsonl")
        state_path = os.path.join(tmp, "state.json")

        _write_workspace(vault, ["a.md", "b.md", "c.md"])
        snapshot_open_files(vault, log_path, state_path)
        check("初回は3件記録", len(read_events(log_path=log_path)) == 3,
              str(len(read_events(log_path=log_path))))

        snapshot_open_files(vault, log_path, state_path)
        check("2回目は増えない (重複しない)", len(read_events(log_path=log_path)) == 3,
              str(len(read_events(log_path=log_path))))

        _write_workspace(vault, ["d.md", "a.md", "b.md", "c.md"])
        snapshot_open_files(vault, log_path, state_path)
        events = read_events(log_path=log_path)
        check("新規ファイルで1件だけ増える", len(events) == 4, str(len(events)))
        check("増えたのは d.md", events[-1]["item"] == "d.md", events[-1]["item"])

        # 既存ファイルの再オープン (先頭入れ替えのみ) も検出する
        _write_workspace(vault, ["c.md", "d.md", "a.md", "b.md"])
        snapshot_open_files(vault, log_path, state_path)
        events = read_events(log_path=log_path)
        check("再オープンを検出する", len(events) == 5 and events[-1]["item"] == "c.md",
              f"{len(events)} / {events[-1]['item']}")

        # seed_only は状態だけ初期化しイベントを書かない
        seed_state = os.path.join(tmp, "seed_state.json")
        seed_log = os.path.join(tmp, "seed.jsonl")
        _write_workspace(vault, ["x.md", "y.md"])
        snapshot_open_files(vault, seed_log, seed_state, seed_only=True)
        check("seed_only はイベントを書かない", read_events(log_path=seed_log) == [])
        snapshot_open_files(vault, seed_log, seed_state)
        check("seed 後の初回スナップショットも0件",
              read_events(log_path=seed_log) == [],
              str(read_events(log_path=seed_log)))

        # 純粋関数の直接検証
        check("diff: 変化なしなら空", diff_open_files(["a", "b"], ["a", "b"]) == [])
        check("diff: workspace.json 欠損でも落ちない",
              "skipped" in snapshot_open_files(os.path.join(tmp, "nonexistent"),
                                               log_path, state_path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 5. summarize の CTR 計算
# ---------------------------------------------------------

def test_summarize():
    print("\n[5] summarize の集計")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")
        check("空ログでも落ちない", "エントリがありません" in summarize(log_path=log_path))

        gid = log_impressions(["A", "B", "C"], "curate", log_path=log_path)
        log_event("click", "C", "curate", group_id=gid, log_path=log_path)
        log_event("query", "BTCの見通し", "discord", log_path=log_path)
        log_event("query", "BTCの見通し", "discord", log_path=log_path)

        text = summarize(days=30, log_path=log_path)
        check("CTR が 1/3 = 33%", "1/3 = 33%" in text, text)
        check("イベント種別が出る", "impression: 3" in text, text)
        check("頻出クエリが出る", "(2) BTCの見通し" in text, text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 6. 古いイベントの days フィルタ / src_id
# ---------------------------------------------------------

def test_days_filter_and_src_id():
    print("\n[6] days フィルタと src_id")
    tmp = tempfile.mkdtemp()
    try:
        log_path = os.path.join(tmp, "engagement.jsonl")
        sid = make_src_id("chatlog", "古い質問", "2026-01-01T10:00:00")
        log_event("query", "古い質問", "chatlog", ts="2026-01-01T10:00:00",
                  backfill=True, src_id=sid, log_path=log_path)
        log_event("query", "新しい質問", "discord", log_path=log_path)

        check("全件では2件", len(read_events(log_path=log_path)) == 2)
        check("直近30日では1件", len(read_events(days=30, log_path=log_path)) == 1,
              str(read_events(days=30, log_path=log_path)))
        check("src_id が16桁", len(sid) == 16, sid)
        check("src_id は決定的",
              sid == make_src_id("chatlog", "古い質問", "2026-01-01T10:00:00"))
        check("existing_src_ids で回収できる",
              existing_src_ids(log_path=log_path) == {sid},
              str(existing_src_ids(log_path=log_path)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------
# 7. バックフィルの冪等性・パース精度
# ---------------------------------------------------------

CHATLOG_SAMPLE = """# 2026-07-24

## 🤖 Discord Command (00:44)
**Command**: /agent 介護業界のDXについて教えて
**Result**: 実行結果...
#log/mobile

## 🤖 Discord Command (01:12)
**Command**: /crypto BTC/USDT
**Result**: Success
#log/mobile

## 🤖 Discord Command (02:00)
**Command**: /curate -> 分散型システムの生物学的類推
**Result**: Success
#log/mobile

## 💬 Discord Chat (09:30)
**User**: 今日のニュースまとめて
**AI**: ...
"""

NOTE_SAMPLE = """---
tags:
  - type/permanent
  - source/curator
  - branch/entropy
created: 2026-02-16
---

# サンプル
"""


def test_backfill():
    print("\n[7] バックフィルの冪等性・パース")
    from scripts.backfill_engagement import backfill, collect_chatlog_events

    tmp = tempfile.mkdtemp()
    try:
        vault = os.path.join(tmp, "vault")
        chatlog_dir = os.path.join(vault, "50_Archive", "Chatlog")
        kb_dir = os.path.join(vault, "30_Main", "Knowledge_Base")
        os.makedirs(chatlog_dir)
        os.makedirs(kb_dir)
        with open(os.path.join(chatlog_dir, "2026-07-24.md"), "w", encoding="utf-8") as f:
            f.write(CHATLOG_SAMPLE)
        with open(os.path.join(kb_dir, "2026-02-16_サンプル.md"), "w", encoding="utf-8") as f:
            f.write(NOTE_SAMPLE)
        _write_workspace(vault, ["30_Main/Knowledge_Base/2026-02-16_サンプル.md", "01_Daily/x.md"])

        log_path = os.path.join(tmp, "engagement.jsonl")
        state_path = os.path.join(tmp, "state.json")

        # dry-run は書き込まない
        backfill(vault, log_path, state_path, dry_run=True)
        check("dry-run は書き込まない", read_events(log_path=log_path) == [])

        backfill(vault, log_path, state_path)
        events = read_events(log_path=log_path)
        queries = [e for e in events if e["event"] == "query"]
        notes = [e for e in events if e["event"] == "note_created"]
        opens = [e for e in events if e["event"] == "open"]

        check("query 3件 (/curate -> は除外)", len(queries) == 3,
              str([q["item"] for q in queries]))
        check("note_created 1件", len(notes) == 1, str(len(notes)))
        check("open seed 2件", len(opens) == 2, str(len(opens)))

        q = next(q for q in queries if q["item"].startswith("/agent"))
        check("Chatlog の時刻が ts に反映される", q["ts"] == "2026-07-24T00:44:00", q["ts"])
        check("command が meta に分離される", q["meta"]["command"] == "/agent", str(q["meta"]))
        check("backfill フラグが立つ", q.get("backfill") is True, str(q))

        n = notes[0]
        check("ノートは Vault相対スラッシュ区切り",
              n["item"] == "30_Main/Knowledge_Base/2026-02-16_サンプル.md", n["item"])
        check("frontmatter の created を使う", n["ts"] == "2026-02-16T00:00:00", n["ts"])
        check("branch タグを meta に保持", "branch/entropy" in n["meta"]["tags"], str(n["meta"]))
        check("open seed とノートの item が突合できる",
              n["item"] in {o["item"] for o in opens})

        # 2回目: 件数が増えない
        backfill(vault, log_path, state_path)
        check("2回目でも件数が増えない (冪等)",
              len(read_events(log_path=log_path)) == len(events),
              f"{len(read_events(log_path=log_path))} vs {len(events)}")

        # seed 済みなので直後のスナップショットは0件
        before = len(read_events(log_path=log_path))
        snapshot_open_files(vault, log_path, state_path)
        check("バックフィル直後のスナップショットは0件",
              len(read_events(log_path=log_path)) == before,
              str(len(read_events(log_path=log_path)) - before))

        check("Chatlog ディレクトリ不在でも落ちない",
              collect_chatlog_events(os.path.join(tmp, "nope")) == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print("=== test_engagement_log ===")
    test_roundtrip()
    test_never_raises()
    test_group_id_join()
    test_snapshot()
    test_summarize()
    test_days_filter_and_src_id()
    test_backfill()

    print(f"\n=== 結果: {len(_PASS)} passed / {len(_FAIL)} failed ===")
    if _FAIL:
        for f in _FAIL:
            print(f"  - {f}")
        sys.exit(1)
