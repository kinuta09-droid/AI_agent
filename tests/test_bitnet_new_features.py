# -*- coding: utf-8 -*-
"""BitNet 新機能テスト: generate_pattern / quick_filter_discord / batch_tag_notes

TEST 1 (quick_filter_discord) は BitNet バイナリを使わないのでどこでも動く。
TEST 2 以降は C:\\BitNet の llama-cli.exe + 量子化モデルが要るので、前提が無い
環境 (CI 等) では TEST 1 だけ実行して SKIP で正常終了する。

TEST 2 以降は ARMS_SLOW_TESTS=1 のときだけ実行する (2026-07-31):
  推論で約71秒かかる計時ベンチだから。**TEST 1 は必ず残す** — 唯一の
  アサーション付きテストで、BitNet バイナリを必要としない。
  ARMS_LIVE_TESTS との違いは tests/test_bitnet_practical.py の冒頭を参照。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')
from tools.tool_bitnet import (
    generate_pattern, quick_filter_discord, generate_note_tags, batch_tag_notes,
    LLAMA_CLI, MODEL_PATH,
)
import time

_BITNET_AVAILABLE = os.path.exists(LLAMA_CLI) and os.path.exists(MODEL_PATH)
_SLOW_ENABLED = os.getenv("ARMS_SLOW_TESTS", "").strip() == "1"
_FAIL = []

print("=" * 55)
print("TEST 1: quick_filter_discord (BitNet 不使用・即時)")
print("=" * 55)

filter_cases = [
    ("こんにちは",          "reply",   "挨拶"),
    ("hi",                  "reply",   "英語挨拶"),
    ("test",                "skip",    "スパム"),
    ("...",                 "skip",    "ノイズ"),
    ("BTCどう思う？",       "pass",    "実質問"),
    ("",                    "skip",    "空文字"),
    ("ありがとう",          "reply",   "お礼"),
    ("lol",                 "reply",   "笑い"),
]

ok = 0
for msg, expected_action, label in filter_cases:
    result = quick_filter_discord(msg)
    action = result["action"]
    mark = "OK" if action == expected_action else f"FAIL(expected={expected_action})"
    if action == expected_action:
        ok += 1
    else:
        _FAIL.append(f"quick_filter_discord {label}: {msg!r}")
    reply_str = f', reply="{result["reply"][:20]}"' if result.get("reply") else ""
    print(f"  [{mark}] {label}: {repr(msg)} -> {action}{reply_str}")

print(f"\n  正解率: {ok}/{len(filter_cases)}\n")

if not _BITNET_AVAILABLE or not _SLOW_ENABLED:
    # TEST 2 以降は (a) BitNet の実行環境と (b) 遅いベンチの opt-in の両方が要る。
    # どちらで止まったのかを書き分ける — 「SKIP されている」だけ見えても、
    # 意図した理由で止まっているのか分からないと、ゲートを付けた側が
    # 「永久に走らなくなった」ことに気付けない。
    # ここまでの TEST 1 の判定は終了コードに載せる。
    if not _BITNET_AVAILABLE:
        print("⏭️ SKIP: TEST 2 以降は BitNet の実行環境が必要です")
        print(f"   llama-cli: {LLAMA_CLI}")
        print(f"   model    : {MODEL_PATH}")
    else:
        print("⏭️ SKIP: TEST 2 以降は遅い実測ベンチ (約71秒)。ARMS_SLOW_TESTS=1 で実行します")
    print(f"\n=== 結果: FAIL {len(_FAIL)} ===")
    sys.exit(1 if _FAIL else 0)

print("=" * 55)
print("TEST 2: generate_pattern - cron 式")
print("=" * 55)

cron_cases = [
    ("every weekday at 9am",                 "0 9 * * 1-5"),
    ("every day at midnight",                "0 0 * * *"),
    ("every hour",                           "0 * * * *"),
]

for desc, expected in cron_cases:
    t0 = time.time()
    result = generate_pattern(desc, "cron")
    elapsed = time.time() - t0
    print(f"  [{elapsed:.1f}s] {desc}")
    print(f"    -> {result!r}  (expected: {expected})\n")

print("=" * 55)
print("TEST 3: generate_pattern - regex")
print("=" * 55)

regex_cases = [
    "match YYYY-MM-DD date format",
    "match Japanese postal code like 123-4567",
    "match email address",
]

for desc in regex_cases:
    t0 = time.time()
    result = generate_pattern(desc, "regex")
    elapsed = time.time() - t0
    print(f"  [{elapsed:.1f}s] {desc}")
    print(f"    -> {result!r}\n")

print("=" * 55)
print("TEST 4: generate_pattern - python snippet")
print("=" * 55)

python_cases = [
    "read a JSON file and print its keys",
    "get current timestamp as string",
]

for desc in python_cases:
    t0 = time.time()
    result = generate_pattern(desc, "python")
    elapsed = time.time() - t0
    print(f"  [{elapsed:.1f}s] {desc}")
    print(f"    -> {result!r}\n")

print("=" * 55)
print("TEST 5: generate_note_tags (単体)")
print("=" * 55)

tag_cases = [
    ("BTC Analysis 2024", "Bitcoin RSI 70 exceeded, overheating. Next resistance at 100000."),
    ("Python Tutorial", "How to use list comprehensions and lambda functions."),
    ("Meeting Notes 2024-03-12", "Discussed quarterly roadmap and team OKRs."),
]

for title, preview in tag_cases:
    t0 = time.time()
    tags = generate_note_tags(title, preview, max_tags=5)
    elapsed = time.time() - t0
    print(f"  [{elapsed:.1f}s] {title}")
    print(f"    -> {tags}\n")

print("=" * 55)
print("TEST 6: batch_tag_notes (3件バッチ)")
print("=" * 55)

notes = [
    {"title": "BTC Weekly Analysis", "content": "Bitcoin price hit 95000. RSI is at 75 indicating overbought conditions."},
    {"title": "Python Async Tips",   "content": "Using asyncio.gather for concurrent API calls reduces latency significantly."},
    {"title": "Obsidian Workflow",   "content": "Daily notes with templates, Dataview queries for task tracking."},
]

t0 = time.time()
results = batch_tag_notes(notes)
total_elapsed = time.time() - t0

for r in results:
    print(f"  {r['title']}")
    print(f"    -> {r['tags']}")

print(f"\n  合計時間: {total_elapsed:.1f}s")
print("\nALL TESTS DONE")
print(f"\n=== 結果: FAIL {len(_FAIL)} ===")
sys.exit(1 if _FAIL else 0)
