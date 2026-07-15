# -*- coding: utf-8 -*-
"""BitNet 新機能テスト: generate_pattern / quick_filter_discord / batch_tag_notes"""
import sys
sys.path.insert(0, r'C:\AI_agent')
sys.stdout.reconfigure(encoding='utf-8')
from tools.tool_bitnet import generate_pattern, quick_filter_discord, generate_note_tags, batch_tag_notes
import time

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
    reply_str = f', reply="{result["reply"][:20]}"' if result.get("reply") else ""
    print(f"  [{mark}] {label}: {repr(msg)} -> {action}{reply_str}")

print(f"\n  正解率: {ok}/{len(filter_cases)}\n")

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
