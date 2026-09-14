# -*- coding: utf-8 -*-
"""
tools/tool_article_project.py の往復テスト (indexed-snacking-crab plan §1)。

管理ノートはObsidianノート自体を正本とする設計のため、create/load/update/appendの
往復と、ファイル名衝突時のタイムスタンプ退避を検証する。外部API呼び出しは一切ない
(YAML読み書きのみ) だが、tool_obsidian経由でlib_shared/クライアントをimportするため、
このマシンの .env (実キー) がそのまま使われる — 実際に呼び出す関数が無いので無害。

PROJECTS_DIR は `from lib.lib_shared import VAULT_ROOT` の値から import 時に一度だけ
計算されるモジュール変数 (known-bug-patterns.md: from-import はコピー束縛なので
lib_shared.VAULT_ROOT を差し替えても連動しない)。そのためテストは
tools.tool_article_project.PROJECTS_DIR を直接一時ディレクトリへ差し替える。

実行: py tests/test_tool_article_project.py
"""
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_article_project as tap  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


class _TmpProjectsDir:
    """tool_article_project.PROJECTS_DIR / MAIN_DIR を一時ディレクトリへ差し替える。"""

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="article_project_test_")
        self.main_root = tempfile.mkdtemp(prefix="article_project_main_test_")
        self._saved = tap.PROJECTS_DIR
        self._saved_main = tap.MAIN_DIR
        tap.PROJECTS_DIR = self.root
        tap.MAIN_DIR = self.main_root
        return self

    def __exit__(self, *exc):
        tap.PROJECTS_DIR = self._saved
        tap.MAIN_DIR = self._saved_main
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.main_root, ignore_errors=True)


def test_create_and_load_roundtrip():
    with _TmpProjectsDir():
        filename, error = tap.create_project("未経験からAIエージェントを作った話", "初心者向け", thread_id=12345)
        check("create_project succeeds", error is None, str(error))
        check("create_project returns filename", bool(filename))

        project = tap.load_project(filename)
        check("load_project returns dict", project is not None)
        if project:
            fm = project["frontmatter"]
            check("frontmatter status=goal_set", fm.get("status") == "goal_set", fm.get("status"))
            check("frontmatter goal preserved", fm.get("goal") == "未経験からAIエージェントを作った話", fm.get("goal"))
            check("frontmatter thread_id preserved", fm.get("thread_id") == 12345, fm.get("thread_id"))
            check(
                "body has all sections",
                all(f"## {h}" in project["body"] for h in ("知識収集ログ", "不足情報Q&A", "生成した記事（下書き）")),
                project["body"],
            )


def test_update_fields():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標X")
        ok = tap.update_project_fields(filename, status="qa", adopted_source_files=["A.md", "B.md"])
        check("update_project_fields returns True", ok)

        project = tap.load_project(filename)
        check("status updated", project["frontmatter"]["status"] == "qa")
        check("adopted_source_files updated", project["frontmatter"]["adopted_source_files"] == ["A.md", "B.md"])
        check("updated field present", "updated" in project["frontmatter"])


def test_append_section():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標Y")
        tap.append_project_section(filename, "知識収集ログ", "採用: [[Note1]]")
        tap.append_project_section(filename, "知識収集ログ", "採用: [[Note2]]")

        project = tap.load_project(filename)
        body = project["body"]
        check("first append present", "採用: [[Note1]]" in body)
        check("second append present", "採用: [[Note2]]" in body)
        qa_idx = body.index("## 不足情報Q&A")
        note1_idx = body.index("採用: [[Note1]]")
        check("append stays within its own section", note1_idx < qa_idx, f"note1_idx={note1_idx}, qa_idx={qa_idx}")


def test_append_creates_missing_section():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標Z")
        tap.append_project_section(filename, "新規セクション", "内容A")
        project = tap.load_project(filename)
        check(
            "new section created",
            "## 新規セクション" in project["body"] and "内容A" in project["body"],
            project["body"],
        )


def test_duplicate_goal_gets_timestamp_suffix():
    with _TmpProjectsDir():
        filename1, _ = tap.create_project("同じ目標")
        filename2, _ = tap.create_project("同じ目標")
        check("duplicate filenames differ", filename1 != filename2, f"{filename1} vs {filename2}")


def test_empty_goal_rejected():
    with _TmpProjectsDir():
        filename, error = tap.create_project("   ")
        check("empty goal rejected", filename is None and error is not None)


def test_load_missing_project_returns_none():
    with _TmpProjectsDir():
        check("missing project returns None", tap.load_project("no_such_file.md") is None)


