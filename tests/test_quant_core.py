# -*- coding: utf-8 -*-
"""
Phase 3 (feat/crypto-phase3-atr-backtest) のユニットテスト:
- _compute_levels: ATR倍数計算 / floor・capクランプ / 価格ガード
- _apply_daily_gate: BUY系降格 / 非BUY素通し / UNKNOWN・無効時素通し
- _daily_trend: 上昇/下降/データ不足
- _resolve_trade: SL/TP/EXPIRED/OPEN / 時系列順序 / 同一バーSL優先
- _compute_indicators: キー完備 / trend判定 / 100本でのEMA200フォールバック
実API・実ファイルは使わない (全て純関数のテスト)。
"""
import sys
import os
import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd

from lib.quant_core import (
    _compute_levels, _apply_daily_gate, _daily_trend,
    _resolve_trade, _compute_indicators,
)

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


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_compute_levels():
    print("[Test] _compute_levels")
    # 通常ケース: price=100, ATR=2 (2%) → floor(1%)にもcap(4%)にも触れない
    lv = _compute_levels(100.0, 2.0, sl_mult=2.0, dca_mults=(1.5, 3.0, 4.5))
    check("atr_eff = 生ATRのまま", _approx(lv["atr_eff"], 2.0))
    check("DCA1 = 100 - 1.5×2 = 97", _approx(lv["dca_entry1"], 97.0))
    check("DCA2 = 100 - 3.0×2 = 94", _approx(lv["dca_entry2"], 94.0))
    check("DCA3 = 100 - 4.5×2 = 91", _approx(lv["dca_entry3"], 91.0))
    check("Stop = 100 - 2.0×2 = 96", _approx(lv["dca_stop"], 96.0))
    check("atr_used_pct = 2%", _approx(lv["atr_used_pct"], 2.0))

    # floorクランプ: ATR=0.3 (0.3%) → 1%に引き上げ (Stopが浅くなりすぎ防止)
    lv = _compute_levels(100.0, 0.3, atr_floor_pct=1.0)
    check("低ボラ: atr_eff floor 1%", _approx(lv["atr_eff"], 1.0))
    check("低ボラ: Stop = -2%", _approx(lv["dca_stop"], 98.0))

    # capクランプ: ATR=10 (10%) → 4%に切り下げ (Stopが深くなりすぎ防止)
    lv = _compute_levels(100.0, 10.0, atr_cap_pct=4.0)
    check("高ボラ: atr_eff cap 4%", _approx(lv["atr_eff"], 4.0))
    check("高ボラ: Stop = -8%", _approx(lv["dca_stop"], 92.0))

    # ATR=0 (計算失敗フォールバック) でも floor で水準が出る
    lv = _compute_levels(100.0, 0.0)
    check("ATR=0 → floorで水準成立", _approx(lv["dca_stop"], 98.0))

    # 価格ガード
    lv = _compute_levels(0.0, 2.0)
    check("price=0 → 全水準0でクラッシュしない", lv["dca_stop"] == 0.0)


def test_apply_daily_gate():
    print("[Test] _apply_daily_gate")
    sig, note = _apply_daily_gate("BUY", "DOWN")
    check("DOWN: BUY → WATCH LIST", sig == "WATCH LIST")
    check("DOWN: 降格理由が付く", note is not None and "日足" in note)

    sig, note = _apply_daily_gate("STRONG BUY", "DOWN")
    check("DOWN: STRONG BUY → WATCH LIST", sig == "WATCH LIST")

    for s in ("NEUTRAL", "WATCH LIST", "WAIT", "STAY AWAY", "OVERBOUGHT"):
        sig, note = _apply_daily_gate(s, "DOWN")
        check(f"DOWN: {s} は素通し", sig == s and note is None)

    sig, note = _apply_daily_gate("BUY", "UP")
    check("UP: BUY 素通し", sig == "BUY" and note is None)

    sig, note = _apply_daily_gate("BUY", "UNKNOWN")
    check("UNKNOWN: 素通し (フェイルオープン)", sig == "BUY" and note is None)

    sig, note = _apply_daily_gate("BUY", "DOWN", enabled=False)
    check("enabled=False: 素通し", sig == "BUY" and note is None)


def _daily_df(n, start, step):
    closes = [start + i * step for i in range(n)]
    return pd.DataFrame({
        'timestamp': pd.date_range('2024-01-01', periods=n, freq='D'),
        'open': closes,
        'high': [c * 1.01 for c in closes],
        'low': [c * 0.99 for c in closes],
        'close': closes,
        'volume': [1000.0] * n,
    })


