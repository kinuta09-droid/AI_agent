# -*- coding: utf-8 -*-
"""
lib/bt_data.py のユニットテスト (実API不使用):
- get_ohlcv: 初回取得→CSV書込 / dtype保証 (objectドリフト再発防止) /
  キャッシュヒット時に再取得しない / 頭側バックフィル / 未確定足除外
- get_fng_history: 新鮮なキャッシュはネットワークなしで返す

背景: 空DataFrameとのconcatで数値列がobject dtype化し pandas_ta が
クラッシュする実バグがあった (Phase3 Step5)。dtypeチェックはその回帰テスト。
_fetch_range を差し替えて偽データを注入する。
"""
import sys
import os
import json
import time
import shutil
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from lib import bt_data

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


TMP_DIR = tempfile.mkdtemp(prefix="bt_data_test_")
bt_data.OHLCV_CACHE_DIR = TMP_DIR
bt_data.FNG_CACHE_PATH = os.path.join(TMP_DIR, "fng_history.json")

TF_MS = bt_data.TIMEFRAME_MS["1h"]
FETCH_CALLS = []


def _fake_fetch_range(exchange, symbol, timeframe, since_ms, until_ms):
    """[since, until) のアラインされた1hバーを生成する偽フェッチ。"""
    FETCH_CALLS.append((since_ms, until_ms))
    tf = bt_data.TIMEFRAME_MS[timeframe]
    start = since_ms - (since_ms % tf)
    if start < since_ms:
        start += tf
    rows = []
    t = start
    while t < until_ms:
        rows.append([t, 100.0, 101.0, 99.0, 100.0, 1000.0])
        t += tf
    return rows


bt_data._fetch_range = _fake_fetch_range


def test_initial_fetch_and_dtypes():
    print("[Test] 初回取得 + dtype保証")
    since = int(time.time() * 1000) - 100 * TF_MS
    df = bt_data.get_ohlcv("FAKE/USDT", "1h", since)
    check("約100本取得", 95 <= len(df) <= 101)
    check("CSVキャッシュが作成される",
          os.path.exists(os.path.join(TMP_DIR, "FAKE_USDT_1h.csv")))
    # objectドリフト回帰テスト (concat時にobject化するとpandas_taがクラッシュ)
    numeric = all(str(df[c].dtype) == "float64"
                  for c in ("open", "high", "low", "close", "volume"))
    check("数値列が全てfloat64 (object化していない)", numeric)
    now_ms = int(time.time() * 1000)
    last_ts = int(df["timestamp"].iloc[-1].timestamp() * 1000)
    check("未確定足を含まない", last_ts + TF_MS <= now_ms)


def test_cache_hit_no_refetch():
    print("[Test] キャッシュヒット時は再取得しない")
    since = int(time.time() * 1000) - 100 * TF_MS
    n_before = len(FETCH_CALLS)
    df = bt_data.get_ohlcv("FAKE/USDT", "1h", since)
    check("フェッチ呼び出しなし", len(FETCH_CALLS) == n_before)
    check("本数はキャッシュと同じ", 95 <= len(df) <= 101)


def test_head_backfill():
    print("[Test] 頭側バックフィル (キャッシュより古い期間の要求)")
    since_older = int(time.time() * 1000) - 200 * TF_MS
    n_before = len(FETCH_CALLS)
    df = bt_data.get_ohlcv("FAKE/USDT", "1h", since_older)
    check("フェッチが発生", len(FETCH_CALLS) > n_before)
    check("約200本に拡張", 195 <= len(df) <= 201)
    numeric = all(str(df[c].dtype) == "float64"
                  for c in ("open", "high", "low", "close", "volume"))
    check("マージ後もfloat64維持", numeric)


def test_fng_fresh_cache():
    print("[Test] F&G 新鮮キャッシュはネットワーク不要")
    payload = {"2026-07-17": 27, "2026-07-16": 30}
    with open(bt_data.FNG_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    # mtimeは書き込み直後 = 24h以内 → requests を使わずキャッシュを返すはず
    result = bt_data.get_fng_history()
    check("キャッシュ内容が返る", result == payload)


if __name__ == "__main__":
    try:
        test_initial_fetch_and_dtypes()
        test_cache_hit_no_refetch()
        test_head_backfill()
        test_fng_fresh_cache()
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("\n" + "=" * 40)
    print(f"Result: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