# ---------------------------------------------------------
# sanitize_reference_material — 個人情報の絶対除外 (レビュー指摘の再発防止)
#
# tool_obsidian._strip_section_or_warn は標準見出し `## パーソナル情報` への完全一致でしか
# 検出できず、見出しに説明文・絵文字が付くと非破壊のまま (警告だけで内容が残る)。
# 標準見出し1例のテストだけでは「除去できないケースがある」ことを保証できないという
# レビュー指摘を受け、標準/崩れた見出しの両方をここで検証する。
# ---------------------------------------------------------

def _with_fake_fetch_context(mapping: dict):
    """tool_fetch_context を差し替えるコンテキストマネージャ。"""
    class _Ctx:
        def __enter__(self):
            self._orig = tap.tool_fetch_context
            tap.tool_fetch_context = lambda filename: mapping[filename]
            return self

        def __exit__(self, *exc):
            tap.tool_fetch_context = self._orig

    return _Ctx()


def test_sanitize_reference_material_removes_standard_heading():
    mapping = {
        "Note.md": "## パーソナル情報\n住所: 東京都どこか1-2-3\n\n## 好きな技術\nPython\n",
    }
    with _with_fake_fetch_context(mapping):
        material, excluded = tap.sanitize_reference_material(["Note.md"])
    check("standard heading file not excluded", excluded == [], excluded)
    check("standard heading content removed", "東京都どこか" not in material, material)
    check("unrelated content preserved", "好きな技術" in material, material)


def test_sanitize_reference_material_excludes_unremovable_heading():
    mapping = {
        "Note.md": "## パーソナル情報 (private)\n住所: 東京都どこか1-2-3\n\n## 好きな技術\nPython\n",
    }
    with _with_fake_fetch_context(mapping):
        material, excluded = tap.sanitize_reference_material(["Note.md"])
    check("file with unremovable heading is excluded", excluded == ["Note.md"], excluded)
    check("excluded file's content absent from material", material == "", material)


def test_sanitize_reference_material_mixed_files():
    mapping = {
        "Clean.md": "## 好きな技術\nPython\n",
        "Dirty.md": "## 🔒パーソナル情報\n電話: 000-0000-0000\n",
    }
    with _with_fake_fetch_context(mapping):
        material, excluded = tap.sanitize_reference_material(["Clean.md", "Dirty.md"])
    check("only the dirty file is excluded", excluded == ["Dirty.md"], excluded)
    check("clean file content present", "好きな技術" in material, material)


def test_sanitize_reference_material_excludes_indented_heading():
    """レビュー指摘1の回帰: 先頭に空白があるインデント付き見出しも検出対象にする。
    CommonMark では見出し行の前に最大3スペースまでのインデントが許容される。"""
    mapping = {
        "Note.md": "   ## パーソナル情報\n住所: 東京都どこか1-2-3\n\n## 好きな技術\nPython\n",
    }
    with _with_fake_fetch_context(mapping):
        material, excluded = tap.sanitize_reference_material(["Note.md"])
    check("indented personal-info heading is excluded", excluded == ["Note.md"], excluded)
    check("excluded file's content absent from material", "東京都どこか" not in material, material)


# ---------------------------------------------------------
# save_final_draft — 記事を独立ファイルに保存し、状態はリンクのみをアトミックに記録する
# (レビュー指摘2: 記事本文の##見出しがセクション終端判定を誤らせ重複する欠陥への対応)
# ---------------------------------------------------------

def test_save_final_draft_saves_article_to_separate_file():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標A")
        ok, backup = tap.save_final_draft(filename, "# 完成記事\n本文です。")
        check("save_final_draft returns True", ok)
        check("no conflict backup on normal save", backup is None, backup)

        project = tap.load_project(filename)
        check("status becomes draft_done", project["frontmatter"]["status"] == "draft_done")
        check("draft_filename recorded in frontmatter", project["frontmatter"]["draft_filename"] is not None)
        check(
            "note body contains a link, not the article body itself",
            "[[" in project["body"] and "# 完成記事" not in project["body"],
            project["body"],
        )

        draft = tap.load_draft_article(filename)
        check("draft article readable via load_draft_article", draft is not None)
        check("draft article content matches", draft == "# 完成記事\n本文です。" if draft else False, draft)


def test_save_final_draft_with_internal_headings_does_not_corrupt_other_sections():
    """レビュー指摘2の再現ケース: 記事本文に `## 詳細` のような見出しを含めても、
    管理ノートの他セクション(不足情報Q&A等)を侵食したり、再保存で重複したりしないこと。"""
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標A2")
        tap.append_project_section(filename, "不足情報Q&A", "Q: q1\nA: a1")

        article_with_headings = "# 完成記事\n導入\n\n## 詳細\nもっと詳しく\n\n## まとめ\n以上です。"
        tap.save_final_draft(filename, article_with_headings)
        # 保存失敗後の再試行を模して同じ記事でもう一度保存する
        tap.save_final_draft(filename, article_with_headings)

        project = tap.load_project(filename)
        check(
            "Q&A section untouched by article's internal headings",
            "Q: q1\nA: a1" in project["body"],
            project["body"],
        )
        check(
            "note body still just a link after retry (no duplication)",
            project["body"].count("[[") == 1,
            project["body"],
        )

        draft = tap.load_draft_article(filename)
        check("draft article content matches exactly (no duplication)", draft == article_with_headings, draft)


