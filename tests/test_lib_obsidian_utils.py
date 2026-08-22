# -*- coding: utf-8 -*-
"""tests/test_lib_obsidian_utils.py — lib/lib_obsidian_utils.py の単体テスト

対象: read_frontmatter_field (plans/obsidian-agent-native-notes.md)、
      extract_note_query (commands/bot_commands_idea.py から移設、
      tools/tool_orphan_relink.py と共用)

検証する不変条件:
  [1] 正常系: frontmatter内の `key: value` を読める
  [2] キー未設定: None を返す (例外を投げない)
  [3] frontmatter自体が無いノート: None を返す
  [4] ファイル未存在: None を返す (例外を投げない)
  [5] head_bytes で打ち切った先にキーがある場合は拾えない (仕様どおり)
  [6] 値の前後空白・複数行値は最初の空白区切りトークンのみ拾う
  [7] extract_note_query: SUMMARY calloutがあればその中身 (複数行を結合) を返す
  [8] extract_note_query: calloutが無ければ本文先頭 max_chars 文字にフォールバックする
  [9] extract_note_query: 空文字/None は例外を投げず空文字を返す

本番ファイルには一切触れない (tempfile のみ使う)。
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.lib_obsidian_utils import read_frontmatter_field, extract_note_query  # noqa: E402


def _write(tmp, name, content):
    path = os.path.join(tmp, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


# ---------------------------------------------------------
# [1] 正常系
# ---------------------------------------------------------

def test_read_frontmatter_field_basic():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "A.md", "---\nstatus: open\ndate: 2026-08-14\n---\n\nBody\n")
        assert read_frontmatter_field(path, "status") == "open"
        assert read_frontmatter_field(path, "date") == "2026-08-14"
    print("✅ [1] 正常系: key: value を読める")


# ---------------------------------------------------------
# [2] キー未設定
# ---------------------------------------------------------

def test_read_frontmatter_field_missing_key():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "A.md", "---\nstatus: open\n---\n\nBody\n")
        assert read_frontmatter_field(path, "topic") is None
    print("✅ [2] キー未設定は None")


# ---------------------------------------------------------
# [3] frontmatter自体が無い
# ---------------------------------------------------------

def test_read_frontmatter_field_no_frontmatter():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "A.md", "# Title\n\nBody without frontmatter\nstatus in body text\n")
        # 本文中に "status" という語があっても、行頭 "status:" でなければ拾わない
        assert read_frontmatter_field(path, "status") is None
    print("✅ [3] frontmatter が無いノートは None (本文中の紛らわしい語に反応しない)")


# ---------------------------------------------------------
# [4] ファイル未存在
# ---------------------------------------------------------

def test_read_frontmatter_field_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "does_not_exist.md")
        assert read_frontmatter_field(path, "status") is None
    print("✅ [4] ファイル未存在は None (例外を投げない)")


# ---------------------------------------------------------
# [5] head_bytes 打ち切り
# ---------------------------------------------------------

def test_read_frontmatter_field_respects_head_bytes():
    with tempfile.TemporaryDirectory() as tmp:
        padding = "x" * 600
        path = _write(tmp, "A.md", f"---\n{padding}\nstatus: open\n---\n\nBody\n")
        # デフォルト head_bytes=500 では status: 行に届かない
        assert read_frontmatter_field(path, "status", head_bytes=500) is None
        # 十分な head_bytes を渡せば読める
        assert read_frontmatter_field(path, "status", head_bytes=2000) == "open"
    print("✅ [5] head_bytes を超えた位置のキーは既定では拾えない (仕様どおり)")


# ---------------------------------------------------------
# [6] 値は最初の空白区切りトークンのみ
# ---------------------------------------------------------

def test_read_frontmatter_field_scalar_only():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "A.md", "---\ntopic: 新規事業 検討中\n---\n\nBody\n")
        assert read_frontmatter_field(path, "topic") == "新規事業"
    print("✅ [6] ブロック値/複数語の値は最初のトークンのみ (単一行スカラー専用の仕様どおり)")


# ---------------------------------------------------------
# [7]-[9] extract_note_query
# ---------------------------------------------------------

def test_extract_note_query_summary_callout():
    content = "本文冒頭\n\n> [!SUMMARY]\n> 1行目の要約\n> 2行目の要約\n\n### 関連ノート\n"
    assert extract_note_query(content) == "1行目の要約 2行目の要約"
    print("✅ [7] SUMMARY calloutの複数行を結合して返す")


def test_extract_note_query_fallback_to_head():
    content = "x" * 300
    assert extract_note_query(content) == "x" * 200
    assert extract_note_query(content, max_chars=10) == "x" * 10
    print("✅ [8] calloutが無ければ本文先頭 max_chars 文字にフォールバック")


def test_extract_note_query_empty_input():
    assert extract_note_query("") == ""
    assert extract_note_query(None) == ""
    print("✅ [9] 空文字/None は例外を投げず空文字を返す")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                failed += 1
                print(f"❌ {name}: {e}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    if failed:
        print(f"\nFAILURE: {failed}/{total} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {total} tests passed")
