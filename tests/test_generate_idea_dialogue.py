# -*- coding: utf-8 -*-
"""
scripts/generate_idea_dialogue.py のユニットテスト

方針: Gemini 呼び出しは実行しない (CLAUDE.md: テストを一周回すだけで課金や実副作用が
起きてはいけない)。lib_shared.client.models.generate_content をスタブに差し替える
(tests/test_paper_radar.py と同じ差し替え方)。

実行: py tests/test_generate_idea_dialogue.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import scripts.generate_idea_dialogue as dialogue
import lib.lib_shared as lib_shared


class _FakeResp:
    def __init__(self, text):
        self.text = text


def _stub_generate_content(fn):
    """generate_content を差し替えるコンテキストマネージャ代わりのヘルパー。"""
    class _Ctx:
        def __enter__(self):
            self.original = lib_shared.client.models.generate_content
            lib_shared.client.models.generate_content = fn
        def __exit__(self, *exc):
            lib_shared.client.models.generate_content = self.original
    return _Ctx()


def test_generate_next_question_returns_stripped_text():
    with _stub_generate_content(lambda model, contents: _FakeResp("  次の質問は？  ")):
        q = dialogue.generate_next_question("雑なアイデア", [])
    assert q == "次の質問は？", q
    print("✅ [1] generate_next_question: レスポンスをそのまま返す（前後空白は除去）")


def test_generate_next_question_includes_history_in_prompt():
    captured = {}
    def _capture(model, contents):
        captured["contents"] = contents
        return _FakeResp("次の一手は？")

    turns = [{"question": "最初の質問", "answer": "最初の回答"}]
    with _stub_generate_content(_capture):
        dialogue.generate_next_question("元のアイデア", turns)

    assert "最初の質問" in captured["contents"]
    assert "最初の回答" in captured["contents"]
    assert "元のアイデア" in captured["contents"]
    print("✅ [2] generate_next_question: これまでの対話をプロンプトへ含める")


def test_generate_next_question_failure_returns_empty_string():
    def _boom(model, contents):
        raise RuntimeError("quota exceeded")
    with _stub_generate_content(_boom):
        q = dialogue.generate_next_question("雑なアイデア", [])
    assert q == "", q
    print("✅ [3] generate_next_question: 失敗時は例外を外へ出さず空文字")


def test_summarize_idea_session_parses_json():
    fake_json = (
        '{"title": "テストタイトル", "summary": "1行要約", '
        '"content": "# 本文\\n詳細"}'
    )
    with _stub_generate_content(lambda model, contents: _FakeResp(fake_json)):
        result = dialogue.summarize_idea_session("種", [{"question": "Q", "answer": "A"}])

    assert result == {
        "title": "テストタイトル", "summary": "1行要約", "content": "# 本文\n詳細",
    }, result
    print("✅ [4] summarize_idea_session: JSONをdictへパースする")


def test_summarize_idea_session_strips_code_fence():
    fenced = '```json\n{"title": "T", "summary": "S", "content": "C"}\n```'
    with _stub_generate_content(lambda model, contents: _FakeResp(fenced)):
        result = dialogue.summarize_idea_session("種", [])
    assert result["title"] == "T", result
    print("✅ [5] summarize_idea_session: ```json フェンス付きでもパースできる")


def test_summarize_idea_session_failure_returns_empty_dict():
    def _boom(model, contents):
        raise RuntimeError("network error")
    with _stub_generate_content(_boom):
        result = dialogue.summarize_idea_session("種", [])
    assert result == {}, result
    print("✅ [6] summarize_idea_session: 失敗時は例外を外へ出さず空dict")


def test_summarize_idea_session_invalid_json_returns_empty_dict():
    with _stub_generate_content(lambda model, contents: _FakeResp("not json at all")):
        result = dialogue.summarize_idea_session("種", [])
    assert result == {}, result
    print("✅ [7] summarize_idea_session: 壊れたJSONでも例外を投げず空dict")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
