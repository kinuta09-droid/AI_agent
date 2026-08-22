# -*- coding: utf-8 -*-
"""tests/test_orphan_relink.py — 孤児ノート検出+関連ノートリンク提案の週次自動化

検証する不変条件:
  [1] SQL検出: target_dirs配下・被リンク0件のノートが返る
  [2] SQL検出: dst_pathが解決済み(NOT NULL)のノートは除外される
  [3] SQL検出 (回帰ガード): dst_path IS NULLの未解決リンク行が混在していても
      孤児件数が正しく数えられる (WHERE dst_path IS NOT NULLを外すとNOT INの
      三値論理で全行false評価になり孤児が常に0件になる、という無言のメトリクス
      破壊の回帰テスト)
  [4] SQL検出: target_dirs以外の配下は対象外
  [5] SQL検出: mtime昇順(最も古い=長く放置されたノートが先頭)で返る
  [6] _filter_new_orphans: pending中のfilepathは除外される
  [7] _filter_new_orphans: seenがlinked/rejected/no_candidates_exhaustedのいずれか
      なら恒久除外される
  [8] _filter_new_orphans: seenがno_candidatesかつattempts<予算内なら除外されず
      再提案対象に含まれる
  [9] _record_no_candidate_attempt: NO_CANDIDATE_RETRY_BUDGET回目でno_candidates_
      exhaustedに昇格する
  [10] run_weekly_orphan_relink_job: 孤児0件ならnotify_fnを一切呼ばず
       "孤児ノートなし"を返す
  [11] run_weekly_orphan_relink_job: 空きスロットを超えた分は通知されず、
       pending/seenにも入らない (remaining件数に反映)
  [12] run_weekly_orphan_relink_job: find_related_fnが空リストを返した孤児は
       通知されずseenにno_candidatesとして記録される
  [13] run_weekly_orphan_relink_job (重要な回帰): find_related_fnが特定の孤児で
       例外を投げても、それより前に処理が成功した孤児の通知・pending保存は
       失われない
  [14] tool_approve_orphan_relink: confirm_fnが成功結果を返したらseenにlinkedが
       記録され、pendingからslotが消える
  [15] tool_approve_orphan_relink: confirm_fnが失敗結果("Error: ...")を返したら
       seenに書かず、その失敗結果をそのまま返す
  [16] tool_reject_orphan_relink: ファイルは一切変更せずseenにrejectedのみ記録
  [17] 未知slot: 承認/却下ともに"Error: ..."を返す (二重クリック相当の冪等性)
  [18] 統合: _compute_orphan_notes → run_weekly_orphan_relink_job → tool_approve_
       orphan_relink を実際に繋いだ経路で通しで実行し、pending生成→承認→seen
       記録までの一連の状態遷移が壊れていないことを確認する

本番logs/vault_index.db・logs/orphan_relink_pending.json・logs/orphan_relink_seen.json
には一切触れない (tempfile配下のDB/JSONパスを関数の引数として渡すだけで完全に隔離できる
— このリポジトリが優先する「シグネチャ引数注入」方式)。tool_confirm_related_notes/
tool_find_related_notesは実物を呼ばず、confirm_fn/find_related_fn引数にダミー関数を
注入する (実Vault書き込み・実API呼び出しを避けるための依存性注入の作法)。
"""
import datetime
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tool_vault_index import _connect, SOURCE_VAULT  # noqa: E402
import tools.tool_orphan_relink as orph  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------
# DB構築ヘルパー
# ---------------------------------------------------------

def _insert_file(conn, filepath, mtime, source=SOURCE_VAULT):
    conn.execute(
        "INSERT INTO files(filepath, mtime, chunk_count, source) VALUES (?, ?, 1, ?)",
        (filepath, mtime, source),
    )


def _insert_link(conn, src_path, dst_name, dst_path):
    conn.execute(
        "INSERT INTO links(src_path, dst_name, dst_path) VALUES (?, ?, ?)",
        (src_path, dst_name, dst_path),
    )


def _make_db(db_path, files=(), links=()):
    """files: [(filepath, mtime, source), ...] / links: [(src, dst_name, dst_path), ...]"""
    conn = _connect(db_path)
    for entry in files:
        _insert_file(conn, *entry)
    for entry in links:
        _insert_link(conn, *entry)
    conn.commit()
    conn.close()


# ---------------------------------------------------------
# [1]-[5] _compute_orphan_notes (SQL検出)
# ---------------------------------------------------------

