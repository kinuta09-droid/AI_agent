# -*- coding: utf-8 -*-
"""Gemini の Google Search グラウンディング疎通テスト (手動デバッグスクリプト)。

**実 Gemini + 実 Google Search を1回呼ぶ = 課金が発生する。**
共有 client / MODEL_NAME(LIGHT) 経由で、ライブと同一モデル・同一クライアントの
グラウンディング設定が生きているかを見るのが目的なので、スタブ化すると意味を失う。
よって既定では SKIP し、明示的なオプトインを要求する:

    ARMS_LIVE_TESTS=1 py tests/test_grounding.py

2026-07-30 に直した欠陥:
  1. **ゲートが無く、スイープ1周ごとに実 Gemini を1回呼んでいた** (日次カウンタが
     +1 されるのをカウンタ差分で確認)。「テストは課金ゼロ」が成立していなかった。
  2. `grounding_test.txt` を**カレント (= リポジトリ直下) に書いていた**。
     .gitignore されているのでコミットはされないが、実行するたびにリポジトリが汚れる。
     -> 一時ディレクトリへ。
  3. 例外を print するだけで **終了コードが常に 0** だった。

近縁: tests/test_search.py も同じくグラウンディングの疎通を見る。目的が重複している
ので、どちらかに寄せる価値はある (今回は挙動を変えないため両方残した)。
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _live_gemini_allowed() -> bool:
    """実 Gemini 呼び出しを行ってよいか (オプトイン + 実キーの両方が必要)。"""
    if os.getenv("ARMS_LIVE_TESTS") != "1":
        return False
    key = os.getenv("GOOGLE_API_KEY", "")
    return bool(key) and not key.startswith("ci-")


def main() -> int:
    if not _live_gemini_allowed():
        print("⏭️ SKIP: ARMS_LIVE_TESTS=1 と実 GOOGLE_API_KEY のときだけ実行します")
        print("   (Google Search グラウンディング付きの生成を実呼び出しするため課金が発生します)")
        return 0

    from google.genai import types
    # 施策③ Phase 4a: 独自 client + MODEL_NAME 直書きを廃し、共有 client /
    # MODEL_NAME(LIGHT・.env で世代交代可) 経由へ。ライブと同一条件で検証する。
    from lib.lib_shared import client, MODEL_NAME

    print(f"Testing Grounding with {MODEL_NAME}...")
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents="今日の東京の天気は？",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return 1

    print("\n--- Response ---")
    print(response.text)

    # 出力はリポジトリではなく一時ディレクトリへ
    out_dir = tempfile.mkdtemp(prefix="grounding_test_")
    out_path = os.path.join(out_dir, "grounding_test.txt")
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(response.text or "")
    print(f"(応答を保存: {out_path})")

    if not response.candidates:
        print("\n❌ candidates が空")
        return 1
    meta = response.candidates[0].grounding_metadata
    if not meta:
        # グラウンディングが効いていないなら疎通確認としては失敗
        print("\n❌ grounding_metadata が無い (google_search が効いていない)")
        return 1

    print("\n--- Grounding Metadata Found! ---")
    entry = getattr(meta, "search_entry_point", None)
    if entry is not None:
        print("Search Queries:", getattr(entry, "rendered_content", None))
    else:
        print("⚠️ search_entry_point が無い (metadata はあるので致命ではない)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
