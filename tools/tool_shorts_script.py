# -*- coding: utf-8 -*-
"""
tool_shorts_script.py — SNS用ショート動画自動編集 Path B (テキスト/画像から生成) の骨組み

TTS(音声合成)がこのリポジトリに未導入のため、台本生成インターフェースの定義のみ置く。
実装は次セッションで行う。tool_registry.py には未登録(未実装ツールをPlannerに晒さない)。
"""


def generate_shorts_script(topic: str, target_seconds: int = 30) -> dict:
    """[次セッション実装予定] テキストのみからショート動画用台本を生成する (Path B)。

    Returns想定: {"scenes": [{"narration": str, "image_prompt": str, "duration_sec": float}, ...]}
    """
    raise NotImplementedError("Path B (テキスト/画像からの動画生成) は次セッションで実装予定")