def test_compute_orphan_notes_finds_unlinked():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("30_Main/orphan.md", 1.0, SOURCE_VAULT),
            ("40_Report/linked.md", 2.0, SOURCE_VAULT),
        ], links=[("30_Main/other.md", "linked", "40_Report/linked.md")])
        orphans = orph._compute_orphan_notes(db_path=db)
        check("[1] 被リンク0件のノートが返る",
              [o["filepath"] for o in orphans] == ["30_Main/orphan.md"], orphans)
    print("--- [1] done ---")


def test_compute_orphan_notes_excludes_linked():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("30_Main/a.md", 1.0, SOURCE_VAULT),
            ("30_Main/b.md", 2.0, SOURCE_VAULT),
        ], links=[("30_Main/x.md", "a", "30_Main/a.md")])
        orphans = orph._compute_orphan_notes(db_path=db)
        check("[2] dst_path解決済みのノートは除外",
              [o["filepath"] for o in orphans] == ["30_Main/b.md"], orphans)
    print("--- [2] done ---")


def test_compute_orphan_notes_unresolved_link_does_not_zero_out_count():
    """dst_path IS NULL の未解決リンク行が混在していても孤児件数が0件に潰れないこと。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("30_Main/a.md", 1.0, SOURCE_VAULT),
            ("30_Main/b.md", 2.0, SOURCE_VAULT),
        ], links=[
            ("30_Main/x.md", "unresolved-target", None),  # 未解決リンク (dst_path NULL)
            ("30_Main/x.md", "a", "30_Main/a.md"),          # 解決済みリンク
        ])
        orphans = orph._compute_orphan_notes(db_path=db)
        check("[3] 未解決リンクが混在しても孤児件数が正しい (0件に潰れない回帰ガード)",
              [o["filepath"] for o in orphans] == ["30_Main/b.md"], orphans)
    print("--- [3] done ---")


def test_compute_orphan_notes_excludes_other_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("30_Main/a.md", 1.0, SOURCE_VAULT),
            ("00_System/b.md", 2.0, SOURCE_VAULT),
            ("50_Archive/c.md", 3.0, SOURCE_VAULT),
        ])
        orphans = orph._compute_orphan_notes(db_path=db)
        check("[4] target_dirs以外は対象外",
              [o["filepath"] for o in orphans] == ["30_Main/a.md"], orphans)
    print("--- [4] done ---")


def test_compute_orphan_notes_orders_by_mtime_asc():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        _make_db(db, files=[
            ("30_Main/new.md", 300.0, SOURCE_VAULT),
            ("40_Report/old.md", 100.0, SOURCE_VAULT),
            ("30_Main/mid.md", 200.0, SOURCE_VAULT),
        ])
        orphans = orph._compute_orphan_notes(db_path=db)
        check("[5] mtime昇順 (最も古いものが先頭)",
              [o["filepath"] for o in orphans] == ["40_Report/old.md", "30_Main/mid.md", "30_Main/new.md"],
              orphans)
    print("--- [5] done ---")


# ---------------------------------------------------------
# [6]-[8] _filter_new_orphans
# ---------------------------------------------------------

def test_filter_excludes_pending():
    orphans = [{"filepath": "30_Main/a.md", "mtime": 1.0}, {"filepath": "30_Main/b.md", "mtime": 2.0}]
    pending = {"0": {"filepath": "30_Main/a.md"}}
    fresh = orph._filter_new_orphans(orphans, pending, {})
    check("[6] pending中のfilepathは除外", fresh == [orphans[1]], fresh)


def test_filter_excludes_permanent_decisions():
    orphans = [
        {"filepath": "30_Main/a.md", "mtime": 1.0},
        {"filepath": "30_Main/b.md", "mtime": 2.0},
        {"filepath": "30_Main/c.md", "mtime": 3.0},
        {"filepath": "30_Main/d.md", "mtime": 4.0},
    ]
    seen = {
        "30_Main/a.md": {"decision": "linked"},
        "30_Main/b.md": {"decision": "rejected"},
        "30_Main/c.md": {"decision": "no_candidates_exhausted"},
    }
    fresh = orph._filter_new_orphans(orphans, {}, seen)
    check("[7] linked/rejected/no_candidates_exhaustedは恒久除外", fresh == [orphans[3]], fresh)


def test_filter_allows_no_candidates_within_budget():
    orphans = [{"filepath": "30_Main/a.md", "mtime": 1.0}]
    seen = {"30_Main/a.md": {"decision": "no_candidates", "attempts": orph.NO_CANDIDATE_RETRY_BUDGET - 1}}
    fresh = orph._filter_new_orphans(orphans, {}, seen)
    check("[8] 予算内のno_candidatesは再提案対象に含まれる", fresh == orphans, fresh)


# ---------------------------------------------------------
# [9] _record_no_candidate_attempt
# ---------------------------------------------------------

def test_record_no_candidate_attempt_escalates_at_budget():
    now = datetime.datetime.now()
    seen = {}
    for i in range(orph.NO_CANDIDATE_RETRY_BUDGET - 1):
        orph._record_no_candidate_attempt(seen, "30_Main/a.md", now)
        check(f"[9] {i + 1}回目はno_candidatesのまま",
              seen["30_Main/a.md"]["decision"] == "no_candidates", seen)
    orph._record_no_candidate_attempt(seen, "30_Main/a.md", now)
    check(f"[9] {orph.NO_CANDIDATE_RETRY_BUDGET}回目でno_candidates_exhaustedに昇格",
          seen["30_Main/a.md"]["decision"] == "no_candidates_exhausted", seen)
    print("--- [9] done ---")


# ---------------------------------------------------------
# run_weekly_orphan_relink_job まわりの共通ヘルパー
# ---------------------------------------------------------

def _make_vault_notes(root, filenames):
    for fn in filenames:
        path = os.path.join(root, fn)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {os.path.splitext(os.path.basename(fn))[0]}\n\n本文テキスト。\n")


def _fake_candidate(name):
    return {"filename": name}


# ---------------------------------------------------------
# [10]-[13] run_weekly_orphan_relink_job
# ---------------------------------------------------------

def test_run_job_silent_when_no_orphans():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")  # ファイルなし = 孤児0件
        notified = []
        result = orph.run_weekly_orphan_relink_job(
            db_path=db,
            pending_path=os.path.join(tmp, "pending.json"),
            seen_path=os.path.join(tmp, "seen.json"),
            work_dir=tmp,
            find_related_fn=lambda q, exclude_filename="", top_k=8: [_fake_candidate("x.md")],
            notify_fn=lambda msg, components=None: notified.append(msg),
        )
        check("[10] 孤児0件では通知しない", notified == [], notified)
        check("[10] 戻り値に孤児ノートなしと分かる文言", "孤児ノートなし" in result, result)
    print("--- [10] done ---")


def test_run_job_respects_free_slots():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        # ORPHAN_RELINK_SLOTS(5) を超える 6 件の孤児を用意する
        filenames = [f"30_Main/n{i}.md" for i in range(6)]
        _make_db(db, files=[(fn, float(i), SOURCE_VAULT) for i, fn in enumerate(filenames)])
        _make_vault_notes(tmp, filenames)

        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        notified = []
        result = orph.run_weekly_orphan_relink_job(
            db_path=db, pending_path=pending_path, seen_path=seen_path, work_dir=tmp,
            find_related_fn=lambda q, exclude_filename="", top_k=8: [_fake_candidate("related.md")],
            notify_fn=lambda msg, components=None: notified.append(msg),
        )
        check("[11] 通知は空きスロット数(5)まで", len(notified) == orph.ORPHAN_RELINK_SLOTS, len(notified))
        pending_after = orph._load_pending(pending_path)
        check("[11] pendingも空きスロット数まで", len(pending_after) == orph.ORPHAN_RELINK_SLOTS, pending_after)
        seen_after = orph._load_seen(seen_path)
        check("[11] 溢れた1件はseenにも入らない (翌週へ持ち越し)", seen_after == {}, seen_after)
        check("[11] 戻り値に翌週持ち越し1件が明記される", "翌週持ち越し1件" in result, result)
    print("--- [11] done ---")


def test_run_job_records_no_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        filenames = ["30_Main/a.md"]
        _make_db(db, files=[(filenames[0], 1.0, SOURCE_VAULT)])
        _make_vault_notes(tmp, filenames)

        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        notified = []
        result = orph.run_weekly_orphan_relink_job(
            db_path=db, pending_path=pending_path, seen_path=seen_path, work_dir=tmp,
            find_related_fn=lambda q, exclude_filename="", top_k=8: [],
            notify_fn=lambda msg, components=None: notified.append(msg),
        )
        check("[12] 候補0件では通知しない", notified == [], notified)
        seen_after = orph._load_seen(seen_path)
        check("[12] seenにno_candidatesとして記録される",
              seen_after.get("30_Main/a.md", {}).get("decision") == "no_candidates", seen_after)
        check("[12] 戻り値の候補なし件数が1件", "候補なし1件" in result, result)
    print("--- [12] done ---")


def test_run_job_survives_partial_search_failure():
    """find_related_fnが特定の孤児で例外を投げても、それより前に成功した孤児の
    通知・pending保存が失われないこと (重要な回帰ポイント)。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "index.db")
        filenames = ["30_Main/a.md", "30_Main/b.md", "30_Main/c.md"]
        _make_db(db, files=[(fn, float(i), SOURCE_VAULT) for i, fn in enumerate(filenames)])
        _make_vault_notes(tmp, filenames)

        def flaky_find(query, exclude_filename="", top_k=8):
            if exclude_filename == "b.md":
                raise RuntimeError("検索API失敗")
            return [_fake_candidate("related.md")]

        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        notified = []
        result = orph.run_weekly_orphan_relink_job(
            db_path=db, pending_path=pending_path, seen_path=seen_path, work_dir=tmp,
            find_related_fn=flaky_find,
            notify_fn=lambda msg, components=None: notified.append(msg),
        )
        check("[13] 例外の前後2件は通知される", len(notified) == 2, notified)
        pending_after = orph._load_pending(pending_path)
        saved_filepaths = {v["filepath"] for v in pending_after.values()}
        check("[13] a.md/c.mdはpendingに保存される",
              saved_filepaths == {"30_Main/a.md", "30_Main/c.md"}, pending_after)
        check("[13] b.mdはpendingに入らない (検索失敗)", "30_Main/b.md" not in saved_filepaths, pending_after)
        check("[13] 戻り値に検索失敗1件が明記される", "検索失敗1件" in result, result)
    print("--- [13] done ---")


