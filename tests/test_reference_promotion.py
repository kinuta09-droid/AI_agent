# -*- coding: utf-8 -*-
"""tests/test_reference_promotion.py — 教訓の自動昇格 (bug-pattern-archivist の Vault 版)

検証する不変条件:
  [1] _load_search_log: window内のエントリのみ読み込まれ、window外は除外される
  [2] _load_search_log: 壊れたJSON行はスキップして継続する
  [3] _count_note_hits: ヒット回数が正しく集計される
  [4] _compute_promotion_candidates: 閾値未満は候補に入らない
  [5] _compute_promotion_candidates: pending中のfilepathは除外される
  [6] _compute_promotion_candidates: promotedは恒久除外 (cooldownに関わらず)
  [7] _compute_promotion_candidates: rejected/not_hubはcooldown中のみ除外・cooldown後は
      再提案対象に戻る
  [8] _sanitize_judgement: 不正dict/promote=true+必須フィールド欠如はpromote=Falseに落ちる
  [8b] _build_hub_judgement_prompt: 既存MOC一覧がプロンプトに含まれる (grillレビュー指摘の
       回帰: 一覧を渡さないと「既存の汎用MOCと重複するならfalse」をGeminiが判断できない)
  [9] run_reference_promotion_job: 検索ログが無ければ通知せず終了する
  [10] run_reference_promotion_job: 空きスロットを超えた分は通知されずpending/seenにも
       入らない (翌週へ持ち越し)
  [11] run_reference_promotion_job: judge_fnがpromote=falseを返したノートはseenに
       not_hubとして記録される
  [12] run_reference_promotion_job (重要な回帰): judge_fnが特定のノートで例外を投げても、
       それより前に処理が成功したノートの通知・pending保存は失われない
  [13] tool_approve_reference_promotion: MOCノートが新設され、対象ノートのup:が
       新MOCへ張り替えられる
  [14] tool_approve_reference_promotion: pendingからslotが消え、seenにpromotedが記録される
  [15] tool_reject_reference_promotion: ファイルは一切変更せずseenにrejectedのみ記録
  [16] 未知slot: 承認/却下ともに"Error: ..."を返す (二重クリック相当の冪等性)
  [17] 統合: run_reference_promotion_job → tool_approve_reference_promotion を実際に
       繋いだ経路で通しで実行し、pending生成→承認→MOC新設→seen記録までの一連の
       状態遷移が壊れていないことを確認する

本番logs/vault_search_log.jsonl・logs/reference_promotion_pending.json・
logs/reference_promotion_seen.json・実Vaultには一切触れない (tempfile配下のパスを
関数の引数として渡すだけで完全に隔離できる — tests/test_orphan_relink.py と同じ
「シグネチャ引数注入」方式)。Gemini呼び出しは行わず judge_fn 引数にダミー関数を注入する。
"""
import datetime
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.tool_reference_promotion as refp  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------
# ログ書き込みヘルパー
# ---------------------------------------------------------

def _write_log(path: str, entries) -> None:
    """entries: [dict, ...] または生の文字列 (壊れた行を混ぜるため) の混在リスト。"""
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            if isinstance(e, str):
                f.write(e + "\n")
            else:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _make_vault_note(root, filepath, body="# タイトル\n\n本文テキスト。\n"):
    path = os.path.join(root, filepath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


# ---------------------------------------------------------
# [1]-[2] _load_search_log
# ---------------------------------------------------------

def test_load_search_log_filters_by_window():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "log.jsonl")
        now = datetime.datetime.now()
        _write_log(log_path, [
            {"at": now.isoformat(), "query": "q1", "hits": ["30_Main/a.md"]},
            {"at": (now - datetime.timedelta(days=40)).isoformat(), "query": "q2",
             "hits": ["30_Main/b.md"]},
        ])
        entries = refp._load_search_log(window_days=30, log_path=log_path)
        check("[1] window内の1件のみ読み込まれる",
              len(entries) == 1 and entries[0]["hits"] == ["30_Main/a.md"], entries)
    print("--- [1] done ---")


