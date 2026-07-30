# -*- coding: utf-8 -*-
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from tools.tool_bitnet import quick_filter_discord

# quick_filter_discord は BitNet バイナリを使わない (純キーワード判定) ので、
# このファイルは C:\BitNet が無い環境でもそのまま実行できる。
_FAIL = []

print("=== Fix1: URL は skip でなく pass ===")
url_cases = [
    ("https://x.com/status/12345", "pass"),
    ("test",                        "skip"),
    ("...",                         "skip"),
    ("BTCどう思う？",               "pass"),
]
for msg, expected in url_cases:
    r = quick_filter_discord(msg)
    if r["action"] == expected:
        ok = "OK"
    else:
        ok = f"FAIL(got={r['action']})"
        _FAIL.append(f"url_case {msg!r}")
    print(f"  [{ok}] {msg!r} -> {r['action']}")

print()
print("=== Fix3: greeting replies ===")
for msg in ["よろしく", "はじめまして", "hey", "thanks", "thank you"]:
    r = quick_filter_discord(msg)
    print(f"  {msg!r} -> {r.get('reply', '(no reply)')}")

print()
print("=== Fix4: regex後処理 ===")
# re.MULTILINE なし: string先頭のfenceのみ除去
raw_with_mid_fence = "pattern\n```extra"
cleaned = re.sub(r"^```\w*\s*", "", raw_with_mid_fence).strip()
print(f"  中間fence除去されないこと: {repr(cleaned)}")

# \w+$ 一律除去を廃止 → 有効パターン末尾を壊さない
safe_cases = [
    (r"^\d{4}-\d{2}-\d{2}\s+\w+",   r"^\d{4}-\d{2}-\d{2}\s+\w+"),  # 壊れない
    (r"^\d+ Response",                r"^\d+"),                         # Responseは除去
    (r"^\d+ Example",                 r"^\d+"),                         # Exampleは除去
]
for inp, expected in safe_cases:
    out = re.sub(r"\s+(Response|Example|Output|Answer|Result)$", "", inp, flags=re.IGNORECASE)
    if out == expected:
        ok = "OK"
    else:
        ok = f"FAIL(got={out!r})"
        _FAIL.append(f"safe_case {inp!r}")
    print(f"  [{ok}] {inp!r} -> {out!r}")

# 判定を終了コードに載せる。print するだけだと FAIL が並んでいても exit 0 =
# CI からは永久に緑になる (2026-07-30)。
print(f"\n=== 結果: FAIL {len(_FAIL)} ===")
if _FAIL:
    for f in _FAIL:
        print(f"  - {f}")
sys.exit(1 if _FAIL else 0)