def test_daily_trend():
    print("[Test] _daily_trend")
    check("上昇250日 → UP", _daily_trend(_daily_df(250, 100.0, 0.5)) == "UP")
    check("下降250日 → DOWN", _daily_trend(_daily_df(250, 250.0, -0.5)) == "DOWN")
    check("150日 (EMA200不足) → UNKNOWN", _daily_trend(_daily_df(150, 100.0, 0.5)) == "UNKNOWN")
    check("None → UNKNOWN", _daily_trend(None) == "UNKNOWN")


def _candles(rows):
    """rows: [(hours_after_entry, high, low)] → 1h足DataFrame"""
    base = datetime.datetime(2026, 1, 1, 0, 0, 0)
    return pd.DataFrame({
        'timestamp': [base + datetime.timedelta(hours=h) for h, _, _ in rows],
        'high': [hi for _, hi, _ in rows],
        'low': [lo for _, _, lo in rows],
    })


ENTRY_TIME = datetime.datetime(2026, 1, 1, 0, 0, 0)


def test_resolve_trade():
    print("[Test] _resolve_trade")
    # entry=100, stop=96, target=108
    st, ts, note = _resolve_trade(100, 96, 108, _candles([(1, 101, 99), (2, 102, 95)]), ENTRY_TIME)
    check("SL到達バー → LOSE", st == "LOSE")

    st, ts, note = _resolve_trade(100, 96, 108, _candles([(1, 101, 99), (2, 109, 100)]), ENTRY_TIME)
    check("TP到達バー → WIN", st == "WIN")

    st, ts, note = _resolve_trade(100, 96, 108, _candles([(1, 109, 95)]), ENTRY_TIME)
    check("同一バーで両到達 → SL優先 LOSE", st == "LOSE")

    # 時系列順序: TPが2h目、SLは10h目 → 先に到達したTPが勝つ
    st, ts, note = _resolve_trade(100, 96, 108, _candles([(2, 109, 100), (10, 100, 90)]), ENTRY_TIME)
    check("TP先着 → 後のSLに関係なくWIN", st == "WIN")

    # 21日経過 → EXPIRED (境界: ちょうど21日のバーで期限切れ)
    st, ts, note = _resolve_trade(100, 96, 108, _candles([(1, 101, 99), (21 * 24, 109, 90)]), ENTRY_TIME)
    check("21日後のバー → 到達より期限切れ優先 EXPIRED", st == "EXPIRED")

    st, ts, note = _resolve_trade(100, 96, 108, _candles([(1, 101, 99), (2, 102, 100)]), ENTRY_TIME)
    check("データ端まで未決着 → OPEN", st == "OPEN")

    # entry_time 以前のバーは無視 (シグナルバー自身のヒゲで即決着しない)
    st, ts, note = _resolve_trade(100, 96, 108, _candles([(0, 120, 80), (1, 101, 99)]), ENTRY_TIME)
    check("entry_time以前のバーは無視", st == "OPEN")


def _ohlcv_df(n, start, step):
    closes = [start + i * step for i in range(n)]
    return pd.DataFrame({
        'timestamp': pd.date_range('2025-01-01', periods=n, freq='4h'),
        'open': closes,
        'high': [c * 1.01 for c in closes],
        'low': [c * 0.99 for c in closes],
        'close': closes,
        'volume': [1000.0] * n,
    })


def test_compute_indicators():
    print("[Test] _compute_indicators")
    ind = _compute_indicators(_ohlcv_df(300, 100.0, 0.1))
    expected_keys = {"price", "rsi", "ema50", "ema200", "ema200_valid", "regime",
                     "adx", "atr_pct", "atr_value", "dmp", "dmn", "is_sweep",
                     "sweep_level", "has_divergence", "is_falling_knife",
                     "trend", "change_24h"}
    check("全キー完備", expected_keys <= set(ind.keys()))
    check("300本: ema200_valid", ind["ema200_valid"] is True or ind["ema200_valid"] == True)
    check("300本上昇: trend=UP", ind["trend"] == "UP")
    check("atr_value > 0", ind["atr_value"] > 0)
    check("change_24h = 6本分の上昇", ind["change_24h"] > 0)

    # 100本ではEMA200がNaN → 現在価格フォールバック → trendはDOWNに倒れる
    # (live従来挙動の再現 = limit=100バグの検証用。300本で解消される)
    ind100 = _compute_indicators(_ohlcv_df(100, 100.0, 0.1))
    check("100本: ema200_valid=False (旧バグ再現)", not ind100["ema200_valid"])
    check("100本: trend=DOWN に倒れる", ind100["trend"] == "DOWN")

    df = _ohlcv_df(300, 100.0, 0.1)
    before = df.copy()
    _compute_indicators(df)
    check("入力dfを変更しない", df.equals(before))


if __name__ == "__main__":
    test_compute_levels()
    test_apply_daily_gate()
    test_daily_trend()
    test_resolve_trade()
    test_compute_indicators()
    print("\n" + "=" * 40)
    print(f"Result: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
