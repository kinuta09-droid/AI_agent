# -*- coding: utf-8 -*-
"""BitNet 実用テスト: ルーター・要約・下書き"""
import sys
sys.path.insert(0, r'C:\AI_agent')
from tools.tool_bitnet import route_tool, summarize, draft_message
import time

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
