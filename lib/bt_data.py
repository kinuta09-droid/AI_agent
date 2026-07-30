# -*- coding: utf-8 -*-
"""
Backtest Data Layer — OHLCV / Fear&Greed 履歴のローカルキャッシュ

バックテストは同じ履歴データを何度も読むため、Binance(ccxt) と
alternative.me からの取得結果を logs/ohlcv_cache/ にキャッシュする。
2回目以降はAPIを叩かず即時ロード、増分のみ追加取得。

- OHLCV: {SYMBOL}_{timeframe}.csv (ts=ミリ秒UNIX, 昇順, 未確定足は含めない)
- F&G:   fng_history.json (YYYY-MM-DD(UTC) → int, 24時間で再取得)

lib_shared には依存しない (APIキー不要のオフライン層)。
"""
import os
import json
import time
import datetime

import ccxt
import pandas as pd
import requests as req

from lib.paths import LOGS_DIR

OHLCV_CACHE_DIR = os.path.join(LOGS_DIR, "ohlcv_cache")
FNG_CACHE_PATH = os.path.join(OHLCV_CACHE_DIR, "fng_history.json")
FNG_API_URL = "https://api.alternative.me/fng/?limit=0"
FNG_CACHE_TTL_SEC = 24 * 3600

TIMEFRAME_MS = {
    "1h": 3_600_000,
    "4h": 4 * 3_600_000,
    "1d": 24 * 3_600_000,
}

CSV_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def _cache_path(symbol: str, timeframe: str) -> str:
    return os.path.join(OHLCV_CACHE_DIR, f"{symbol.replace('/', '_')}_{timeframe}.csv")


def _fetch_range(exchange, symbol: str, timeframe: str,
                 since_ms: int, until_ms: int) -> list:
    """[since_ms, until_ms) のOHLCVをページネーションで取得する。

    Binanceは1コール最大1000本。レートリミット対策で0.3s sleep を挟む。
    """
    tf_ms = TIMEFRAME_MS[timeframe]
    rows = []
    cursor = since_ms
    while cursor < until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(b for b in batch if b[0] < until_ms)
        next_cursor = batch[-1][0] + tf_ms
        if next_cursor <= cursor:
            break  # 進捗なし (異常応答) → 無限ループ防止
        cursor = next_cursor
        if len(batch) < 1000:
            break
        time.sleep(0.3)
    return rows


def get_ohlcv(symbol: str, timeframe: str, since_ms: int,
              exchange=None) -> pd.DataFrame:
    """CSVキャッシュ付きOHLCV取得。

    キャッシュより古い区間 (頭側) も新しい区間 (尻側) も増分取得して
    マージする。未確定の最新足 (ts + tf_ms > 現在時刻) は含めない。

    Returns: DataFrame [timestamp(datetime64, UTC naive), open, high, low,
             close, volume] 昇順、ts >= since_ms の行のみ。
    """
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    os.makedirs(OHLCV_CACHE_DIR, exist_ok=True)
    tf_ms = TIMEFRAME_MS[timeframe]
    path = _cache_path(symbol, timeframe)
    now_ms = int(time.time() * 1000)

    cached = pd.DataFrame(columns=CSV_COLUMNS)
    if os.path.exists(path):
        try:
            cached = pd.read_csv(path, encoding="utf-8")
        except Exception:
            cached = pd.DataFrame(columns=CSV_COLUMNS)  # 破損キャッシュは捨てて再取得

    new_rows = []
    if exchange is None and (
        len(cached) == 0
        or since_ms < int(cached["ts"].iloc[0]) - tf_ms
        or int(cached["ts"].iloc[-1]) + 2 * tf_ms <= now_ms
    ):
        exchange = ccxt.binance({"enableRateLimit": True})

    if len(cached):
        head_ts = int(cached["ts"].iloc[0])
        tail_ts = int(cached["ts"].iloc[-1])
        if since_ms < head_ts - tf_ms:
            new_rows += _fetch_range(exchange, symbol, timeframe, since_ms, head_ts)
        if tail_ts + 2 * tf_ms <= now_ms:  # 尻側に確定済みの新バーがある
            new_rows += _fetch_range(exchange, symbol, timeframe, tail_ts + tf_ms, now_ms)
    else:
        new_rows = _fetch_range(exchange, symbol, timeframe, since_ms, now_ms)

    if new_rows:
        new_df = pd.DataFrame(new_rows, columns=CSV_COLUMNS)
        # 空dfとのconcatはobject dtypeに化ける (pandas 2.x) ため空はスキップ
        merged = (pd.concat([cached, new_df], ignore_index=True)
                  if len(cached) else new_df)
    else:
        merged = cached

    if len(merged) == 0:
        return pd.DataFrame(columns=["timestamp"] + CSV_COLUMNS[1:])

    merged = merged.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    merged = merged.astype({"ts": "int64", "open": "float64", "high": "float64",
                            "low": "float64", "close": "float64",
                            "volume": "float64"})
    # 未確定足を除外 (バー開始 + 期間 が現在より未来 = まだ形成中)
    merged = merged[merged["ts"] + tf_ms <= now_ms].reset_index(drop=True)

    if new_rows:
        merged.to_csv(path, index=False, encoding="utf-8")

    out = merged[merged["ts"] >= since_ms].reset_index(drop=True).copy()
    out["timestamp"] = pd.to_datetime(out["ts"], unit="ms")
    return out[["timestamp", "open", "high", "low", "close", "volume"]]


def get_fng_history() -> dict:
    """Fear & Greed Index 全履歴 → {"YYYY-MM-DD"(UTC): int}。

    alternative.me は無料・全履歴一括。24時間キャッシュし、取得失敗時は
    stale キャッシュがあればそれを返す (完全失敗時は空dict)。
    バックテスト側は欠損日を 50 (Neutral) で埋める想定。
    """
    os.makedirs(OHLCV_CACHE_DIR, exist_ok=True)
    if os.path.exists(FNG_CACHE_PATH):
        age = time.time() - os.path.getmtime(FNG_CACHE_PATH)
        if age < FNG_CACHE_TTL_SEC:
            try:
                with open(FNG_CACHE_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass  # 破損 → 再取得へ

    try:
        resp = req.get(FNG_API_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        history = {}
        for item in data:
            ts = int(item["timestamp"])
            date_str = datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime("%Y-%m-%d")
            history[date_str] = int(item["value"])
        with open(FNG_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f)
        return history
    except Exception as e:
        print(f"  ⚠️ F&G history fetch failed: {e}")
        if os.path.exists(FNG_CACHE_PATH):
            try:
                with open(FNG_CACHE_PATH, encoding="utf-8") as f:
                    return json.load(f)  # stale でも無いよりマシ
            except Exception:
                pass
        return {}