# ---------------------------------------------------------
# [14]-[17] tool_approve_orphan_relink / tool_reject_orphan_relink
# ---------------------------------------------------------

def _save_pending_entry(pending_path, slot, filepath, candidates=("related.md",)):
    orph._save_pending({str(slot): {"filepath": filepath, "candidates": list(candidates)}}, pending_path)


def test_approve_success_records_linked_and_pops_pending():
    with tempfile.TemporaryDirectory() as tmp:
        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        _save_pending_entry(pending_path, 0, "30_Main/a.md")

        calls = []

        def fake_confirm(idea_filename, candidate_filenames):
            calls.append((idea_filename, candidate_filenames))
            return "Success: linked."

        result = orph.tool_approve_orphan_relink(0, pending_path=pending_path, seen_path=seen_path,
                                                   confirm_fn=fake_confirm)
        check("[14] confirm_fnが呼ばれる", calls == [("a.md", ["related.md"])], calls)
        check("[14] 結果に失敗接頭辞が無い", not result.startswith("Error"), result)
        seen_after = orph._load_seen(seen_path)
        check("[14] seenにlinkedが記録される", seen_after.get("30_Main/a.md", {}).get("decision") == "linked", seen_after)
        pending_after = orph._load_pending(pending_path)
        check("[14] pendingからslotが消える", "0" not in pending_after, pending_after)
    print("--- [14] done ---")


