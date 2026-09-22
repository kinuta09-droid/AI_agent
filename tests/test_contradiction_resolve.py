# -*- coding: utf-8 -*-
"""tests/test_contradiction_resolve.py — 矛盾の能動解決ワークフロー

対象:
  - lib/lib_obsidian_utils.py: build_contradiction_names_and_links
  - tools/tool_obsidian.py: _set_frontmatter_field
  - tools/tool_contradiction_resolve.py: queue_contradiction / tool_resolve_contradiction /
    _mark_contradiction_resolved

検証する不変条件:
  [1] queue_contradiction: related_note が実ファイルに解決できればpendingに登録しnotify_fnを呼ぶ
  [2] queue_contradiction: related_note が解決できなければ何もしない (notify_fn未呼び出し)
  [3] queue_contradiction: 空きスロットが無ければ何もしない (notify_fn未呼び出し)
  [4] tool_resolve_contradiction: verdict="a"/"b"/"both" で両ノートのfrontmatter/本文追記が
      期待通りになる
  [5] tool_resolve_contradiction: 不正なverdictは "Error: ..." を返す
  [6] tool_resolve_contradiction: 存在しないslotへの裁定は "Error: ..." を返す (冪等性)
  [7] tool_resolve_contradiction: 裁定後、pendingからslotが削除される
  [8] _mark_contradiction_resolved: 該当行が[x]に書き換わり裁定結果が追記される。
      複数矛盾が記録されたファイルで他行に影響しない
  [9] _set_frontmatter_field: 既存フィールド上書き・新規フィールド追加・frontmatter無しノートは
      何もしない、の3パターン

本番Vault (D:\\マイドライブ\\Obsidian) と logs/contradiction_pending.json には一切触れない
(tempfile 配下のVault/pendingパスを関数の引数として渡す、または該当モジュールの定数を
一時パスへ差し替えて隔離する — Category C の教訓通り、共有可変資源は必ず退避する)。

実行: py tests/test_contradiction_resolve.py
"""
import os
import re
import shutil
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.lib_shared as _ls  # noqa: E402
from lib.lib_obsidian_utils import build_contradiction_names_and_links  # noqa: E402
import tools.tool_obsidian as tobs  # noqa: E402
import tools.tool_contradiction_resolve as tcr  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------
# 一時Vault (find_file_in_vault/tool_update_note/_set_frontmatter_field の隔離)
# ---------------------------------------------------------