def test_load_search_log_skips_broken_lines():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "log.jsonl")
        now = datetime.datetime.now()
        _write_log(log_path, [
            "{not valid json",
            {"at": now.isoformat(), "hits": ["30_Main/a.md"]},
            "",
        ])
        entries = refp._load_search_log(window_days=30, log_path=log_path)
        check("[2] 壊れた行をスキップして続行する", len(entries) == 1, entries)
    print("--- [2] done ---")


# ---------------------------------------------------------
# [3] _count_note_hits
# ---------------------------------------------------------

def test_count_note_hits():
    entries = [
        {"hits": ["30_Main/a.md", "30_Main/b.md"]},
        {"hits": ["30_Main/a.md"]},
    ]
    counts = refp._count_note_hits(entries)
    check("[3] ヒット回数が正しく集計される",
          counts == {"30_Main/a.md": 2, "30_Main/b.md": 1}, counts)


# ---------------------------------------------------------
# [4]-[7] _compute_promotion_candidates
# ---------------------------------------------------------

def test_candidates_below_threshold_excluded():
    counts = {"30_Main/a.md": 3, "30_Main/b.md": 2}
    candidates = refp._compute_promotion_candidates(counts, {}, {}, threshold=3)
    check("[4] 閾値未満は候補に入らない", candidates == [("30_Main/a.md", 3)], candidates)


def test_candidates_exclude_pending():
    counts = {"30_Main/a.md": 5}
    pending = {"0": {"filepath": "30_Main/a.md"}}
    candidates = refp._compute_promotion_candidates(counts, pending, {})
    check("[5] pending中のfilepathは除外される", candidates == [], candidates)


def test_candidates_promoted_is_permanent():
    counts = {"30_Main/a.md": 10}
    now = datetime.datetime.now()
    seen = {"30_Main/a.md": {"decision": "promoted", "at": (now - datetime.timedelta(days=365)).isoformat()}}
    candidates = refp._compute_promotion_candidates(counts, {}, seen, now=now)
    check("[6] promoted済みはcooldownに関わらず恒久除外", candidates == [], candidates)


def test_candidates_rejected_cooldown():
    counts = {"30_Main/a.md": 10}
    now = datetime.datetime.now()
    seen_recent = {"30_Main/a.md": {"decision": "rejected", "at": now.isoformat()}}
    candidates_recent = refp._compute_promotion_candidates(counts, {}, seen_recent, now=now)
    check("[7] rejected直後はcooldown中で除外される", candidates_recent == [], candidates_recent)

    seen_old = {"30_Main/a.md": {"decision": "not_hub",
                                  "at": (now - datetime.timedelta(days=30)).isoformat()}}
    candidates_old = refp._compute_promotion_candidates(counts, {}, seen_old, now=now)
    check("[7] cooldown経過後は再提案対象に戻る",
          candidates_old == [("30_Main/a.md", 10)], candidates_old)


# ---------------------------------------------------------
# [8] _sanitize_judgement
# ---------------------------------------------------------

def test_sanitize_judgement_falls_back_to_false():
    check("[8] 非dictはpromote=Falseに落ちる",
          refp._sanitize_judgement("not a dict") == {"promote": False})
    check("[8] promote=falseはそのまま",
          refp._sanitize_judgement({"promote": False}) == {"promote": False})
    broken = refp._sanitize_judgement({"promote": True, "moc_title": "MOC_X"})  # moc_summary欠如
    check("[8] promote=true+必須フィールド欠如はFalseに落ちる",
          broken.get("promote") is False, broken)
    ok = refp._sanitize_judgement({"promote": True, "moc_title": "MOC_X", "moc_summary": "説明"})
    check("[8] 必須フィールドが揃っていればpromote=trueを維持", ok.get("promote") is True, ok)