def test_save_final_draft_retry_overwrites_not_duplicates():
    """同じ記事で2回呼んでも(保存失敗後の再試行を模す)、記事ファイルの内容が重複しないこと。"""
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標B")
        tap.save_final_draft(filename, "# 完成記事\n本文です。")
        tap.save_final_draft(filename, "# 完成記事\n本文です。")

        draft = tap.load_draft_article(filename)
        occurrences = draft.count("# 完成記事") if draft else 0
        check("article appears exactly once after retry", occurrences == 1, f"occurrences={occurrences}")


def test_save_final_draft_missing_project_returns_false():
    with _TmpProjectsDir():
        check(
            "save_final_draft on missing project returns False",
            tap.save_final_draft("no_such_file.md", "# 記事") == (False, None),
        )


def test_load_draft_article_missing_returns_none():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標A3")
        check("load_draft_article returns None before any draft is saved", tap.load_draft_article(filename) is None)


# ---------------------------------------------------------
# save_adopted_sources — 資料採用時の status・ログをアトミックに保存する (レビュー指摘3)
# ---------------------------------------------------------

def test_save_adopted_sources_updates_status_and_log_together():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標C")
        ok = tap.save_adopted_sources(filename, ["A.md", "B.md"], "採用した資料:\n- [[A]]\n- [[B]]")
        check("save_adopted_sources returns True", ok)

        project = tap.load_project(filename)
        check("status becomes qa", project["frontmatter"]["status"] == "qa")
        check("adopted_source_files saved", project["frontmatter"]["adopted_source_files"] == ["A.md", "B.md"])
        check("log text appended", "採用した資料" in project["body"])


def test_save_adopted_sources_missing_project_returns_false():
    with _TmpProjectsDir():
        check(
            "save_adopted_sources on missing project returns False",
            tap.save_adopted_sources("no_such_file.md", [], "log") is False,
        )


def test_update_missing_project_returns_false():
    with _TmpProjectsDir():
        check(
            "update on missing project returns False",
            tap.update_project_fields("no_such_file.md", status="qa") is False,
        )


# ---------------------------------------------------------
# _atomic_write — 書き込み失敗時に元ファイルを壊さない (レビュー指摘1)
# ---------------------------------------------------------