def test_approve_failure_does_not_record_seen():
    with tempfile.TemporaryDirectory() as tmp:
        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        _save_pending_entry(pending_path, 0, "30_Main/a.md")

        fake_confirm = lambda idea_filename, candidate_filenames: "Error: リンク挿入に失敗しました"
        result = orph.tool_approve_orphan_relink(0, pending_path=pending_path, seen_path=seen_path,
                                                   confirm_fn=fake_confirm)
        check("[15] 失敗結果がそのまま返る", result == "Error: リンク挿入に失敗しました", result)
        seen_after = orph._load_seen(seen_path)
        check("[15] seenには書かれない (次週また拾える)", seen_after == {}, seen_after)
    print("--- [15] done ---")


def test_reject_leaves_files_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        filenames = ["30_Main/a.md"]
        _make_vault_notes(tmp, filenames)
        before = open(os.path.join(tmp, filenames[0]), encoding="utf-8").read()

        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        _save_pending_entry(pending_path, 0, filenames[0])

        result = orph.tool_reject_orphan_relink(0, pending_path=pending_path, seen_path=seen_path)
        check("[16] 却下結果に失敗接頭辞が無い", not result.startswith("Error"), result)
        after = open(os.path.join(tmp, filenames[0]), encoding="utf-8").read()
        check("[16] ファイル内容は不変", before == after)
        seen_after = orph._load_seen(seen_path)
        check("[16] seenにrejectedが記録される", seen_after.get(filenames[0], {}).get("decision") == "rejected", seen_after)
        pending_after = orph._load_pending(pending_path)
        check("[16] pendingからslotが消える", "0" not in pending_after, pending_after)
    print("--- [16] done ---")