def test_build_hub_judgement_prompt_includes_existing_mocs():
    """既存MOC一覧をプロンプトに含めること (grillレビュー指摘の回帰: 一覧を渡さないと
    「既存の汎用MOCと重複するならpromote=false」という指示をGeminiが実際には
    判断できない)。"""
    prompt = refp._build_hub_judgement_prompt(
        "タイトル", "本文", 5, existing_mocs=["MOC_AI", "MOC_Business"])
    check("[8b] 既存MOC名がプロンプトに含まれる",
          "MOC_AI" in prompt and "MOC_Business" in prompt, prompt)
    check("[8b] インジェクション対策タグが含まれる", "<existing_mocs>" in prompt, prompt)

    empty_prompt = refp._build_hub_judgement_prompt("タイトル", "本文", 5, existing_mocs=[])
    check("[8b] 既存MOCが空でも例外にならず「なし」で埋まる", "(なし)" in empty_prompt, empty_prompt)


# ---------------------------------------------------------
# [9]-[12] run_reference_promotion_job
# ---------------------------------------------------------

def test_run_job_silent_when_no_log():
    with tempfile.TemporaryDirectory() as tmp:
        result = refp.run_reference_promotion_job(
            log_path=os.path.join(tmp, "missing.jsonl"),
            pending_path=os.path.join(tmp, "pending.json"),
            seen_path=os.path.join(tmp, "seen.json"),
            work_dir=tmp,
            notify_fn=lambda msg, components=None: (_ for _ in ()).throw(AssertionError("呼ばれてはいけない")),
            moc_list_fn=lambda: [],
        )
        check("[9] 検索ログなしと分かる文言が返る", "検索ログなし" in result, result)
    print("--- [9] done ---")


def test_run_job_respects_free_slots():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "log.jsonl")
        now = datetime.datetime.now()
        filenames = [f"30_Main/n{i}.md" for i in range(6)]  # REFERENCE_PROMOTION_SLOTS(5)を超える
        for fn in filenames:
            _make_vault_note(tmp, fn)
        _write_log(log_path, [
            {"at": now.isoformat(), "hits": filenames * 3},  # 各3回ヒットで閾値クリア
        ])
        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        notified = []
        result = refp.run_reference_promotion_job(
            log_path=log_path, pending_path=pending_path, seen_path=seen_path, work_dir=tmp,
            judge_fn=lambda prompt: {"promote": True, "moc_title": "MOC_X", "moc_summary": "s"},
            notify_fn=lambda msg, components=None: notified.append(msg),
            moc_list_fn=lambda: [],
        )
        check("[10] 通知は空きスロット数(5)まで",
              len(notified) == refp.REFERENCE_PROMOTION_SLOTS, len(notified))
        pending_after = refp._load_pending(pending_path)
        check("[10] pendingも空きスロット数まで",
              len(pending_after) == refp.REFERENCE_PROMOTION_SLOTS, pending_after)
        check("[10] 戻り値に翌週持ち越し1件が明記される", "翌週持ち越し1件" in result, result)
    print("--- [10] done ---")


def test_run_job_records_not_hub():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "log.jsonl")
        now = datetime.datetime.now()
        _make_vault_note(tmp, "30_Main/a.md")
        _write_log(log_path, [{"at": now.isoformat(), "hits": ["30_Main/a.md"] * 3}])
        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        notified = []
        result = refp.run_reference_promotion_job(
            log_path=log_path, pending_path=pending_path, seen_path=seen_path, work_dir=tmp,
            judge_fn=lambda prompt: {"promote": False},
            notify_fn=lambda msg, components=None: notified.append(msg),
            moc_list_fn=lambda: [],
        )
        check("[11] promote=falseでは通知しない", notified == [], notified)
        seen_after = refp._load_seen(seen_path)
        check("[11] seenにnot_hubとして記録される",
              seen_after.get("30_Main/a.md", {}).get("decision") == "not_hub", seen_after)
        check("[11] 戻り値にハブ非該当1件が明記される", "ハブ非該当1件" in result, result)
    print("--- [11] done ---")