class _Vault:
    """一時Vaultを作り、lib_shared.WORK_DIR/VAULT_ROOT・tobs.WORK_DIR・
    tobs.run_obsidian_cli(本物はObsidianを起動する)を差し替える
    (tests/test_create_note.py の _Vault と同じ隔離パターン)。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="contradiction_vault_")
        self.cli_calls = []

    def __enter__(self):
        self._saved_tobs_work_dir = tobs.WORK_DIR
        self._saved_tobs_vault_root = tobs.VAULT_ROOT
        self._saved_tobs_cli = tobs.run_obsidian_cli
        self._saved_ls_work_dir = _ls.WORK_DIR
        self._saved_ls_vault_root = _ls.VAULT_ROOT
        tobs.WORK_DIR = self.root
        # tools.tool_obsidian は `from lib.lib_shared import VAULT_ROOT` で値をコピー
        # 束縛しているため、_ls.VAULT_ROOT だけ書き換えても tool_update_note 内の
        # os.path.relpath(path, VAULT_ROOT) は古い(本番)VAULT_ROOTを見続け、
        # tempfile(Cドライブ)と本番Vault(Dドライブ)のクロスドライブ relpath エラーで
        # run_obsidian_cli 呼び出し自体がすり抜ける (実害はログ警告のみだが、
        # _fake_cli が本当に使われているかテストが保証できなくなる)。tobs.VAULT_ROOT
        # も揃えて差し替える。
        tobs.VAULT_ROOT = self.root
        _ls.WORK_DIR = self.root
        _ls.VAULT_ROOT = self.root

        def _fake_cli(args, **kw):
            self.cli_calls.append(list(args))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        tobs.run_obsidian_cli = _fake_cli
        return self

    def __exit__(self, *exc):
        tobs.WORK_DIR = self._saved_tobs_work_dir
        tobs.VAULT_ROOT = self._saved_tobs_vault_root
        tobs.run_obsidian_cli = self._saved_tobs_cli
        _ls.WORK_DIR = self._saved_ls_work_dir
        _ls.VAULT_ROOT = self._saved_ls_vault_root
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    def write_note(self, relpath: str, frontmatter: str = "tags:\n  - type/permanent",
                    body: str = "本文") -> str:
        path = os.path.join(self.root, *relpath.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\n{frontmatter}\n---\n# タイトル\n\n{body}\n")
        return path

    def read(self, relpath: str) -> str:
        path = os.path.join(self.root, *relpath.split("/"))
        with open(path, encoding="utf-8") as f:
            return f.read()


def _field(content: str, field: str) -> str:
    m = re.search(rf'^{field}:\s*(.*)$', content, re.MULTILINE)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------
# [1]-[3] queue_contradiction
# ---------------------------------------------------------

def test_queue_contradiction_registers_pending_and_notifies():
    with _Vault() as v:
        # own側は relpath (拡張子込みで own_name の元になる) をそのまま vault 直下に置く。
        # own_name は relpath 由来でフォルダを含みうる (実運用の check_vault_insights は
        # "00_inbox/xxx.md" 形式の relpath をそのまま渡す) が、ここでは直下配置で単純化する。
        v.write_note("NoteA.md")
        v.write_note("NoteB.md")

        pending_path = os.path.join(v.root, "pending.json")
        notified = []
        tcr.queue_contradiction(
            "NoteA.md", "NoteA",
            {"related_note": "NoteB.md", "insight": "AとBが矛盾しています"},
            pending_path=pending_path,
            notify_fn=lambda message, components=None: notified.append((message, components)))

        pending = tcr._load_pending(pending_path)
        check("[1] pendingにslot0が登録される", "0" in pending, pending)
        entry = pending.get("0", {})
        check("[1] own_name/related_nameが正しい",
              (entry.get("own_name"), entry.get("related_name")) == ("NoteA", "NoteB"), entry)
        check("[1] own_link/related_linkがwikilink形式",
              (entry.get("own_link"), entry.get("related_link")) == ("[[NoteA]]", "[[NoteB]]"), entry)
        check("[1] insight/timestampが保存される",
              entry.get("insight") == "AとBが矛盾しています" and bool(entry.get("timestamp")), entry)
        check("[1] notify_fnが1回呼ばれる", len(notified) == 1, notified)
        if notified:
            message, components = notified[0]
            check("[1] メッセージにown_link/related_linkが含まれる",
                  "[[NoteA]]" in message and "[[NoteB]]" in message, message)
            custom_ids = [c["custom_id"] for row in components for c in row["components"]]
            check("[1] 3つの裁定ボタン(a/b/both)がslot0向けに揃う",
                  custom_ids == ["contra_resolve:0:a", "contra_resolve:0:b", "contra_resolve:0:both"],
                  custom_ids)
    print("--- [1] done ---")


def test_queue_contradiction_skips_when_related_note_unresolvable():
    with _Vault() as v:
        v.write_note("NoteA.md")
        # NoteB.md は作らない = 実ファイルに解決できない

        pending_path = os.path.join(v.root, "pending.json")
        notified = []
        tcr.queue_contradiction(
            "NoteA.md", "NoteA",
            {"related_note": "NoteB.md", "insight": "解決できない矛盾"},
            pending_path=pending_path,
            notify_fn=lambda message, components=None: notified.append(message))

        check("[2] pendingに何も登録されない", tcr._load_pending(pending_path) == {}, pending_path)
        check("[2] notify_fnが呼ばれない", notified == [], notified)
    print("--- [2] done ---")


def test_queue_contradiction_skips_when_slots_full():
    with _Vault() as v:
        v.write_note("NoteA.md")
        v.write_note("NoteB.md")

        pending_path = os.path.join(v.root, "pending.json")
        full = {str(i): {"own_name": f"X{i}", "related_name": f"Y{i}", "own_link": f"[[X{i}]]",
                          "related_link": f"[[Y{i}]]", "insight": "既存", "timestamp": "t"}
                for i in range(tcr.CONTRADICTION_SLOTS)}
        tcr._save_pending(full, pending_path)

        notified = []
        tcr.queue_contradiction(
            "NoteA.md", "NoteA",
            {"related_note": "NoteB.md", "insight": "新しい矛盾"},
            pending_path=pending_path,
            notify_fn=lambda message, components=None: notified.append(message))

        pending_after = tcr._load_pending(pending_path)
        check("[3] スロットが満杯なら新規登録されない",
              pending_after == full, pending_after)
        check("[3] notify_fnが呼ばれない", notified == [], notified)
    print("--- [3] done ---")


# ---------------------------------------------------------
# [4]-[7] tool_resolve_contradiction
# ---------------------------------------------------------

def _seed_pending_pair(v: _Vault, pending_path: str, slot: str = "0"):
    v.write_note("30_Main/NoteA.md", body="Aの本文")
    v.write_note("30_Main/NoteB.md", body="Bの本文")
    entry = {
        "own_name": "NoteA", "related_name": "NoteB",
        "own_link": "[[NoteA]]", "related_link": "[[NoteB]]",
        "insight": "AとBが矛盾しています", "timestamp": "2026-08-22T10:00:00",
    }
    tcr._save_pending({slot: entry}, pending_path)
    return entry


def test_resolve_contradiction_verdict_a_marks_own_adopted():
    with _Vault() as v:
        pending_path = os.path.join(v.root, "pending.json")
        _seed_pending_pair(v, pending_path)

        result = tcr.tool_resolve_contradiction(0, "a", pending_path=pending_path)
        check("[4a] 成功結果に失敗接頭辞が無い", not result.startswith("Error"), result)

        note_a = v.read("30_Main/NoteA.md")
        note_b = v.read("30_Main/NoteB.md")
        check("[4a] own(NoteA)がadopted", _field(note_a, "contradiction_verdict") == "adopted", note_a)
        check("[4a] related(NoteB)がsuperseded", _field(note_b, "contradiction_verdict") == "superseded", note_b)
        check("[4a] 両ノートともstatus=resolved",
              _field(note_a, "contradiction_status") == "resolved"
              and _field(note_b, "contradiction_status") == "resolved", (note_a, note_b))
        check("[4a] 両ノートにcontradiction_resolved_atが付く",
              bool(_field(note_a, "contradiction_resolved_at"))
              and bool(_field(note_b, "contradiction_resolved_at")), (note_a, note_b))
        check("[4a] adopted側(NoteA)本文に優先した旨が追記される",
              "この内容を優先しました" in note_a, note_a)
        check("[4a] superseded側(NoteB)本文に優先された旨が追記される",
              "の内容が優先されました" in note_b, note_b)
    print("--- [4a] done ---")


def test_resolve_contradiction_verdict_b_marks_related_adopted():
    with _Vault() as v:
        pending_path = os.path.join(v.root, "pending.json")
        _seed_pending_pair(v, pending_path)

        result = tcr.tool_resolve_contradiction(0, "b", pending_path=pending_path)
        check("[4b] 成功結果に失敗接頭辞が無い", not result.startswith("Error"), result)

        note_a = v.read("30_Main/NoteA.md")
        note_b = v.read("30_Main/NoteB.md")
        check("[4b] own(NoteA)がsuperseded (b裁定はrelatedが採用側)",
              _field(note_a, "contradiction_verdict") == "superseded", note_a)
        check("[4b] related(NoteB)がadopted", _field(note_b, "contradiction_verdict") == "adopted", note_b)
        check("[4b] adopted側(NoteB)本文に優先した旨が追記される",
              "この内容を優先しました" in note_b, note_b)
        check("[4b] superseded側(NoteA)本文に優先された旨が追記される",
              "の内容が優先されました" in note_a, note_a)
    print("--- [4b] done ---")


def test_resolve_contradiction_verdict_both_marks_both():
    with _Vault() as v:
        pending_path = os.path.join(v.root, "pending.json")
        _seed_pending_pair(v, pending_path)

        result = tcr.tool_resolve_contradiction(0, "both", pending_path=pending_path)
        check("[4c] 成功結果に失敗接頭辞が無い", not result.startswith("Error"), result)

        note_a = v.read("30_Main/NoteA.md")
        note_b = v.read("30_Main/NoteB.md")
        check("[4c] 両ノートともverdict=both",
              _field(note_a, "contradiction_verdict") == "both"
              and _field(note_b, "contradiction_verdict") == "both", (note_a, note_b))
        check("[4c] 両ノート本文に両論併記の旨が追記される",
              "両論併記として整理" in note_a and "両論併記として整理" in note_b, (note_a, note_b))
    print("--- [4c] done ---")


def test_resolve_contradiction_invalid_verdict_returns_error():
    with _Vault() as v:
        pending_path = os.path.join(v.root, "pending.json")
        _seed_pending_pair(v, pending_path)

        result = tcr.tool_resolve_contradiction(0, "c", pending_path=pending_path)
        check("[5] 不正なverdictはErrorを返す", result.startswith("Error"), result)
        check("[5] pendingは変更されない (popされない)", "0" in tcr._load_pending(pending_path),
              tcr._load_pending(pending_path))
    print("--- [5] done ---")


def test_resolve_contradiction_unknown_slot_returns_error():
    with _Vault() as v:
        pending_path = os.path.join(v.root, "pending.json")  # 何も登録しない
        result = tcr.tool_resolve_contradiction(0, "a", pending_path=pending_path)
        check("[6] 未知slotの裁定はErrorを返す (二重クリック対策)", result.startswith("Error"), result)
    print("--- [6] done ---")


def test_resolve_contradiction_removes_slot_after_success():
    with _Vault() as v:
        pending_path = os.path.join(v.root, "pending.json")
        _seed_pending_pair(v, pending_path)

        tcr.tool_resolve_contradiction(0, "a", pending_path=pending_path)
        pending_after = tcr._load_pending(pending_path)
        check("[7] 裁定後、pendingからslot0が削除される", "0" not in pending_after, pending_after)

        # 二重クリック相当 (同じslotへ再度裁定) はErrorになること
        result_again = tcr.tool_resolve_contradiction(0, "a", pending_path=pending_path)
        check("[7] 同slotへの再裁定はError (冪等)", result_again.startswith("Error"), result_again)
    print("--- [7] done ---")


# ---------------------------------------------------------
# [8] _mark_contradiction_resolved
# ---------------------------------------------------------

def test_mark_contradiction_resolved_rewrites_only_matching_line():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "Vault_Contradictions.md")
        content = (
            "# Vault Contradictions\n\n"
            "- [ ] resolved | 2026-08-20T09:00:00 | [[X]] ⇔ [[Y]]\n"
            "  Xと Yが矛盾\n"
            "- [ ] resolved | 2026-08-22T10:00:00 | [[NoteA]] ⇔ [[NoteB]]\n"
            "  AとBが矛盾しています\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        tcr._mark_contradiction_resolved(
            "[[NoteA]]", "[[NoteB]]", "2026-08-22T10:00:00", "both", "2026-08-22T11:00:00",
            contradictions_path=path)

        with open(path, encoding="utf-8") as f:
            updated = f.read()
        check("[8] 対象行が[x]に書き換わる",
              "- [x] resolved | 2026-08-22T10:00:00 | [[NoteA]] ⇔ [[NoteB]]" in updated, updated)
        check("[8] 裁定結果 (両論併記) が追記される", "→ 裁定: 両論併記 (2026-08-22T11:00:00)" in updated, updated)
        check("[8] 無関係な行 ([[X]] ⇔ [[Y]]) はそのまま残る",
              "- [ ] resolved | 2026-08-20T09:00:00 | [[X]] ⇔ [[Y]]" in updated, updated)
        check("[8] 無関係な行の所見も残る", "Xと Yが矛盾" in updated, updated)
    print("--- [8] done ---")


def test_mark_contradiction_resolved_no_match_leaves_file_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "Vault_Contradictions.md")
        content = "# Vault Contradictions\n\n- [ ] resolved | 2026-08-22T10:00:00 | [[X]] ⇔ [[Y]]\n  所見\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        # detected_at が一致しないので該当行なし → 静かに何もしない
        tcr._mark_contradiction_resolved(
            "[[NoteA]]", "[[NoteB]]", "2026-08-22T10:00:00", "a", "2026-08-22T11:00:00",
            contradictions_path=path)
        with open(path, encoding="utf-8") as f:
            after = f.read()
        check("[8b] 該当行が無ければファイルは無変更", after == content, after)
    print("--- [8b] done ---")


def test_resolve_contradiction_updates_contradictions_note_via_full_path():
    """tool_resolve_contradiction を通しても Vault_Contradictions.md への反映が
    実際に起きること (tcr.CONTRADICTIONS_NOTE を一時パスへ差し替えて確認)。"""
    with _Vault() as v:
        pending_path = os.path.join(v.root, "pending.json")
        _seed_pending_pair(v, pending_path)

        contradictions_path = os.path.join(v.root, "00_System", "Vault_Contradictions.md")
        os.makedirs(os.path.dirname(contradictions_path), exist_ok=True)
        with open(contradictions_path, "w", encoding="utf-8") as f:
            f.write("# Vault Contradictions\n\n"
                     "- [ ] resolved | 2026-08-22T10:00:00 | [[NoteA]] ⇔ [[NoteB]]\n"
                     "  AとBが矛盾しています\n")

        saved_note = tcr.CONTRADICTIONS_NOTE
        tcr.CONTRADICTIONS_NOTE = contradictions_path
        try:
            result = tcr.tool_resolve_contradiction(0, "a", pending_path=pending_path)
        finally:
            tcr.CONTRADICTIONS_NOTE = saved_note

        check("[8c] 裁定結果に失敗接頭辞が無い", not result.startswith("Error"), result)
        with open(contradictions_path, encoding="utf-8") as f:
            content = f.read()
        check("[8c] Vault_Contradictions.mdの該当行が[x]に書き換わる",
              "- [x] resolved | 2026-08-22T10:00:00 | [[NoteA]] ⇔ [[NoteB]]" in content, content)
        check("[8c] 裁定結果 (A採用) が追記される", "→ 裁定: A採用" in content, content)
    print("--- [8c] done ---")


# ---------------------------------------------------------
# [9] _set_frontmatter_field
# ---------------------------------------------------------

def test_set_frontmatter_field_overwrites_existing():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "note.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\ntags:\n  - type/permanent\ncontradiction_status: open\n---\n# T\n\n本文\n")
        tobs._set_frontmatter_field(path, "contradiction_status", "resolved")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        check("[9a] 既存フィールドが上書きされる", _field(content, "contradiction_status") == "resolved", content)
        check("[9a] 1行のみ (重複しない)", content.count("contradiction_status:") == 1, content)
    print("--- [9a] done ---")


def test_set_frontmatter_field_adds_new_field():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "note.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\ntags:\n  - type/permanent\n---\n# T\n\n本文\n")
        tobs._set_frontmatter_field(path, "contradiction_verdict", "adopted")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        check("[9b] 新規フィールドが末尾に追加される",
              _field(content, "contradiction_verdict") == "adopted", content)
        check("[9b] 本文部分は変更されない", "本文" in content, content)
    print("--- [9b] done ---")


def test_set_frontmatter_field_noop_without_frontmatter():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "note.md")
        original = "# T (frontmatterなし)\n\n本文\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)
        tobs._set_frontmatter_field(path, "contradiction_status", "resolved")
        with open(path, encoding="utf-8") as f:
            after = f.read()
        check("[9c] frontmatterが無いノートは無変更", after == original, after)
    print("--- [9c] done ---")


# ---------------------------------------------------------
# build_contradiction_names_and_links (queue_contradiction/_default_write_contradiction が
# 共有する組み立てロジック自体の単体テスト)
# ---------------------------------------------------------

def test_build_contradiction_names_and_links_handles_missing_related():
    own_name, own_link, related_name, related_link = build_contradiction_names_and_links(
        "30_Main/NoteA.md", "")
    check("[0] own_nameは拡張子なし", own_name == "30_Main/NoteA", own_name)
    check("[0] own_linkはwikilink形式", own_link == "[[30_Main/NoteA]]", own_link)
    check("[0] related_note空はrelated_name空", related_name == "", related_name)
    check("[0] related_note空はrelated_linkが(不明)", related_link == "(不明)", related_link)
    print("--- [0] done ---")


def test_find_file_in_vault_resolves_subfolder_relpath():
    """Vault Watcher は 30_Main/Knowledge_Base/X.md を own_name="Knowledge_Base/X" で
    渡す。旧実装はベース名一致しか見ず常に None → 裁定ボタンが「ノートが見つかりません」。"""
    with _Vault() as v:
        sub = os.path.join(v.root, "30_Main", "Knowledge_Base")
        os.makedirs(sub)
        target = os.path.join(sub, "NoteX.md")
        open(target, "w", encoding="utf-8").write("x")
        other = os.path.join(v.root, "40_Report", "NoteX.md")  # 同名の別ファイル
        os.makedirs(os.path.dirname(other))
        open(other, "w", encoding="utf-8").write("y")
        own_name = build_contradiction_names_and_links("Knowledge_Base/NoteX.md", "")[0]
        got = _ls.find_file_in_vault(own_name)
        check("[S1] サブフォルダ相対パスで解決できる", got == target, str(got))
        check("[S2] Vaultルート相対パスも従来どおり解決",
              os.path.normpath(_ls.find_file_in_vault("30_Main/Knowledge_Base/NoteX") or "")
              == target)
        check("[S3] ベース名だけでも従来どおり見つかる",
              _ls.find_file_in_vault("NoteX") in (target, other))
        check("[S4] 末尾一致しないパスは None",
              _ls.find_file_in_vault("Wrong_Dir/NoteX") is None)
    print("--- [S] done ---")


if __name__ == "__main__":
    test_find_file_in_vault_resolves_subfolder_relpath()
    test_build_contradiction_names_and_links_handles_missing_related()
    test_queue_contradiction_registers_pending_and_notifies()
    test_queue_contradiction_skips_when_related_note_unresolvable()
    test_queue_contradiction_skips_when_slots_full()
    test_resolve_contradiction_verdict_a_marks_own_adopted()
    test_resolve_contradiction_verdict_b_marks_related_adopted()
    test_resolve_contradiction_verdict_both_marks_both()
    test_resolve_contradiction_invalid_verdict_returns_error()
    test_resolve_contradiction_unknown_slot_returns_error()
    test_resolve_contradiction_removes_slot_after_success()
    test_mark_contradiction_resolved_rewrites_only_matching_line()
    test_mark_contradiction_resolved_no_match_leaves_file_untouched()
    test_resolve_contradiction_updates_contradictions_note_via_full_path()
    test_set_frontmatter_field_overwrites_existing()
    test_set_frontmatter_field_adds_new_field()
    test_set_frontmatter_field_noop_without_frontmatter()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for f in _FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    sys.exit(0)
