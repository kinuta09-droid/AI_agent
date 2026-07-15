# -*- coding: utf-8 -*-
"""
_truncate_preserving_xtrend の回帰テスト (2026-07-16)

背景: agent_scheduler._run_morning_briefing が単純な content[:1900] で
切り詰めていたため、Gemini要約が長い日に末尾のXTrendセクションが
丸ごと/一部消えるバグがあった。XTrend優先保持ロジックを検証する。

実行: py tests/test_briefing_truncation.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agent_scheduler import _truncate_preserving_xtrend

XTREND_BLOCK = (
    "\n---\n\n## 🔥 X Trend Top 3 — ARMS導入候補 (2026-07-16)\n\n"
    "1. **トピックA** — 説明文。\n"
    "2. **トピックB** — 説明文。\n"
    "3. **トピックC** — 説明文。\n"
)


def test_short_content_unchanged():
    content = "短い本文\n" + XTREND_BLOCK
    result = _truncate_preserving_xtrend(content, max_len=1900)
    assert result == content
    print("✅ 上限以内のコンテンツは無変更")


def test_long_briefing_text_truncated_but_xtrend_survives():
    long_summary = "これはとても長いブリーフィング本文です。" * 200  # 2000+文字
    content = long_summary + XTREND_BLOCK
    assert len(content) > 1900
    result = _truncate_preserving_xtrend(content, max_len=1900)
    assert "## 🔥 X Trend Top 3" in result, "XTrend section must survive truncation"
    assert "トピックA" in result and "トピックB" in result and "トピックC" in result
    assert len(result) <= 1900 + len("...(続きは省略)\n\n") + 50
    print(f"✅ 長い本文({len(content)}字)でもXTrendは完全に保持される (結果{len(result)}字)")


def test_no_marker_falls_back_to_simple_truncation():
    content = "マーカーなしの長文。" * 300
    assert len(content) > 1900
    result = _truncate_preserving_xtrend(content, max_len=1900)
    assert result.endswith("... (truncated)")
    assert len(result) <= 1900 + len("... (truncated)")
    print("✅ XTrendマーカーがない場合は従来通りの単純切り詰め")


def test_extreme_case_xtrend_alone_exceeds_budget():
    huge_xtrend = "## 🔥 X Trend Top 3\n" + ("非常に長い説明。" * 500)
    content = "本文\n" + huge_xtrend
    result = _truncate_preserving_xtrend(content, max_len=1900)
    # budget < 100 のフォールバック分岐: 単純切り詰めになる
    assert result.endswith("... (truncated)")
    print("✅ XTrend自体が予算を超える極端なケースでもクラッシュしない")


if __name__ == "__main__":
    test_short_content_unchanged()
    test_long_briefing_text_truncated_but_xtrend_survives()
    test_no_marker_falls_back_to_simple_truncation()
    test_extreme_case_xtrend_alone_exceeds_budget()
    print("\n🎉 ALL BRIEFING TRUNCATION TESTS PASSED")
