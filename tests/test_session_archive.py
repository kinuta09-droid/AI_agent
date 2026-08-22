# -*- coding: utf-8 -*-
"""tests/test_session_archive.py — tools/tool_session_archive.py の回帰テスト

対象: tools/tool_session_archive.py の archive_chat_session()

検証する不変条件:
  [1] history がターン数閾値未満 (MIN_TURNS*2 未満) のときは、LLM (client.models.
      generate_content) を一切呼ばずに "Skipped: turns=..." を返す
  [2] LLM が should_save=false を返したときはファイルを作らず "Skipped: ..." を返す
  [3] LLM が should_save=true を返したときは SESSION_DIR 配下に .md ファイルを作成し、
      frontmatter (type: session)・タイトル見出し・会話ログが本文に含まれる
  [4] title に禁止文字 (\\ / : * ? " < > |) を含むとき、生成されるファイル名から
      それらの文字が除去される

本番Vault (D:\\マイドライブ\\Obsidian) 配下の 40_Report/Sessions/ には一切触れない。
tool_session_archive.SESSION_DIR を tempfile.mkdtemp() の一時ディレクトリへ差し替え、
try/finally で必ず元の値へ復元する (Category C の教訓通り、共有可変資源は必ず退避する)。

実行: py tests/test_session_archive.py
"""
import glob
import os
import sys
import tempfile
import shutil
from unittest import mock

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_session_archive as tsa  # noqa: E402

_PASS, _FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _resp(text: str):
    m = mock.Mock()
    m.text = text
    return m


def _history(turns: int) -> list:
    """turns 往復分の history を作る (user/model の交互list)。"""
    h = []
    for i in range(turns):
        h.append({"role": "user", "content": f"質問{i}"})
        h.append({"role": "model", "content": f"回答{i}"})
    return h


class _SessionDir:
    """tsa.SESSION_DIR を一時ディレクトリへ退避する。本番Vaultを汚染しない隔離が最重要。"""

    def __enter__(self):
        self._saved = tsa.SESSION_DIR
        self.root = tempfile.mkdtemp(prefix="session_archive_")
        tsa.SESSION_DIR = self.root
        return self

    def __exit__(self, *exc):
        tsa.SESSION_DIR = self._saved
        shutil.rmtree(self.root, ignore_errors=True)
        return False

    def files(self):
        return glob.glob(os.path.join(self.root, "*.md"))


# ---------------------------------------------------------
# [1] ターン数閾値未満はLLM呼び出し無しにスキップ
# ---------------------------------------------------------

def test_below_threshold_skips_without_llm_call():
    with mock.patch.object(tsa.client.models, "generate_content") as m:
        m.side_effect = AssertionError("generate_content が呼ばれてはいけない")
        result = tsa.archive_chat_session(_history(1))  # 1往復 = 2要素 < MIN_TURNS*2(=4)
        check("[1] LLMが呼ばれない", not m.called, m.call_args_list)
    check("[1] Skipped接頭辞", result.startswith("Skipped: turns=1"), result)
    print("--- [1] done ---")


# ---------------------------------------------------------
# [2] should_save=false はスキップ・ファイル未作成
# ---------------------------------------------------------

def test_should_save_false_skips_and_creates_no_file():
    with _SessionDir() as sd:
        with mock.patch.object(tsa.client.models, "generate_content") as m:
            m.return_value = _resp('{"should_save": false, "title": "", "summary": ""}')
            result = tsa.archive_chat_session(_history(2))  # 2往復 = 閾値ちょうど
        check("[2] Skipped接頭辞", result.startswith("Skipped:"), result)
        check("[2] ファイルが作成されない", sd.files() == [], sd.files())
    print("--- [2] done ---")


# ---------------------------------------------------------
# [3] 保存成功パス
# ---------------------------------------------------------

def test_save_success_writes_expected_content():
    with _SessionDir() as sd:
        with mock.patch.object(tsa.client.models, "generate_content") as m, \
             mock.patch("tools.tool_bitnet.generate_note_tags", return_value=["ai", "agent"]):
            m.return_value = _resp(
                '{"should_save": true, "title": "テストセッション", "summary": "要約テキスト"}')
            result = tsa.archive_chat_session(_history(3))

        check("[3] Success接頭辞", result.startswith("Success:"), result)
        files = sd.files()
        check("[3] .mdファイルが1件作成される", len(files) == 1, files)
        if files:
            with open(files[0], encoding="utf-8") as f:
                content = f.read()
            check("[3] frontmatterにtype: sessionを含む", "type: session" in content, content)
            check("[3] タイトル見出しを含む", "# テストセッション" in content, content)
            check("[3] 要約(引用)を含む", "> 要約テキスト" in content, content)
            check("[3] 会話ログセクションを含む", "## 会話ログ" in content, content)
            check("[3] 会話本文(User/AI形式)を含む",
                  "User: 質問0" in content and "AI: 回答0" in content, content)
            check("[3] タグ行を含む", "tags: [ai, agent]" in content, content)
            check("[3] turnsがhistoryの往復数と一致", "turns: 3" in content, content)
    print("--- [3] done ---")


# ---------------------------------------------------------
# [5] LLM呼び出し自体が例外 (API障害・予算超過) のとき Error文字列を返す
# ---------------------------------------------------------

def test_llm_call_failure_returns_error_string():
    with _SessionDir() as sd:
        with mock.patch.object(tsa.client.models, "generate_content") as m:
            m.side_effect = RuntimeError("budget exceeded")
            result = tsa.archive_chat_session(_history(2))
        check("[5] Error接頭辞", result.startswith("Error:"), result)
        from lib.bot_errors import is_failure_result
        check("[5] is_failure_resultがTrueと判定", is_failure_result(result), result)
        check("[5] ファイルが作成されない", sd.files() == [], sd.files())
    print("--- [5] done ---")


# ---------------------------------------------------------
# [4] ファイル名サニタイズ
# ---------------------------------------------------------

def test_filename_sanitizes_forbidden_characters():
    with _SessionDir() as sd:
        with mock.patch.object(tsa.client.models, "generate_content") as m, \
             mock.patch("tools.tool_bitnet.generate_note_tags", return_value=[]):
            m.return_value = _resp(
                '{"should_save": true, "title": "A/B:C*D?E\\"F<G>H|I", "summary": "s"}')
            result = tsa.archive_chat_session(_history(2))

        check("[4] Success接頭辞", result.startswith("Success:"), result)
        files = sd.files()
        check("[4] .mdファイルが1件作成される", len(files) == 1, files)
        if files:
            basename = os.path.basename(files[0])
            forbidden = set('\\/:*?"<>|')
            check("[4] ファイル名に禁止文字を含まない",
                  not (forbidden & set(basename)), basename)
    print("--- [4] done ---")


if __name__ == "__main__":
    test_below_threshold_skips_without_llm_call()
    test_should_save_false_skips_and_creates_no_file()
    test_save_success_writes_expected_content()
    test_llm_call_failure_returns_error_string()
    test_filename_sanitizes_forbidden_characters()

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        for f in _FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    sys.exit(0)
