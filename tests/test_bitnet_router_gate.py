# -*- coding: utf-8 -*-
"""
test_bitnet_router_gate.py — 施策① BitNet二段ルーティングの一次判定ゲート単体テスト

対象: tools.tool_bitnet.route_tool_confident()
方針:
  - _run_bitnet() をモックするため exe / モデル不在の Linux/CI 環境でも動く。
  - 「高確信のときだけツール名、それ以外は None+理由」という確信度シグナルの
    分岐（clean_match / catchall / ambiguous / empty_output / error / no_tools）を
    網羅する。ここが崩れると Gemini へのフォールバックが効かずサイレント劣化する。

実行:
  py -m pytest tests/test_bitnet_router_gate.py -q
  （pytest 不在時）py tests/test_bitnet_router_gate.py
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools import tool_bitnet
from tools.tool_bitnet import route_tool_confident


# テスト用の代表的なツール集合（main.TOOLS の名前を模す）
TOOL_NAMES = [
    "daily_news",
    "crypto_analysis",
    "market_scan",
    "discord_bot",
    "scheduler",
    "agent",  # catchall
]
TOOL_DESCS = {
    "daily_news": "最新ニュースを収集・要約",
    "crypto_analysis": "仮想通貨の詳細分析",
    "market_scan": "主要銘柄一括スキャン",
    "discord_bot": "Discord Botを起動する",
    "scheduler": "スケジューラーを起動する",
    "agent": "AIエージェントに自律実行させる",
}


def _bitnet_returns(text):
    """_run_bitnet の戻り値 dict を模すヘルパ。"""
    return {"text": text, "elapsed_sec": 0.42, "tokens_per_sec": 12.3}


class TestRouteToolConfident(unittest.TestCase):
    # ── 高確信ケース: Gemini をスキップしてよい ──────────────────────
    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_clean_forward_match(self, m):
        """出力先頭トークンが実ツール名に forward 一致 → (tool, clean_match)。"""
        m.return_value = _bitnet_returns("market_scan")
        tool, reason = route_tool_confident("ざっと相場を見たい", TOOL_NAMES, TOOL_DESCS)
        self.assertEqual(tool, "market_scan")
        self.assertEqual(reason, "clean_match")

    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_clean_match_with_trailing_punct_and_extra_tokens(self, m):
        """先頭トークンの末尾約物と後続説明を無視して一致させる。"""
        m.return_value = _bitnet_returns("daily_news. This is the best tool.")
        tool, reason = route_tool_confident("ニュース教えて", TOOL_NAMES, TOOL_DESCS)
        self.assertEqual(tool, "daily_news")
        self.assertEqual(reason, "clean_match")

    # ── 低確信ケース: None を返し呼び出し側で Gemini へエスカレーション ──
    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_catchall_escalates(self, m):
        """catchall(agent) に一致しても高確信扱いせず None+catchall。"""
        m.return_value = _bitnet_returns("agent")
        tool, reason = route_tool_confident("なんかいい感じにやって", TOOL_NAMES, TOOL_DESCS)
        self.assertIsNone(tool)
        self.assertEqual(reason, "catchall")

    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_ambiguous_no_match_escalates(self, m):
        """どのツール名にも一致しない出力 → None+ambiguous。"""
        m.return_value = _bitnet_returns("banana")
        tool, reason = route_tool_confident("？？？", TOOL_NAMES, TOOL_DESCS)
        self.assertIsNone(tool)
        self.assertEqual(reason, "ambiguous")

    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_empty_output_escalates(self, m):
        """空文字出力 → None+empty_output。"""
        m.return_value = _bitnet_returns("   \n  ")
        tool, reason = route_tool_confident("何か", TOOL_NAMES, TOOL_DESCS)
        self.assertIsNone(tool)
        self.assertEqual(reason, "empty_output")

    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_none_text_escalates(self, m):
        """text が None（キー欠落相当）でも例外を出さず empty_output。"""
        m.return_value = {"text": None, "elapsed_sec": 0.1}
        tool, reason = route_tool_confident("何か", TOOL_NAMES, TOOL_DESCS)
        self.assertIsNone(tool)
        self.assertEqual(reason, "empty_output")

    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_exec_error_escalates(self, m):
        """_run_bitnet が例外（exe不在/subprocess失敗）→ None+error:...。"""
        m.side_effect = FileNotFoundError("llama-cli.exe not found")
        tool, reason = route_tool_confident("何か", TOOL_NAMES, TOOL_DESCS)
        self.assertIsNone(tool)
        self.assertTrue(reason.startswith("error:"))

    def test_no_tools_escalates(self):
        """ツール集合が空 → BitNet を呼ばず None+no_tools。"""
        with mock.patch.object(tool_bitnet, "_run_bitnet") as m:
            tool, reason = route_tool_confident("何か", [])
            self.assertIsNone(tool)
            self.assertEqual(reason, "no_tools")
            m.assert_not_called()

    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_works_without_descriptions(self, m):
        """tool_descriptions 省略でも forward 一致で動く。"""
        m.return_value = _bitnet_returns("crypto_analysis")
        tool, reason = route_tool_confident("BTC分析", TOOL_NAMES)
        self.assertEqual(tool, "crypto_analysis")
        self.assertEqual(reason, "clean_match")

    @mock.patch.object(tool_bitnet, "_run_bitnet")
    def test_long_running_tool_returned_as_clean_match(self, m):
        """
        現状仕様の記録: route_tool_confident は long_running 概念を知らないため、
        discord_bot への forward 一致は clean_match を返す。long_running の除外は
        呼び出し側ゲート（main.route_to_tool）の責務であることを固定する回帰テスト。
        """
        m.return_value = _bitnet_returns("discord_bot")
        tool, reason = route_tool_confident("ボット起動", TOOL_NAMES, TOOL_DESCS)
        self.assertEqual(tool, "discord_bot")
        self.assertEqual(reason, "clean_match")


import types

# main.py は dotenv 等に依存するため、import できない CI ではゲート統合テストのみ
# skip する（route_tool_confident の単体テストは上で完結しており無関係）。
try:
    import main as _main  # noqa: E402
    _MAIN_IMPORT_ERR = None
except Exception as e:  # pragma: no cover - 環境依存
    _main = None
    _MAIN_IMPORT_ERR = e


def _fake_lib_shared(return_json):
    """`from lib.lib_shared import client, MODEL_NAME` が拾う偽モジュールを作る。"""
    fake = types.ModuleType("lib.lib_shared")
    resp = mock.Mock()
    resp.text = return_json
    fake.client = mock.Mock()
    fake.client.models.generate_content.return_value = resp
    fake.MODEL_NAME = "fake-model"
    return fake


@unittest.skipIf(_main is None, f"main import 不可（環境依存）: {_MAIN_IMPORT_ERR}")
class TestRouteToToolGate(unittest.TestCase):
    """main.route_to_tool の二段ゲート。route_tool_confident をモックし Gemini は偽注入。"""

    def _run(self, confident_return, gemini_json='{"tool": "agent", "arg": "x"}'):
        fake = _fake_lib_shared(gemini_json)
        with mock.patch("tools.tool_bitnet.route_tool_confident") as m_conf, \
             mock.patch.dict(sys.modules, {"lib.lib_shared": fake}):
            if isinstance(confident_return, Exception):
                m_conf.side_effect = confident_return
            else:
                m_conf.return_value = confident_return
            result = _main.route_to_tool("dummy input")
        return result, fake.client.models.generate_content

    def test_noarg_clean_match_skips_gemini(self):
        """no-arg clean match → BitNet採用・Geminiは呼ばれない・arg=None。"""
        result, gemini = self._run(("market_scan", "clean_match"))
        self.assertEqual(result, {"tool": "market_scan", "arg": None})
        gemini.assert_not_called()

    def test_long_running_does_not_skip_gemini(self):
        """
        Critical回帰: long_running(scheduler) は clean_match でも BitNet採用しない。
        Geminiの二次判定を必ず経由する（無確認オートローンチ防止）。
        """
        result, gemini = self._run(
            ("scheduler", "clean_match"),
            gemini_json='{"tool": "scheduler", "arg": "start"}',
        )
        gemini.assert_called_once()
        # BitNetスキップ経路なら {"scheduler", None} になるはず。そうでないことを確認。
        self.assertEqual(result, {"tool": "scheduler", "arg": "start"})
        self.assertNotEqual(result.get("arg"), None)

    def test_long_running_discord_bot_does_not_skip_gemini(self):
        """discord_bot も同様に Gemini 経由。"""
        result, gemini = self._run(
            ("discord_bot", "clean_match"),
            gemini_json='{"tool": "discord_bot", "arg": "start"}',
        )
        gemini.assert_called_once()

    def test_needs_arg_falls_through_to_gemini(self):
        """needs_arg=True(crypto_analysis) は BitNet一致でも引数抽出のためGeminiへ。"""
        result, gemini = self._run(
            ("crypto_analysis", "clean_match"),
            gemini_json='{"tool": "crypto_analysis", "arg": "BTC/USDT"}',
        )
        gemini.assert_called_once()
        self.assertEqual(result, {"tool": "crypto_analysis", "arg": "BTC/USDT"})

    def test_confident_none_falls_through_to_gemini(self):
        """route_tool_confident が None(escalate) → Geminiへ。"""
        result, gemini = self._run((None, "ambiguous"))
        gemini.assert_called_once()

    def test_confident_exception_degrades_to_gemini(self):
        """route_tool_confident が例外(exe不在等) → 縮退してGeminiへ。"""
        result, gemini = self._run(RuntimeError("boom"))
        gemini.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