def test_run_job_survives_partial_judge_failure():
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "log.jsonl")
        now = datetime.datetime.now()
        filenames = ["30_Main/a.md", "30_Main/b.md", "30_Main/c.md"]
        for fn in filenames:
            _make_vault_note(tmp, fn)
        _write_log(log_path, [{"at": now.isoformat(), "hits": filenames * 3}])

        def flaky_judge(prompt):
            if "b" in prompt.split("タイトル: ")[1].split("\n")[0]:
                raise RuntimeError("Gemini呼び出し失敗")
            return {"promote": True, "moc_title": "MOC_X", "moc_summary": "s"}

        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        notified = []
        result = refp.run_reference_promotion_job(
            log_path=log_path, pending_path=pending_path, seen_path=seen_path, work_dir=tmp,
            judge_fn=flaky_judge,
            notify_fn=lambda msg, components=None: notified.append(msg),
            moc_list_fn=lambda: [],
        )
        check("[12] 例外の前後2件は通知される", len(notified) == 2, notified)
        pending_after = refp._load_pending(pending_path)
        saved = {v["filepath"] for v in pending_after.values()}
        check("[12] a.md/c.mdはpendingに保存される", saved == {"30_Main/a.md", "30_Main/c.md"}, pending_after)
        check("[12] b.mdはpendingに入らない (判定失敗)", "30_Main/b.md" not in saved, pending_after)
        check("[12] 戻り値にハブ非該当1件が明記される (失敗はpromote=false扱い)",
              "ハブ非該当1件" in result, result)
    print("--- [12] done ---")


# ---------------------------------------------------------
# [13]-[16] tool_approve_reference_promotion / tool_reject_reference_promotion
# ---------------------------------------------------------

def _save_pending_entry(pending_path, slot, filepath, judgement):
    refp._save_pending(
        {str(slot): {"filepath": filepath, "count": 5, "judgement": judgement}}, pending_path)


def test_approve_creates_moc_and_relinks_up():
    with tempfile.TemporaryDirectory() as tmp:
        filepath = "30_Main/a.md"
        note_path = _make_vault_note(tmp, filepath, body='---\ntags:\n  - type/permanent\nup: "[[00_Inbox]]"\n---\n\n# a\n本文\n')
        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        moc_dir = os.path.join(tmp, "10_MOCs")
        judgement = {"promote": True, "moc_title": "MOC_Test", "moc_summary": "テスト用MOC"}
        _save_pending_entry(pending_path, 0, filepath, judgement)

        result = refp.tool_approve_reference_promotion(
            0, pending_path=pending_path, seen_path=seen_path, work_dir=tmp, moc_dir=moc_dir)
        check("[13] 承認結果に失敗接頭辞が無い", not result.startswith("Error"), result)

        moc_path = os.path.join(moc_dir, "MOC_Test.md")
        check("[13] MOCノートが新設される", os.path.exists(moc_path), os.listdir(moc_dir))
        moc_body = open(moc_path, encoding="utf-8").read()
        check("[13] MOC本文にsummaryが含まれる", "テスト用MOC" in moc_body, moc_body)

        note_body_after = open(note_path, encoding="utf-8").read()
        check("[13] 対象ノートのup:が新MOCへ張り替えられる",
              'up: "[[MOC_Test]]"' in note_body_after, note_body_after)

        seen_after = refp._load_seen(seen_path)
        check("[14] seenにpromotedが記録される",
              seen_after.get(filepath, {}).get("decision") == "promoted", seen_after)
        pending_after = refp._load_pending(pending_path)
        check("[14] pendingからslotが消える", "0" not in pending_after, pending_after)
    print("--- [13][14] done ---")


def test_reject_leaves_files_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        filepath = "30_Main/a.md"
        note_path = _make_vault_note(tmp, filepath)
        before = open(note_path, encoding="utf-8").read()

        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        _save_pending_entry(pending_path, 0, filepath, {"promote": True, "moc_title": "MOC_X", "moc_summary": "s"})

        result = refp.tool_reject_reference_promotion(0, pending_path=pending_path, seen_path=seen_path)
        check("[15] 却下結果に失敗接頭辞が無い", not result.startswith("Error"), result)
        after = open(note_path, encoding="utf-8").read()
        check("[15] ファイル内容は不変", before == after)
        seen_after = refp._load_seen(seen_path)
        check("[15] seenにrejectedが記録される",
              seen_after.get(filepath, {}).get("decision") == "rejected", seen_after)
        pending_after = refp._load_pending(pending_path)
        check("[15] pendingからslotが消える", "0" not in pending_after, pending_after)
    print("--- [15] done ---")


