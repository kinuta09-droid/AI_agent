# -*- coding: utf-8 -*-
"""
test_shorts_editor.py — tools.tool_shorts_editor の純粋ロジック/モックテスト

外部呼び出し(ffmpeg subprocess・Gemini)は全てモックし、課金・実行環境に依存しない。
実ffmpeg統合テストは tests/test_shorts_editor_practical.py (ffmpeg不在ならSKIP) を参照。

実行:
  py -m pytest tests/test_shorts_editor.py -q
  （pytest 不在時）py tests/test_shorts_editor.py
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools import tool_shorts_editor as se


class TestSelectHighlightGemini(unittest.TestCase):
    def _resp(self, text):
        resp = mock.Mock()
        resp.text = text
        return resp

    def test_empty_segments_skips_gemini(self):
        """セグメントが無ければGeminiを呼ばずフォールバック。"""
        with mock.patch.object(se.client.models, "generate_content") as m:
            result = se._select_highlight_gemini([], target_seconds=30, topic_hint=None)
            m.assert_not_called()
        self.assertEqual(result["start"], 0.0)
        self.assertEqual(result["end"], 30.0)
        self.assertIn("fallback", result["reason"])

    def test_normal_json_response_parsed(self):
        """正常なJSON応答はstart/endがそのまま採用される。"""
        segments = [{"start": 0.0, "end": 60.0, "text": "dummy"}]
        with mock.patch.object(se.client.models, "generate_content") as m:
            m.return_value = self._resp('{"start": 10.0, "end": 35.0, "reason": "面白い"}')
            result = se._select_highlight_gemini(segments, target_seconds=25, topic_hint=None)
        self.assertEqual(result["start"], 10.0)
        self.assertEqual(result["end"], 35.0)
        self.assertEqual(result["reason"], "面白い")

    def test_broken_json_falls_back(self):
        """壊れたJSON応答 → 先頭target_seconds秒へフォールバック。"""
        segments = [{"start": 0.0, "end": 60.0, "text": "dummy"}]
        with mock.patch.object(se.client.models, "generate_content") as m:
            m.return_value = self._resp("not a json at all")
            result = se._select_highlight_gemini(segments, target_seconds=20, topic_hint=None)
        self.assertEqual(result["start"], 0.0)
        self.assertEqual(result["end"], 20.0)
        self.assertIn("fallback", result["reason"])

    def test_start_gte_end_falls_back(self):
        """start >= end の異常値 → フォールバック。"""
        segments = [{"start": 0.0, "end": 60.0, "text": "dummy"}]
        with mock.patch.object(se.client.models, "generate_content") as m:
            m.return_value = self._resp('{"start": 40.0, "end": 30.0, "reason": "x"}')
            result = se._select_highlight_gemini(segments, target_seconds=20, topic_hint=None)
        self.assertIn("fallback", result["reason"])

    def test_end_exceeds_total_duration_clamped(self):
        """endが動画長を超える場合はクランプされる。"""
        segments = [{"start": 0.0, "end": 60.0, "text": "dummy"}]
        with mock.patch.object(se.client.models, "generate_content") as m:
            m.return_value = self._resp('{"start": 5.0, "end": 999.0, "reason": "x"}')
            result = se._select_highlight_gemini(
                segments, target_seconds=30, topic_hint=None, total_duration=50.0
            )
        self.assertEqual(result["end"], 50.0)

    def test_gemini_exception_falls_back(self):
        """Gemini呼び出し自体が例外を出しても落とさずフォールバック。"""
        segments = [{"start": 0.0, "end": 60.0, "text": "dummy"}]
        with mock.patch.object(se.client.models, "generate_content") as m:
            m.side_effect = RuntimeError("quota exceeded")
            result = se._select_highlight_gemini(segments, target_seconds=15, topic_hint=None)
        self.assertEqual(result["end"], 15.0)
        self.assertIn("fallback", result["reason"])


class TestShiftSrt(unittest.TestCase):
    def _write_and_read(self, segments, start, end):
        with tempfile.TemporaryDirectory() as d:
            out_path = os.path.join(d, "out.srt")
            n = se._shift_srt(segments, start, end, out_path)
            with open(out_path, "r", encoding="utf-8") as f:
                content = f.read()
        return n, content

    def test_segment_fully_inside_highlight(self):
        segments = [{"start": 10.0, "end": 12.0, "text": "hello"}]
        n, content = self._write_and_read(segments, 10.0, 20.0)
        self.assertEqual(n, 1)
        self.assertIn("00:00:00,000 --> 00:00:02,000", content)
        self.assertIn("hello", content)

    def test_segment_fully_outside_highlight_excluded(self):
        segments = [{"start": 0.0, "end": 5.0, "text": "before"}]
        n, content = self._write_and_read(segments, 10.0, 20.0)
        self.assertEqual(n, 0)
        self.assertEqual(content, "")

    def test_segment_partially_overlapping_start_is_clamped(self):
        """ハイライト開始前から続くセグメントは、区間の先頭にクランプされる。"""
        segments = [{"start": 8.0, "end": 12.0, "text": "straddle"}]
        n, content = self._write_and_read(segments, 10.0, 20.0)
        self.assertEqual(n, 1)
        self.assertIn("00:00:00,000 --> 00:00:02,000", content)

    def test_segment_partially_overlapping_end_is_clamped(self):
        segments = [{"start": 18.0, "end": 25.0, "text": "straddle_end"}]
        n, content = self._write_and_read(segments, 10.0, 20.0)
        self.assertEqual(n, 1)
        self.assertIn("00:00:08,000 --> 00:00:10,000", content)

    def test_no_segments_writes_empty_file_zero_count(self):
        n, content = self._write_and_read([], 0.0, 30.0)
        self.assertEqual(n, 0)
        self.assertEqual(content, "")


class TestBuildVerticalFiltergraph(unittest.TestCase):
    def test_pad_mode_has_blur_and_overlay(self):
        chain = se._build_vertical_filtergraph("pad")
        self.assertIn("gblur", chain)
        self.assertIn("overlay", chain)
        self.assertIn("[vbase]", chain)
        self.assertIn("1080:1920", chain)

    def test_crop_mode_has_crop_no_blur(self):
        chain = se._build_vertical_filtergraph("crop")
        self.assertIn("crop=1080:1920", chain)
        self.assertNotIn("gblur", chain)
        self.assertIn("[vbase]", chain)

    def test_custom_dimensions_applied(self):
        chain = se._build_vertical_filtergraph("crop", width=720, height=1280)
        self.assertIn("720:1280", chain)


class TestRunFfmpeg(unittest.TestCase):
    def test_missing_binary_raises_file_not_found(self):
        with mock.patch.object(se.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                se._run_ffmpeg(["ffmpeg", "-version"])

    def test_nonzero_returncode_raises_runtime_error(self):
        with mock.patch.object(se.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(se.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom" * 200)
            with self.assertRaises(RuntimeError):
                se._run_ffmpeg(["ffmpeg", "-i", "x"])

    def test_success_returns_stdout(self):
        with mock.patch.object(se.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(se.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
            out = se._run_ffmpeg(["ffmpeg", "-version"])
        self.assertEqual(out, "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