def test_unknown_slot_returns_error():
    with tempfile.TemporaryDirectory() as tmp:
        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        result_approve = orph.tool_approve_orphan_relink(
            0, pending_path=pending_path, seen_path=seen_path,
            confirm_fn=lambda idea_filename, candidate_filenames: "Success")
        result_reject = orph.tool_reject_orphan_relink(0, pending_path=pending_path, seen_path=seen_path)
        check("[17] 未知slotの承認はError", result_approve.startswith("Error"), result_approve)
        check("[17] 未知slotの却下はError", result_reject.startswith("Error"), result_reject)
    print("--- [17] done ---")


# ---------------------------------------------------------
# [18] 統合: _compute_orphan_notes → run_weekly_orphan_relink_job → tool_approve_orphan_relink
# ---------------------------------------------------------

def test_end_to_end_detect_to_approve():
    with tempfile.TemporaryDirectory() as root:
        filenames = ["30_Main/orphan.md"]
        _make_vault_notes(root, filenames)
        db = os.path.join(root, "index.db")
        _make_db(db, files=[(filenames[0], 1.0, SOURCE_VAULT)])

        pending_path = os.path.join(root, "pending.json")
        seen_path = os.path.join(root, "seen.json")

        job_result = orph.run_weekly_orphan_relink_job(
            db_path=db, pending_path=pending_path, seen_path=seen_path, work_dir=root,
            find_related_fn=lambda q, exclude_filename="", top_k=8: [_fake_candidate("related.md")],
            notify_fn=lambda msg, components=None: None,
        )
        check("[18] ジョブが1件通知したと報告する", "1件通知" in job_result, job_result)

        pending = orph._load_pending(pending_path)
        check("[18] pendingにfilepathが保存される",
              pending.get("0", {}).get("filepath") == "30_Main/orphan.md", pending)

        approve_calls = []

        def fake_confirm(idea_filename, candidate_filenames):
            approve_calls.append((idea_filename, candidate_filenames))
            return f"Success: [[orphan.md]] に {candidate_filenames} をリンクしました。"

        approve_result = orph.tool_approve_orphan_relink(
            0, pending_path=pending_path, seen_path=seen_path, confirm_fn=fake_confirm)
        check("[18] 実経路を通しても承認が失敗接頭辞にならない",
              not approve_result.startswith("Error"), approve_result)
        check("[18] confirm_fnにorphan.mdと候補が渡る",
              approve_calls == [("orphan.md", ["related.md"])], approve_calls)

        seen_after = orph._load_seen(seen_path)
        check("[18] seenにlinkedが記録される",
              seen_after.get("30_Main/orphan.md", {}).get("decision") == "linked", seen_after)
        pending_after = orph._load_pending(pending_path)
        check("[18] pendingからslotが消える", "0" not in pending_after, pending_after)
    print("--- [18] done ---")


if __name__ == "__main__":
    test_compute_orphan_notes_finds_unlinked()
    test_compute_orphan_notes_excludes_linked()
    test_compute_orphan_notes_unresolved_link_does_not_zero_out_count()
    test_compute_orphan_notes_excludes_other_dirs()
    test_compute_orphan_notes_orders_by_mtime_asc()
    test_filter_excludes_pending()
    test_filter_excludes_permanent_decisions()
    test_filter_allows_no_candidates_within_budget()
    test_record_no_candidate_attempt_escalates_at_budget()
    test_run_job_silent_when_no_orphans()
    test_run_job_respects_free_slots()
    test_run_job_records_no_candidates()
    test_run_job_survives_partial_search_failure()
    test_approve_success_records_linked_and_pops_pending()
    test_approve_failure_does_not_record_seen()
    test_reject_leaves_files_untouched()
    test_unknown_slot_returns_error()
    test_end_to_end_detect_to_approve()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for f in _FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    sys.exit(0)
