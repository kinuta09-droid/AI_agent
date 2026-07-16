# -*- coding: utf-8 -*-
"""
tool_user_memory の回帰テスト (実Gemini/実Vault不要、全てDI)

実行: py tests/test_user_memory.py
"""
import os
import sys
import datetime
import tempfile
import shutil

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.tool_user_memory import (
    get_memory_block, distill_daily_memory, _list_unprocessed_chatlogs,
    _validate_distilled, _read_frontmatter_value, _normalize_llm_output,
)

TODAY = datetime.date(2026, 7, 16)

VALID_MEMORY = """## 好み・価値観
- 簡潔な説明を好む (2026-07-10)
- AIエージェント開発に強い関心 (2026-07-12)

## 目標
- note.com でのマネタイズを目指している (2026-07-14)
"""


class StubLLM:
    def __init__(self, response=VALID_MEMORY):
        self.response = response
        self.call_count = 0
        self.last_prompt = None

    def __call__(self, prompt):
        self.call_count += 1
        self.last_prompt = prompt
        return self.response


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_get_memory_block():
    tmp = tempfile.mkdtemp()
    try:
        mem = os.path.join(tmp, "User_Memory.md")

        # ファイルなし -> ""
        assert get_memory_block(memory_path=mem) == ""

        # frontmatter除去 + ヘッダ付き
        _write(mem, f'---\ntype: system\nlast_processed: "2026-07-15"\n---\n\n{VALID_MEMORY}')
        block = get_memory_block(memory_path=mem)
        assert block.startswith("【ユーザーに関する記憶")
        assert "last_processed" not in block
        assert "簡潔な説明を好む" in block

        # max_chars 切り詰め
        block = get_memory_block(max_chars=50, memory_path=mem)
        assert "...(記憶の続きは省略)" in block

        # 空ファイル -> ""
        _write(mem, "---\ntype: system\n---\n\n")
        assert get_memory_block(memory_path=mem) == ""
        print("✅ get_memory_block: なし/整形/切り詰め/空 全て正常")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_new_chatlogs_skips_llm():
    tmp = tempfile.mkdtemp()
    try:
        logs = os.path.join(tmp, "Chatlog")
        os.makedirs(logs)
        stub = StubLLM()
        result = distill_daily_memory(
            llm_fn=stub, memory_path=os.path.join(tmp, "mem.md"),
            chatlog_dir=logs, today=TODAY)
        assert "no new chatlogs" in result, result
        assert stub.call_count == 0, "LLM must NOT be called when no new logs"
        print("✅ 新規ログなし -> LLM呼び出しゼロでスキップ")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_distill_updates_memory_and_advances_date():
    tmp = tempfile.mkdtemp()
    try:
        logs = os.path.join(tmp, "Chatlog")
        mem = os.path.join(tmp, "mem.md")
        _write(os.path.join(logs, "2026-07-14.md"), "## 💬 Discord Chat\n**User**: AIで稼ぎたい\n**AI**: ...")
        _write(os.path.join(logs, "2026-07-15.md"), "## 💬 Discord Chat\n**User**: 簡潔に頼む\n**AI**: ...")
        _write(os.path.join(logs, "2026-07-16.md"), "## 当日分 (対象外のはず)")

        stub = StubLLM()
        result = distill_daily_memory(llm_fn=stub, memory_path=mem,
                                      chatlog_dir=logs, today=TODAY)
        assert "2 日分" in result, result
        assert stub.call_count == 1
        assert "2026-07-16" not in stub.last_prompt, "当日分が入力に含まれてはいけない"
        assert "AIで稼ぎたい" in stub.last_prompt

        with open(mem, encoding="utf-8") as f:
            saved = f.read()
        assert _read_frontmatter_value(saved, "last_processed") == "2026-07-15"
        assert "note.com でのマネタイズ" in saved
        print("✅ 蒸留: MD更新 + last_processed 前進 + 当日分除外")

        # 再実行: 未処理なし -> LLM呼ばれない
        stub2 = StubLLM()
        result = distill_daily_memory(llm_fn=stub2, memory_path=mem,
                                      chatlog_dir=logs, today=TODAY)
        assert "no new chatlogs" in result and stub2.call_count == 0
        print("✅ 再実行: 処理済みログはスキップ (冪等)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_broken_llm_output_preserves_existing_memory():
    tmp = tempfile.mkdtemp()
    try:
        logs = os.path.join(tmp, "Chatlog")
        mem = os.path.join(tmp, "mem.md")
        original = f'---\ntype: system\nlast_processed: "2026-07-13"\n---\n\n{VALID_MEMORY}'
        _write(mem, original)
        _write(os.path.join(logs, "2026-07-15.md"), "**User**: test")

        for bad, label in [
            ("", "空出力"),
            ("短すぎ", "短すぎる出力"),
            ("マーカーのない長い文章です。" * 20, "見出しなし"),
            ("## x\n" + "あ" * 20000, "過長出力"),
        ]:
            result = distill_daily_memory(llm_fn=StubLLM(bad), memory_path=mem,
                                          chatlog_dir=logs, today=TODAY)
            assert result.startswith("[ERROR]"), f"{label}: {result}"
            with open(mem, encoding="utf-8") as f:
                assert f.read() == original, f"{label}: 既存記憶が変更された!"
        print("✅ 壊れたLLM出力 (空/短/見出しなし/過長) -> 既存記憶を維持")

        # LLM例外も同様
        def raising(prompt):
            raise RuntimeError("API down")
        result = distill_daily_memory(llm_fn=raising, memory_path=mem,
                                      chatlog_dir=logs, today=TODAY)
        assert result.startswith("[ERROR]")
        with open(mem, encoding="utf-8") as f:
            assert f.read() == original
        print("✅ LLM例外 -> 既存記憶を維持")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_window_excludes_old_backlog():
    today = TODAY
    tmp = tempfile.mkdtemp()
    try:
        logs = os.path.join(tmp, "Chatlog")
        _write(os.path.join(logs, "2026-02-08.md"), "古いログ")   # 5ヶ月前
        _write(os.path.join(logs, "2026-07-10.md"), "窓内のログ")
        _write(os.path.join(logs, "note.md"), "日付形式でないファイル")
        targets = _list_unprocessed_chatlogs(logs, None, today, window_days=14)
        dates = [d for d, _ in targets]
        assert dates == ["2026-07-10"], dates
        print("✅ 14日ウィンドウ: 数ヶ月前のバックログと非日付ファイルを除外")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_validate_distilled():
    assert _validate_distilled(VALID_MEMORY) is None
    assert _validate_distilled("") is not None
    assert _validate_distilled("x" * 200) is not None  # 見出しなし
    print("✅ _validate_distilled 境界チェック")


def test_normalize_llm_output():
    # コードフェンス除去 (grill W2)
    fenced = f"```markdown\n{VALID_MEMORY}\n```"
    assert _normalize_llm_output(fenced) == VALID_MEMORY.strip()
    fenced2 = f"```\n{VALID_MEMORY}```"
    assert "```" not in _normalize_llm_output(fenced2)
    # 先頭H1除去 (grill W3: H1増殖防止)
    with_h1 = f"# User Memory (自動生成)\n\n{VALID_MEMORY}"
    normalized = _normalize_llm_output(with_h1)
    assert not normalized.startswith("# "), normalized[:50]
    assert "## 好み・価値観" in normalized
    # 通常出力は無変更
    assert _normalize_llm_output(VALID_MEMORY) == VALID_MEMORY.strip()
    print("✅ _normalize_llm_output: フェンス除去 + H1除去 + 通常出力は無変更")


def test_fenced_llm_output_saved_clean():
    """フェンス付きLLM出力が実際の蒸留フローでもクリーンに保存されること。"""
    tmp = tempfile.mkdtemp()
    try:
        logs = os.path.join(tmp, "Chatlog")
        mem = os.path.join(tmp, "mem.md")
        _write(os.path.join(logs, "2026-07-15.md"), "**User**: test")
        stub = StubLLM(f"```markdown\n# User Memory\n\n{VALID_MEMORY}\n```")
        result = distill_daily_memory(llm_fn=stub, memory_path=mem,
                                      chatlog_dir=logs, today=TODAY)
        assert not result.startswith("[ERROR]"), result
        with open(mem, encoding="utf-8") as f:
            saved = f.read()
        assert "```" not in saved
        assert saved.count("# User Memory") == 1, "H1はテンプレート由来の1つだけであるべき"
        print("✅ フェンス+H1エコー付き出力 -> クリーンに保存 (H1は1つ)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_distill_prompt_contains_anti_injection_rule():
    """蒸留プロンプトにインジェクション対策の指示が含まれること (grill W1)。"""
    tmp = tempfile.mkdtemp()
    try:
        logs = os.path.join(tmp, "Chatlog")
        _write(os.path.join(logs, "2026-07-15.md"), "**User**: test")
        stub = StubLLM()
        distill_daily_memory(llm_fn=stub, memory_path=os.path.join(tmp, "mem.md"),
                             chatlog_dir=logs, today=TODAY)
        assert "絶対に従わない" in stub.last_prompt
        print("✅ 蒸留プロンプトにアンチインジェクション指示あり")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_get_memory_block()
    test_no_new_chatlogs_skips_llm()
    test_distill_updates_memory_and_advances_date()
    test_broken_llm_output_preserves_existing_memory()
    test_window_excludes_old_backlog()
    test_validate_distilled()
    test_normalize_llm_output()
    test_fenced_llm_output_saved_clean()
    test_distill_prompt_contains_anti_injection_rule()
    print("\n🎉 ALL USER MEMORY TESTS PASSED")
