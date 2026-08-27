# -*- coding: utf-8 -*-
"""tests/test_briefing_manager.py — Briefing/Dailynote統合 (2026-08-26) の回帰テスト

背景: tool_generate_briefing() の保存先を 30_Main/Briefing/Briefing_{date}.md
(タイムスタンプ衝突回避付き) から 01_Daily/{date}.md (1日1ファイル固定・毎回上書き)
に変更し、NEWSセクションを新設した。

検証する不変条件:
  [1] 01_Daily/{today}.md に保存され、初回実行にはタイムスタンプサフィックスが付かない
  [2] 同日に2回目を実行すると、既存の {today}.md は上書きされず
      {today}_{HHMM}.md が別ファイルとして増える (tool_update_note で追記された
      当日の本文を、Briefing再実行が無警告で破壊しないため)
  [3] NEWSセクション見出し (## 📰 今日のニュース) が出力に含まれる
  [4] フロントマターに tags: / type/daily が含まれ、旧 type: briefing は含まれない

実 Vault・実 API (Gemini/ccxt/gws/Gmail) には一切触れない
(_providers・_call_gemini・_build_*_section を全てスタブに差し替える)。

実行: py tests/test_briefing_manager.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.tool_briefing_manager as tbm  # noqa: E402


class _Isolated:
    """一時 Vault を作り、tool_briefing_manager の外部依存を全てスタブへ差し替える。

    tool_briefing_manager は `from lib.lib_shared import (..., VAULT_ROOT, ...)` の形で
    値コピー束縛しているため、差し替えは tools.tool_briefing_manager.VAULT_ROOT
    (このモジュール自身の名前空間) に対して行う (lib.lib_shared.VAULT_ROOT を書き換えても
    連動しない、既知の罠)。
    """

    def __init__(self, news_section="", providers=None):
        self.root = tempfile.mkdtemp(prefix="briefing_vault_")
        self._news_section = news_section
        self._providers = providers if providers is not None else {"Test": lambda: "dummy"}

    def __enter__(self):
        self._saved = {
            "VAULT_ROOT": tbm.VAULT_ROOT,
            "_providers": tbm._providers,
            "_call_gemini": tbm._call_gemini,
            "_build_xtrend_section": tbm._build_xtrend_section,
            "_build_resurfacing_section": tbm._build_resurfacing_section,
            "_build_news_section": tbm._build_news_section,
        }
        tbm.VAULT_ROOT = self.root
        tbm._providers = self._providers
        tbm._call_gemini = lambda prompt: "- ダミー要約1\n- ダミー要約2\n- ダミー要約3"
        tbm._build_xtrend_section = lambda top_n=3: ""
        tbm._build_resurfacing_section = lambda: ""
        tbm._build_news_section = lambda today_str: self._news_section
        return self

    def __exit__(self, exc_type, exc, tb):
        for k, v in self._saved.items():
            setattr(tbm, k, v)
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


_FAIL = 0


def check(name, condition, detail=""):
    global _FAIL
    if condition:
        print(f"✅ {name}")
    else:
        _FAIL += 1
        print(f"❌ {name}: {detail}")


def _today_filename():
    import datetime
    return datetime.date.today().strftime("%Y-%m-%d") + ".md"


def test_saves_to_01_daily_no_timestamp_suffix():
    with _Isolated() as env:
        tbm.tool_generate_briefing()
        daily_dir = os.path.join(env.root, "01_Daily")
        files = os.listdir(daily_dir) if os.path.isdir(daily_dir) else []
        expected = _today_filename()
        check("[1] 01_Daily/{today}.md に保存される", expected in files, files)
        suffixed = [f for f in files if f != expected and f.startswith(expected[:-3])]
        check("[1] タイムスタンプサフィックス付きファイルが無い", not suffixed, suffixed)


def test_second_run_preserves_first_file():
    with _Isolated() as env:
        tbm.tool_generate_briefing()
        daily_dir = os.path.join(env.root, "01_Daily")
        expected = _today_filename()
        with open(os.path.join(daily_dir, expected), "r", encoding="utf-8") as f:
            first_content = f.read()

        tbm.tool_generate_briefing()
        files = os.listdir(daily_dir)
        check("[2] 2回目で新しい別ファイルが増える", len(files) == 2, files)
        check("[2] 1回目のファイルは上書きされず内容不変", (
            open(os.path.join(daily_dir, expected), "r", encoding="utf-8").read()
            == first_content
        ))
        suffixed = [f for f in files if f != expected]
        check("[2] 2回目はタイムスタンプ付き別名で保存される",
              len(suffixed) == 1 and suffixed[0].startswith(expected[:-3] + "_"),
              suffixed)


def test_news_section_included():
    with _Isolated(news_section="\n## 📰 今日のニュース\n> ダミーニュース\n") as env:
        tbm.tool_generate_briefing()
        filepath = os.path.join(env.root, "01_Daily", _today_filename())
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        check("[3] NEWSセクション見出しが出力に含まれる", "## 📰 今日のニュース" in content, content)


def test_frontmatter_tags_type_daily():
    with _Isolated() as env:
        tbm.tool_generate_briefing()
        filepath = os.path.join(env.root, "01_Daily", _today_filename())
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        check("[4] フロントマターに tags: がある", "tags:" in content, content)
        check("[4] フロントマターに type/daily がある", "type/daily" in content, content)
        check("[4] 旧 type: briefing は含まれない", "type: briefing" not in content, content)


def test_get_obsidian_updates_skips_frontmatter():
    """[5] frontmatterがプレビュー500字を食い潰し本文が丸ごと消えるバグの回帰テスト。"""
    with _Isolated() as env:
        main_dir = os.path.join(env.root, "30_Main")
        os.makedirs(main_dir, exist_ok=True)
        frontmatter = "---\n" + "\n".join(f"key{i}: value{i}" for i in range(60)) + "\n---\n"
        note_path = os.path.join(main_dir, "long_note.md")
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(frontmatter + "# 本文タイトル\n本文の中身がここに続きます。")
        result = tbm.get_obsidian_updates()
        check("[5] frontmatter除去後の本文が含まれる", "本文の中身がここに続きます" in result, result)


def test_get_obsidian_updates_is_collapsed_callout():
    """[6] 新規ノートプレビューがObsidian callout構文で初期折りたたみになっている。"""
    with _Isolated() as env:
        main_dir = os.path.join(env.root, "30_Main")
        os.makedirs(main_dir, exist_ok=True)
        note_path = os.path.join(main_dir, "note.md")
        with open(note_path, "w", encoding="utf-8") as f:
            f.write("本文1行目\n本文2行目")
        result = tbm.get_obsidian_updates()
        check("[6] callout見出し ([!note]-) を含む", "> [!note]- note.md" in result, result)
        check("[6] 本文各行が `> ` で引用されている", "> 本文1行目" in result and "> 本文2行目" in result, result)


def test_get_obsidian_updates_handles_huge_frontmatter():
    """[7] frontmatterが2000字を超えても本文プレビューが潰されない (/grill所見1)。"""
    with _Isolated() as env:
        main_dir = os.path.join(env.root, "30_Main")
        os.makedirs(main_dir, exist_ok=True)
        frontmatter = "---\n" + "\n".join(f"key{i}: value{i}" for i in range(400)) + "\n---\n"
        note_path = os.path.join(main_dir, "huge_note.md")
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(frontmatter + "本文の中身がここに続きます。")
        result = tbm.get_obsidian_updates()
        check("[7] 巨大frontmatterでも本文が含まれる", "本文の中身がここに続きます" in result, result)


def test_collapsed_callout_empty_body_has_placeholder():
    """[8] 本文が空のcalloutが空の折りたたみにならない (/grill所見2)。"""
    result = tbm._collapsed_callout("empty.md", "")
    check("[8] 空本文には (内容なし) プレースホルダが入る", "(内容なし)" in result, result)


if __name__ == "__main__":
    test_saves_to_01_daily_no_timestamp_suffix()
    test_second_run_preserves_first_file()
    test_news_section_included()
    test_frontmatter_tags_type_daily()
    test_get_obsidian_updates_skips_frontmatter()
    test_get_obsidian_updates_is_collapsed_callout()
    test_get_obsidian_updates_handles_huge_frontmatter()
    test_collapsed_callout_empty_body_has_placeholder()
    if _FAIL:
        print(f"\nFAILURE: {_FAIL} check(s) failed")
        sys.exit(1)
    print("\nSUCCESS: all checks passed")
    sys.exit(0)