def test_unknown_slot_returns_error():
    with tempfile.TemporaryDirectory() as tmp:
        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        result_approve = refp.tool_approve_reference_promotion(
            0, pending_path=pending_path, seen_path=seen_path, work_dir=tmp)
        result_reject = refp.tool_reject_reference_promotion(0, pending_path=pending_path, seen_path=seen_path)
        check("[16] 未知slotの承認はError", result_approve.startswith("Error"), result_approve)
        check("[16] 未知slotの却下はError", result_reject.startswith("Error"), result_reject)
    print("--- [16] done ---")


# ---------------------------------------------------------
# [17] 統合: run_reference_promotion_job → tool_approve_reference_promotion
# ---------------------------------------------------------

def test_end_to_end_detect_to_approve():
    with tempfile.TemporaryDirectory() as tmp:
        filepath = "30_Main/hub.md"
        note_path = _make_vault_note(tmp, filepath, body='---\ntags:\n  - type/permanent\nup: "[[00_Inbox]]"\n---\n\n# hub\n本文\n')
        log_path = os.path.join(tmp, "log.jsonl")
        now = datetime.datetime.now()
        _write_log(log_path, [{"at": now.isoformat(), "hits": [filepath] * 3}])

        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        moc_dir = os.path.join(tmp, "10_MOCs")

        job_result = refp.run_reference_promotion_job(
            log_path=log_path, pending_path=pending_path, seen_path=seen_path, work_dir=tmp,
            judge_fn=lambda prompt: {"promote": True, "moc_title": "MOC_Hub", "moc_summary": "ハブ"},
            notify_fn=lambda msg, components=None: None,
            moc_list_fn=lambda: [],
        )
        check("[17] ジョブが1件通知したと報告する", "1件通知" in job_result, job_result)

        pending = refp._load_pending(pending_path)
        check("[17] pendingにfilepathが保存される", pending.get("0", {}).get("filepath") == filepath, pending)

        approve_result = refp.tool_approve_reference_promotion(
            0, pending_path=pending_path, seen_path=seen_path, work_dir=tmp, moc_dir=moc_dir)
        check("[17] 実経路を通しても承認が失敗接頭辞にならない",
              not approve_result.startswith("Error"), approve_result)
        check("[17] MOCノートが実際に新設される",
              os.path.exists(os.path.join(moc_dir, "MOC_Hub.md")))

        note_body_after = open(note_path, encoding="utf-8").read()
        check("[17] 対象ノートのup:が新MOCへ張り替えられる",
              'up: "[[MOC_Hub]]"' in note_body_after, note_body_after)

        seen_after = refp._load_seen(seen_path)
        check("[17] seenにpromotedが記録される",
              seen_after.get(filepath, {}).get("decision") == "promoted", seen_after)
    print("--- [17] done ---")


if __name__ == "__main__":
    test_load_search_log_filters_by_window()
    test_load_search_log_skips_broken_lines()
    test_count_note_hits()
    test_candidates_below_threshold_excluded()
    test_candidates_exclude_pending()
    test_candidates_promoted_is_permanent()
    test_candidates_rejected_cooldown()
    test_sanitize_judgement_falls_back_to_false()
    test_build_hub_judgement_prompt_includes_existing_mocs()
    test_run_job_silent_when_no_log()
    test_run_job_respects_free_slots()
    test_run_job_records_not_hub()
    test_run_job_survives_partial_judge_failure()
    test_approve_creates_moc_and_relinks_up()
    test_reject_leaves_files_untouched()
    test_unknown_slot_returns_error()
    test_end_to_end_detect_to_approve()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for f in _FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    sys.exit(0)
