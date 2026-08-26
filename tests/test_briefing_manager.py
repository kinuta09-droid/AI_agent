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


if __name__ == "__main__":
    test_saves_to_01_daily_no_timestamp_suffix()
    test_second_run_preserves_first_file()
    test_news_section_included()
    test_frontmatter_tags_type_daily()
    if _FAIL:
        print(f"\nFAILURE: {_FAIL} check(s) failed")
        sys.exit(1)
    print("\nSUCCESS: all checks passed")
    sys.exit(0)
