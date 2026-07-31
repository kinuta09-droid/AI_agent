# -*- coding: utf-8 -*-
"""BitNet 実用テスト: ルーター・要約・下書き

外部前提 (C:\\BitNet の llama-cli.exe + 量子化モデル) が要る手動デバッグスクリプト。
前提が無い環境 (CI 等) では SKIP して正常終了する — 以前はルートを直書きしていて
`No module named 'tools'` で落ち、直した後も FileNotFoundError で落ちる状態だった。

さらに ARMS_SLOW_TESTS=1 のときだけ実行する (2026-07-31):
  アサーションを持たない計時ベンチで、ローカル LLM 推論に約78秒かかる。
  スイープ484.9秒のうち BitNet 3本だけで185秒 = 38.9% を占めていた。
  推論時間は固有の計算量なので速くはできない -> opt-in の後ろへ置く。
  **ARMS_LIVE_TESTS と混同しないこと** — あちらは「課金・外部副作用がある」、
  こちらは「遅いだけで無料・無副作用」。統合すると、課金ゲートを緩めたいときに
  遅いベンチまで一緒に走る。前提のゲート (下の LLAMA_CLI 存在確認) とも別物で、
  CI は前提不在で SKIP・開発機は opt-in 未指定で SKIP と、両方必要。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# このファイルだけ stdout の再設定が抜けていた。SKIP 行の ⏭️ が cp932 で
# エンコードできず UnicodeEncodeError で落ちる (LEVEL 0 の対処)。
sys.stdout.reconfigure(encoding='utf-8')
from tools.tool_bitnet import route_tool, summarize, draft_message, LLAMA_CLI, MODEL_PATH
import time

if os.getenv("ARMS_SLOW_TESTS", "").strip() != "1":
    print("⏭️ SKIP: 遅い実測ベンチ (BitNet 推論で約78秒)。ARMS_SLOW_TESTS=1 で実行します")
    sys.exit(0)

if not (os.path.exists(LLAMA_CLI) and os.path.exists(MODEL_PATH)):
    print("⏭️ SKIP: BitNet の実行環境が見つかりません")
    print(f"   llama-cli: {LLAMA_CLI}")
    print(f"   model    : {MODEL_PATH}")
    sys.exit(0)

TOOLS = ["daily_news", "crypto_analysis", "curate", "agent", "bitnet"]
TOOL_DESC = {
    "daily_news":       "fetch and summarize today's news",
    "crypto_analysis":  "analyze cryptocurrency prices and market data (Bitcoin, ETH, etc.)",
    "curate":           "research a topic deeply and create a knowledge note",
    "agent":            "general-purpose AI agent for complex tasks",
    "bitnet":           "local AI inference tool (offline use)",
}

print("=" * 50)
print("TEST 1: ローカルルーター (3問)")
print("=" * 50)
tests = [
    ("ビットコインの最新価格を教えて",        "crypto_analysis"),
    ("今日のテクノロジーニュースをまとめて",   "daily_news"),
    ("AIについて深く調べてノートにまとめたい", "curate"),
]
correct = 0
for q, expected in tests:
    t0 = time.time()
    result = route_tool(q, TOOLS, TOOL_DESC)
    elapsed = time.time() - t0
    mark = "OK" if result == expected else f"MISS(expected: {expected})"
    print(f"  入力: {q}")
    print(f"  → {result}  {mark}  ({elapsed:.1f}s)\n")
    if result == expected:
        correct += 1
print(f"  正解率: {correct}/{len(tests)}\n")

print("=" * 50)
print("TEST 2: 日本語要約")
print("=" * 50)
text = """
BitNetは1ビット量子化を用いた大規模言語モデルで、各重みを{-1, 0, +1}の3値に
制限することで、従来の16bitモデルに比べてメモリ使用量を約10分の1に削減できます。
さらに整数演算のみを使用するため、CPUでの推論が高速化され、エネルギー消費も
大幅に低下します。2Bパラメータのモデルで1.7GBのメモリしか使用せず、
一般的なノートPCのCPUで毎秒20トークン以上の推論が可能です。
Microsoftが2024年に発表したこの技術は、エッジデバイスでのAI展開を大きく前進させると
期待されています。
"""
t0 = time.time()
summary = summarize(text, lines=3)
elapsed = time.time() - t0
print(f"要約結果 ({elapsed:.1f}s):")
print(f"  {summary}\n")

print("=" * 50)
print("TEST 3: Discordメッセージ下書き")
print("=" * 50)
context = "BitNet b1.58の導入が完了しました。ローカルAI推論が使えるようになりました。"
t0 = time.time()
draft = draft_message(context, style="discord")
elapsed = time.time() - t0
print(f"下書き ({elapsed:.1f}s):")
print(f"  {draft}\n")

print("=" * 50)
print("TEST 4: Obsidianノート下書き")
print("=" * 50)
context2 = "今日のBTCの価格は95000ドル。RSIが70を超え過熱感。次の抵抗線は100000ドル。"
t0 = time.time()
note = draft_message(context2, style="note")
elapsed = time.time() - t0
print(f"ノート下書き ({elapsed:.1f}s):")
print(f"  {note}\n")

print("ALL TESTS DONE")
