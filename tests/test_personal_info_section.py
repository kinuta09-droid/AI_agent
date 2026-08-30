# -*- coding: utf-8 -*-
"""
User_Profile.md の「## パーソナル情報」セクション隔離の回帰テスト。

tool_obsidian._strip_section (純関数) が正しく該当セクションだけを除去すること、
および tool_future_search._load_user_context がそれを実際に使って User_Profile.md の
パーソナル情報をGeminiコンテキストから除外することを確認する (実LLM/実Vault不要)。

実行: py tests/test_personal_info_section.py
"""
import os
import sys
import tempfile
import shutil
import io
import contextlib

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_obsidian import _strip_section, _strip_section_or_warn, PERSONAL_INFO_SECTION_HEADING
import tools.tool_future_search as fs


def test_strip_section_removes_body_until_next_heading():
    text = "# H1\n\n## パーソナル情報\n本名: 例\n\n## 2026-02-08\n- **Q**: x\n"
    out = _strip_section(text, "パーソナル情報")
    assert "パーソナル情報" not in out
    assert "本名: 例" not in out
    assert "## 2026-02-08" in out and "- **Q**: x" in out
    print("✅ 次の見出しまでを除去し、以降のセクションは残す")


def test_strip_section_removes_to_eof_when_last_section():
    text = "# H1\n\n## A\nkeep\n\n## パーソナル情報\n秘密の内容\n最終行"
    out = _strip_section(text, "パーソナル情報")
    assert "秘密の内容" not in out and "最終行" not in out
    assert "## A\nkeep" in out
    print("✅ 末尾セクションでもEOFまで除去する")


def test_strip_section_noop_when_heading_absent():
    text = "# H1\n\n## A\nkeep\n"
    assert _strip_section(text, "パーソナル情報") == text
    print("✅ 見出しが無ければ非破壊")


def test_strip_section_removes_all_duplicate_headings():
    text = (
        "## パーソナル情報\n秘密1\n\n"
        "## A\nkeep\n\n"
        "## パーソナル情報\n秘密2\n"
    )
    out = _strip_section(text, "パーソナル情報")
    assert "秘密1" not in out and "秘密2" not in out
    assert "## A\nkeep" in out
    print("✅ 同名見出しが複数あっても全て除去する")


def test_strip_section_or_warn_prints_when_format_breaks():
    # 見出し行に絵文字が続くと正規表現が一致せず非破壊のまま返る -> 警告が出ること
    text = "## パーソナル情報 📝\n漏れたら困る内容\n"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = _strip_section_or_warn(text, "パーソナル情報", "TestLabel")
    assert "漏れたら困る内容" in out, "この検証は「除去できない書式」を再現するためのもの"
    printed = buf.getvalue()
    assert "[TestLabel]" in printed and "除去できませんでした" in printed
    print("✅ 書式崩れで除去に失敗した場合は警告ログを出す")


def test_strip_section_or_warn_silent_when_heading_absent():
    text = "# H1\n\n## A\nkeep\n"
    out = _strip_section_or_warn(text, "パーソナル情報", "TestLabel")
    assert out == text
    print("✅ 見出しが元々無ければ警告なし")


def test_future_search_load_user_context_excludes_personal_info():
    tmp = tempfile.mkdtemp(prefix="fs_profile_vault_")
    try:
        sysdir = os.path.join(tmp, "00_System")
        os.makedirs(sysdir, exist_ok=True)
        profile = (
            "# User Profile\n\n"
            f"## {PERSONAL_INFO_SECTION_HEADING}\n"
            "実家の住所: 極秘\n\n"
            "## 2026-02-08 21:48\n"
            "- **Q**: 好みは?\n- **A**: 実装重視\n"
        )
        with open(os.path.join(sysdir, "User_Profile.md"), "w", encoding="utf-8") as f:
            f.write(profile)

        saved_vault_root = fs.VAULT_ROOT
        fs.VAULT_ROOT = tmp
        try:
            ctx = fs._load_user_context()
        finally:
            fs.VAULT_ROOT = saved_vault_root

        assert "極秘" not in ctx, "パーソナル情報セクションがGeminiコンテキストに漏れている"
        assert "実装重視" in ctx, "既存のA/B質問ログは従来通り含まれるべき"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ FutureSearchのユーザー文脈からパーソナル情報セクションが除外される")


if __name__ == "__main__":
    test_strip_section_removes_body_until_next_heading()
    test_strip_section_removes_to_eof_when_last_section()
    test_strip_section_noop_when_heading_absent()
    test_strip_section_removes_all_duplicate_headings()
    test_strip_section_or_warn_prints_when_format_breaks()
    test_strip_section_or_warn_silent_when_heading_absent()
    test_future_search_load_user_context_excludes_personal_info()
    print("\n🎉 ALL PERSONAL INFO SECTION TESTS PASSED")