def test_atomic_write_failure_leaves_original_file_untouched():
    """os.replace が失敗する状況を模し、更新前のノート内容が一切変更されないことを確認する。
    以前は open(path, "w") で直接切り詰めていたため、書き込み途中の失敗でノートが
    壊れた状態のまま False が返っていた。"""
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標D", audience="元の想定読者")
        path = os.path.join(tap.PROJECTS_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            original_content = f.read()

        orig_replace = tap.os.replace
        try:
            def _failing_replace(*args, **kwargs):
                raise OSError("simulated disk failure during replace")
            tap.os.replace = _failing_replace

            ok = tap.update_project_fields(filename, audience="壊れているはずの新しい値")
            check("update_project_fields returns False on replace failure", ok is False)
        finally:
            tap.os.replace = orig_replace

        with open(path, "r", encoding="utf-8") as f:
            after_content = f.read()
        check("original file content unchanged after failed write", after_content == original_content)

        leftover_tmp = [f for f in os.listdir(tap.PROJECTS_DIR) if f.endswith(".tmp")]
        check("temp file cleaned up after failure", leftover_tmp == [], leftover_tmp)


def test_atomic_write_failure_during_save_final_draft_leaves_project_note_untouched():
    """save_final_draft の管理ノート側書き込みが失敗しても、記事ファイルは書けているが
    ノート自体は壊れていないこと (次の再試行で load_draft_article から読み直せる)。"""
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標E")
        path = os.path.join(tap.PROJECTS_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            original_content = f.read()

        orig_replace = tap.os.replace
        call_count = {"n": 0}
        try:
            def _fail_second_replace(*args, **kwargs):
                call_count["n"] += 1
                if call_count["n"] >= 2:  # 1回目は記事ファイル書き込み、2回目がノート更新
                    raise OSError("simulated failure on note update")
                return orig_replace(*args, **kwargs)
            tap.os.replace = _fail_second_replace

            ok, _ = tap.save_final_draft(filename, "# 記事\n本文")
            check("save_final_draft returns False when note write fails", ok is False)
        finally:
            tap.os.replace = orig_replace

        with open(path, "r", encoding="utf-8") as f:
            after_content = f.read()
        check("project note unchanged when note-side write fails", after_content == original_content)


# ---------------------------------------------------------
# record_qa_answer — qa_pairs全体の永続化 (要件④)
# ---------------------------------------------------------

def test_record_qa_answer_persists_full_qa_pairs():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標F")
        qa_pairs = [
            {"point": "p1", "question": "q1", "answer": "a1"},
            {"point": "p2", "question": "q2", "answer": None},
        ]
        ok = tap.record_qa_answer(filename, qa_pairs, "q1", "a1")
        check("record_qa_answer returns True", ok)

        project = tap.load_project(filename)
        check("qa_pairs persisted in frontmatter", project["frontmatter"]["qa_pairs"] == qa_pairs)
        check("answer log appended to body", "Q: q1\nA: a1" in project["body"])


def test_record_qa_answer_with_heading_in_answer_does_not_corrupt_sections():
    """レビュー指摘6の回帰: Q&A回答に `## ` から始まる行が含まれても、
    「不足情報Q&A」セクションが分断されず、他セクションを侵食しない。"""
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標G")
        tap.append_project_section(filename, "知識収集ログ", "採用: [[Note1]]")

        tricky_answer = "はい、やりました。\n## これは見出しではなく回答の一部です\n続きの文章。"
        qa_pairs = [{"point": "p1", "question": "q1", "answer": tricky_answer}]
        tap.record_qa_answer(filename, qa_pairs, "q1", tricky_answer)

        project = tap.load_project(filename)
        check(
            "knowledge log section untouched by answer's fake heading",
            "採用: [[Note1]]" in project["body"],
            project["body"],
        )
        check(
            "answer content (minus neutralized heading marker) present in qa section",
            "これは見出しではなく回答の一部です" in project["body"],
            project["body"],
        )
        # ゼロ幅スペースで無害化されているため、素の "## " という行頭パターンは
        # このセクション追記由来では出現しない(知識収集ログ側の見出しは別途存在するため
        # 完全に0件ではなく、"分断" が起きていないことは上のcheckで検証済み)。
        check(
            "fake heading no longer starts with a bare '## '",
            "\n## これは見出しではなく回答の一部です" not in project["body"],
            project["body"],
        )


def test_record_qa_answer_missing_project_returns_false():
    with _TmpProjectsDir():
        check(
            "record_qa_answer on missing project returns False",
            tap.record_qa_answer("no_such_file.md", [], "q", "a") is False,
        )


# ---------------------------------------------------------
# list_projects — 進行中プロジェクトの列挙 (要件④、Bot再起動時の復元に使う)
# ---------------------------------------------------------

def test_list_projects_excludes_draft_and_conflict_files():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標H")
        tap.save_final_draft(filename, "# 記事\n本文")  # *_Draft.md が作られる

        # 手動で conflict ファイルも置いてみる(実際に競合を起こさなくても列挙対象外か確認できる)
        conflict_path = os.path.join(tap.PROJECTS_DIR, "dummy_conflict.md")
        with open(conflict_path, "w", encoding="utf-8") as f:
            f.write("---\nstatus: qa\n---\n")

        projects = tap.list_projects()
        filenames = [p["filename"] for p in projects]
        check("management note included", filename in filenames, filenames)
        check("draft file excluded", not any(f.endswith("_Draft.md") for f in filenames), filenames)
        check("conflict file excluded", not any("_conflict" in f for f in filenames), filenames)


def test_list_projects_filters_by_status():
    with _TmpProjectsDir():
        f1, _ = tap.create_project("目標I1")
        f2, _ = tap.create_project("目標I2")
        tap.update_project_fields(f2, status="qa")

        goal_set_only = tap.list_projects(statuses=["goal_set"])
        check("status filter returns only goal_set", {p["filename"] for p in goal_set_only} == {f1}, goal_set_only)

        qa_only = tap.list_projects(statuses=["qa"])
        check("status filter returns only qa", {p["filename"] for p in qa_only} == {f2}, qa_only)


def test_list_projects_empty_dir_returns_empty_list():
    with _TmpProjectsDir():
        check("empty PROJECTS_DIR returns []", tap.list_projects() == [])


# ---------------------------------------------------------
# 手動編集検知・競合時の別版保存 (要件④)
# ---------------------------------------------------------

def test_update_project_fields_detects_manual_edit_and_backs_up():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標J")
        path = os.path.join(tap.PROJECTS_DIR, filename)

        # ユーザーが手動編集した状況を模す: mtimeを未来にずらす(比較窓2秒を超えさせる)
        future = __import__("time").time() + 100
        os.utime(path, (future, future))

        ok = tap.update_project_fields(filename, status="qa")
        check("update rejected when disk was manually edited", ok is False)

        entries = os.listdir(tap.PROJECTS_DIR)
        check("conflict backup created", any("_conflict" in e for e in entries), entries)

        project = tap.load_project(filename)
        check("status NOT advanced to qa after conflict", project["frontmatter"]["status"] == "goal_set")


def test_save_final_draft_detects_manual_edit_on_draft_file():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標K")
        tap.save_final_draft(filename, "# 初版\n本文")

        draft_path = os.path.join(tap.PROJECTS_DIR, tap._draft_filename(filename))
        future = __import__("time").time() + 100
        os.utime(draft_path, (future, future))

        ok, backup_filename = tap.save_final_draft(filename, "# 再生成された記事\n新しい本文")
        check("save_final_draft returns False on draft conflict", ok is False)
        check("backup filename returned", backup_filename is not None and "_conflict" in backup_filename, backup_filename)

        # 元の下書き(手動編集後の内容)がそのまま残っており、新しい記事で上書きされていない
        draft = tap.load_draft_article(filename)
        check("original (manually edited) draft left untouched", draft == "# 初版\n本文", draft)

        project = tap.load_project(filename)
        check("note status unaffected by rejected draft save", project["frontmatter"]["status"] == "draft_done")


# ---------------------------------------------------------
# promote_and_reflect — 正式ノート化・振り返り記録 (要件⑤)
# ---------------------------------------------------------

def test_promote_and_reflect_creates_main_note_and_records_reflection():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標L", audience="初心者向け")
        tap.save_adopted_sources(filename, ["Note1.md", "Note2.md"], "採用した資料:\n- [[Note1]]\n- [[Note2]]")
        tap.save_final_draft(filename, "# 素晴らしい記事タイトル\n本文です。\n\n## 詳細\nもっと詳しく。")

        main_filename, error = tap.promote_and_reflect(filename, "学んだこと", "次に試すこと")
        check("promote_and_reflect returns no error", error is None, error)
        check("main note filename derived from H1 title", main_filename == "素晴らしい記事タイトル.md", main_filename)

        main_path = os.path.join(tap.MAIN_DIR, main_filename)
        check("main note file created", os.path.exists(main_path))
        with open(main_path, "r", encoding="utf-8") as f:
            main_content = f.read()
        check("main note contains article body", "本文です。" in main_content and "## 詳細" in main_content, main_content)
        check("main note links back to management note", f"[[{os.path.splitext(filename)[0]}]]" in main_content, main_content)
        check("main note lists adopted sources", "[[Note1]]" in main_content and "[[Note2]]" in main_content, main_content)

        project = tap.load_project(filename)
        fm = project["frontmatter"]
        check("management note status becomes completed", fm["status"] == "completed")
        check("promoted_filename recorded", fm["promoted_filename"] == main_filename)
        check("reflection recorded", fm["reflection"]["learning"] == "学んだこと" and fm["reflection"]["next_action"] == "次に試すこと")
        check("promoted-note section links to main note", f"[[{os.path.splitext(main_filename)[0]}]]" in project["body"], project["body"])
        check("reflection section present in body", "学んだこと" in project["body"], project["body"])


def test_promote_and_reflect_is_idempotent_on_retry():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標M")
        tap.save_final_draft(filename, "# タイトルM\n本文")

        first_filename, _ = tap.promote_and_reflect(filename, "学び1", "次1")
        second_filename, error = tap.promote_and_reflect(filename, "別の学び", "別の次")

        check("second call returns same main filename", second_filename == first_filename, (first_filename, second_filename))
        check("second call reports no error", error is None, error)

        main_entries = [e for e in os.listdir(tap.MAIN_DIR) if e.endswith(".md")]
        check("only one main note file exists after retry", len(main_entries) == 1, main_entries)

        project = tap.load_project(filename)
        check(
            "reflection NOT overwritten by second call (idempotent short-circuit)",
            project["frontmatter"]["reflection"]["learning"] == "学び1",
            project["frontmatter"]["reflection"],
        )


def test_promote_and_reflect_missing_draft_returns_error():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標N")  # save_final_draftを呼ばず下書き無しのまま
        main_filename, error = tap.promote_and_reflect(filename, "学び", "次")
        check("promote_and_reflect returns None on missing draft", main_filename is None)
        check("error message present", bool(error), error)


def test_promote_and_reflect_missing_project_returns_error():
    with _TmpProjectsDir():
        main_filename, error = tap.promote_and_reflect("no_such_file.md", "学び", "次")
        check("promote_and_reflect returns None for missing project", main_filename is None)
        check("error message present for missing project", bool(error), error)


def test_promote_and_reflect_falls_back_to_goal_when_title_missing():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("見出しの無い記事の目標")
        tap.save_final_draft(filename, "見出しなしの本文だけの記事です。")

        main_filename, error = tap.promote_and_reflect(filename, "学び", "次")
        check("promote_and_reflect succeeds without H1", error is None, error)
        check(
            "filename derived from goal when title extraction fails",
            main_filename is not None and "見出しの無い記事の目標" in main_filename,
            main_filename,
        )


def test_promote_and_reflect_neutralizes_heading_in_reflection_input():
    """レビュー指摘6と同型の回帰: 振り返り入力に `## ` から始まる行が含まれても
    管理ノートの他セクションを侵食しない。"""
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標O")
        tap.append_project_section(filename, "知識収集ログ", "採用: [[NoteX]]")
        tap.save_final_draft(filename, "# タイトルO\n本文")

        tricky_learning = "## これは見出しではない学び\n続きの文章"
        tap.promote_and_reflect(filename, tricky_learning, "")

        project = tap.load_project(filename)
        check("knowledge log section untouched by reflection's fake heading", "採用: [[NoteX]]" in project["body"], project["body"])
        check(
            "reflection content preserved (heading neutralized, not deleted)",
            "これは見出しではない学び" in project["body"],
            project["body"],
        )


# ---------------------------------------------------------
# build_dashboard_text — 進捗ダッシュボード (要件⑤、純関数)
# ---------------------------------------------------------

def test_build_dashboard_text_empty_projects():
    text = tap.build_dashboard_text([])
    check("empty project list produces a 0-count message without raising", "0件" in text, text)


def test_build_dashboard_text_groups_by_status():
    projects = [
        {"filename": "a.md", "frontmatter": {"status": "goal_set", "goal": "目標A"}},
        {"filename": "b.md", "frontmatter": {"status": "qa", "goal": "目標B", "qa_pairs": [
            {"question": "q1", "answer": "a1"}, {"question": "q2", "answer": None},
        ]}},
        {"filename": "c.md", "frontmatter": {"status": "draft_done", "goal": "目標C", "draft_filename": "c_Draft.md"}},
        {"filename": "d.md", "frontmatter": {
            "status": "completed", "goal": "目標D", "promoted_filename": "記事D.md",
            "reflection": {"learning": "学びD"},
        }},
    ]
    text = tap.build_dashboard_text(projects)
    check("4件と表示される", "4件" in text, text)
    check("goal_set project listed", "目標A" in text, text)
    check("qa progress shown as 1/2", "1/2問回答済み" in text, text)
    check("draft_done links to draft note", "[[c_Draft]]" in text, text)
    check("completed links to promoted note with learning", "[[記事D]]" in text and "学びD" in text, text)


def test_build_dashboard_text_handles_missing_optional_fields():
    projects = [{"filename": "e.md", "frontmatter": {"status": "goal_set"}}]  # goalすら無い
    text = tap.build_dashboard_text(projects)
    check("missing goal does not raise and shows placeholder", "目標未設定" in text, text)


# ---------------------------------------------------------
# X投稿連携: generate_x_post_draft / record_x_post_result
# (client.models.generate_content をモックし、実Gemini APIは一切叩かない)
# ---------------------------------------------------------

class _FakeGeminiResponse:
    def __init__(self, text):
        self.text = text


class _StubGemini:
    """呼び出し毎に texts を順に返すスタブ (余ったら最後の値を繰り返す)。calls に contents を記録。"""

    def __init__(self, texts):
        self.texts = texts
        self.calls = []

    def __enter__(self):
        import lib.lib_shared as ls
        self._ls = ls
        self._orig = ls.client.models.generate_content

        def _fn(model=None, contents=None, config=None):
            self.calls.append(contents)
            idx = min(len(self.calls) - 1, len(self.texts) - 1)
            return _FakeGeminiResponse(self.texts[idx])

        ls.client.models.generate_content = _fn
        return self

    def __exit__(self, *exc):
        self._ls.client.models.generate_content = self._orig


def _write_main_note(main_root: str, filename: str, body: str = "# タイトル\n\n本文です。"):
    with open(os.path.join(main_root, filename), "w", encoding="utf-8") as f:
        f.write(f"---\ntags: [type/permanent]\nstatus: done\n---\n\n{body}\n")


def test_generate_x_post_draft_rejects_non_completed_status():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標A")
        draft, error = tap.generate_x_post_draft(filename)
        check("rejects non-completed status", draft is None and error is not None, error)


def test_generate_x_post_draft_missing_promoted_filename():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標B")
        tap.update_project_fields(filename, status="completed")
        draft, error = tap.generate_x_post_draft(filename)
        check("missing promoted_filename returns error", draft is None and "正式ノート" in (error or ""), error)


def test_generate_x_post_draft_missing_main_note_file():
    with _TmpProjectsDir():
        filename, _ = tap.create_project("目標C")
        tap.update_project_fields(filename, status="completed", promoted_filename="存在しない.md")
        draft, error = tap.generate_x_post_draft(filename)
        check("missing main note file returns error", draft is None and error is not None, error)


def test_generate_x_post_draft_success_within_limit():
    with _TmpProjectsDir() as ctx:
        filename, _ = tap.create_project("目標D")
        tap.update_project_fields(filename, status="completed", promoted_filename="記事D.md")
        _write_main_note(ctx.main_root, "記事D.md")

        with _StubGemini(["短いX投稿文です。"]) as stub:
            draft, error = tap.generate_x_post_draft(filename)
        check("success returns draft", error is None and draft is not None, error)
        if draft:
            check("draft text matches stub", draft["text"] == "短いX投稿文です。", draft["text"])
            check("draft source_note is promoted_filename", draft["source_note"] == "記事D.md", draft["source_note"])
            check("draft char_count matches", draft["char_count"] == len(draft["text"]))
        check("gemini called exactly once (no retry needed)", len(stub.calls) == 1, len(stub.calls))


def test_generate_x_post_draft_without_url_uses_full_280_limit():
    with _TmpProjectsDir() as ctx:
        filename, _ = tap.create_project("目標NoURL")
        tap.update_project_fields(filename, status="completed", promoted_filename="記事NoURL.md")
        _write_main_note(ctx.main_root, "記事NoURL.md")

        with _StubGemini(["本文のみ"]) as stub:
            draft, error = tap.generate_x_post_draft(filename)
        check("no-url draft succeeds", error is None and draft is not None, error)
        if draft:
            check("no-url draft text has no appended url", draft["text"] == "本文のみ", draft["text"])
        check("prompt uses the full 280-char limit when no url is given",
              f"{tap.X_POST_MAX_CHARS}字以内" in stub.calls[0], stub.calls[0])


def test_generate_x_post_draft_with_url_appends_url_and_shrinks_body_limit():
    with _TmpProjectsDir() as ctx:
        filename, _ = tap.create_project("目標URL")
        tap.update_project_fields(filename, status="completed", promoted_filename="記事URL.md")
        _write_main_note(ctx.main_root, "記事URL.md")

        url = "https://example.com/article"
        with _StubGemini(["短い本文"]) as stub:
            draft, error = tap.generate_x_post_draft(filename, url=url)
        check("url draft succeeds", error is None and draft is not None, error)
        if draft:
            check("draft text ends with the url on its own line", draft["text"].endswith(f"\n{url}"), draft["text"])
            check("draft text still contains the generated body", "短い本文" in draft["text"], draft["text"])

        expected_body_limit = tap.X_POST_MAX_CHARS - (tap.X_URL_WEIGHTED_LENGTH + 1)
        check("prompt shrinks the body limit to leave room for the url (weighted length)",
              f"{expected_body_limit}字以内" in stub.calls[0], stub.calls[0])
        check("prompt tells the model not to write a url itself",
              "URLは本文に書かない" in stub.calls[0], stub.calls[0])


def test_generate_x_post_draft_retries_once_when_over_limit():
    with _TmpProjectsDir() as ctx:
        filename, _ = tap.create_project("目標E")
        tap.update_project_fields(filename, status="completed", promoted_filename="記事E.md")
        _write_main_note(ctx.main_root, "記事E.md")

        long_text = "あ" * 300
        short_text = "縮めた投稿文"
        with _StubGemini([long_text, short_text]) as stub:
            draft, error = tap.generate_x_post_draft(filename)
        check("retry succeeds", error is None and draft is not None, error)
        if draft:
            check("draft uses retried short text", draft["text"] == short_text, draft["text"])
        check("gemini called twice (initial + retry)", len(stub.calls) == 2, len(stub.calls))


def test_generate_x_post_draft_fails_when_still_over_limit_after_retry():
    with _TmpProjectsDir() as ctx:
        filename, _ = tap.create_project("目標F")
        tap.update_project_fields(filename, status="completed", promoted_filename="記事F.md")
        _write_main_note(ctx.main_root, "記事F.md")

        long_text = "あ" * 300
        with _StubGemini([long_text, long_text]) as stub:
            draft, error = tap.generate_x_post_draft(filename)
        check("still-over-limit returns error", draft is None and error is not None, error)
        check("gemini called twice before giving up", len(stub.calls) == 2, len(stub.calls))


def test_generate_x_post_draft_gemini_exception_returns_error():
    with _TmpProjectsDir() as ctx:
        filename, _ = tap.create_project("目標G")
        tap.update_project_fields(filename, status="completed", promoted_filename="記事G.md")
        _write_main_note(ctx.main_root, "記事G.md")

        import lib.lib_shared as ls
        orig = ls.client.models.generate_content

        def _raise(model=None, contents=None, config=None):
            raise RuntimeError("API down")

        ls.client.models.generate_content = _raise
        try:
            draft, error = tap.generate_x_post_draft(filename)
        finally:
            ls.client.models.generate_content = orig
        check("gemini exception returns error not raise", draft is None and error is not None, error)


def test_record_x_post_result_updates_frontmatter():
    with _TmpProjectsDir() as ctx:
        _write_main_note(ctx.main_root, "記事H.md")
        ok = tap.record_x_post_result(
            "記事H.md", "https://x.com/i/web/status/999", "2026-09-14T10:00:00", "投稿本文"
        )
        check("record_x_post_result returns True", ok)

        with open(os.path.join(ctx.main_root, "記事H.md"), "r", encoding="utf-8") as f:
            fm, _ = tap._split_frontmatter(f.read())
        check("x_post_url recorded", fm.get("x_post_url") == "https://x.com/i/web/status/999", fm.get("x_post_url"))
        check("x_post_at recorded", fm.get("x_post_at") == "2026-09-14T10:00:00")
        check("x_post_text recorded", fm.get("x_post_text") == "投稿本文")


def test_record_x_post_result_missing_file_returns_false():
    with _TmpProjectsDir():
        ok = tap.record_x_post_result("存在しない.md", "https://x.com/i/web/status/1", "2026-09-14T10:00:00", "t")
        check("missing file returns False", ok is False)


if __name__ == "__main__":
    test_create_and_load_roundtrip()
    test_update_fields()
    test_append_section()
    test_append_creates_missing_section()
    test_duplicate_goal_gets_timestamp_suffix()
    test_empty_goal_rejected()
    test_load_missing_project_returns_none()
    test_update_missing_project_returns_false()
    test_sanitize_reference_material_removes_standard_heading()
    test_sanitize_reference_material_excludes_unremovable_heading()
    test_sanitize_reference_material_mixed_files()
    test_sanitize_reference_material_excludes_indented_heading()
    test_save_final_draft_saves_article_to_separate_file()
    test_save_final_draft_with_internal_headings_does_not_corrupt_other_sections()
    test_save_final_draft_retry_overwrites_not_duplicates()
    test_save_final_draft_missing_project_returns_false()
    test_load_draft_article_missing_returns_none()
    test_save_adopted_sources_updates_status_and_log_together()
    test_save_adopted_sources_missing_project_returns_false()
    test_atomic_write_failure_leaves_original_file_untouched()
    test_atomic_write_failure_during_save_final_draft_leaves_project_note_untouched()
    test_record_qa_answer_persists_full_qa_pairs()
    test_record_qa_answer_with_heading_in_answer_does_not_corrupt_sections()
    test_record_qa_answer_missing_project_returns_false()
    test_list_projects_excludes_draft_and_conflict_files()
    test_list_projects_filters_by_status()
    test_list_projects_empty_dir_returns_empty_list()
    test_update_project_fields_detects_manual_edit_and_backs_up()
    test_save_final_draft_detects_manual_edit_on_draft_file()
    test_promote_and_reflect_creates_main_note_and_records_reflection()
    test_promote_and_reflect_is_idempotent_on_retry()
    test_promote_and_reflect_missing_draft_returns_error()
    test_promote_and_reflect_missing_project_returns_error()
    test_promote_and_reflect_falls_back_to_goal_when_title_missing()
    test_promote_and_reflect_neutralizes_heading_in_reflection_input()
    test_build_dashboard_text_empty_projects()
    test_build_dashboard_text_groups_by_status()
    test_build_dashboard_text_handles_missing_optional_fields()
    test_generate_x_post_draft_rejects_non_completed_status()
    test_generate_x_post_draft_missing_promoted_filename()
    test_generate_x_post_draft_missing_main_note_file()
    test_generate_x_post_draft_success_within_limit()
    test_generate_x_post_draft_without_url_uses_full_280_limit()
    test_generate_x_post_draft_with_url_appends_url_and_shrinks_body_limit()
    test_generate_x_post_draft_retries_once_when_over_limit()
    test_generate_x_post_draft_fails_when_still_over_limit_after_retry()
    test_generate_x_post_draft_gemini_exception_returns_error()
    test_record_x_post_result_updates_frontmatter()
    test_record_x_post_result_missing_file_returns_false()

    print("\n" + "=" * 60)
    print(f"結果: {len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("失敗:")
        for f in _FAIL:
            print(f"  - {f}")
    sys.exit(1 if _FAIL else 0)
