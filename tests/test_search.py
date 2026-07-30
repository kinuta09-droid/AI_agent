# -*- coding: utf-8 -*-
"""Gemini の Google Search グラウンディングの疎通テスト (手動デバッグスクリプト)。

これは **実 Gemini + 実 Google Search を1回呼ぶ = 課金が発生する** 疎通確認であり、
アサーションを持つ回帰テストではない。既定では SKIP し、明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_search.py

2026-07-30 に直した欠陥:
  1. `sys.path.insert(0, r'C:\\AI_agent')` とルートを直書きしており、
     リポジトリが別の場所にある環境 (CI の D:\\a\\AI_agent\\AI_agent) では
     `No module named 'lib'` で必ず落ちていた。開発機だけで通る「下駄」。
  2. 実 API を叩くのに前提のゲートが無く、スイープ1回で必ず課金していた。
  3. 失敗しても `except` して print するだけで終了コードが常に 0 だった
     (このリポジトリで繰り返し出ている「表示と実態の食い違い」)。
"""
import sys
import os

# プロジェクトルートをパスに追加 (ルート直書きはこのマシン専用になるので __file__ 相対)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')


def _live_gemini_allowed() -> bool:
    """実 Gemini 呼び出しを行ってよいか (オプトイン + 実キーの両方が必要)。"""
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        return False
    key = os.getenv("GOOGLE_API_KEY", "")
    return bool(key) and not key.startswith("ci-")


def main() -> int:
    try:
        from lib.lib_shared import api_key, MODEL_NAME
    except ImportError as e:
        print(f"❌ Failed to import lib_shared: {e}")
        return 1
    print(f"✅ Loaded config from lib_shared. Testing with model: {MODEL_NAME}")

    if not _live_gemini_allowed():
        print("⏭️ SKIP: ARMS_LIVE_TESTS=1 と実 GOOGLE_API_KEY のときだけ実行します")
        print("   (Google Search グラウンディング付きの生成を実呼び出しするため課金が発生します)")
        return 0

    from google import genai
    from google.genai.types import GenerateContentConfig, GoogleSearch, Tool, HttpOptions

    client = genai.Client(api_key=api_key, http_options=HttpOptions(api_version="v1beta"))
    print(f"Generating content with grounding for {MODEL_NAME}...")
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents="次のアメリカ合衆国の皆既日食はいつですか？",
            config=GenerateContentConfig(tools=[Tool(google_search=GoogleSearch())]),
        )
    except Exception as e:
        print(f"❌ Error during search test: {e}")
        return 1

    print("TEXT:", response.text)

    meta = response.candidates[0].grounding_metadata if response.candidates else None
    if not meta:
        # グラウンディングが効いていないなら疎通確認としては失敗。
        print("❌ No grounding metadata found (google_search が効いていない)")
        return 1

    print("search_entry_point:", getattr(meta, "search_entry_point", None))
    print("chunks:", len(meta.grounding_chunks) if meta.grounding_chunks else 0)
    for c in meta.grounding_chunks or []:
        if c.web:
            print("-", c.web.title, c.web.uri)
    return 0


if __name__ == "__main__":
    sys.exit(main())
