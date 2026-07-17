# -*- coding: utf-8 -*-
"""
Phase 3 バックテストエンジンのユニットテスト (実API・実ファイル不使用):
- 合成OHLCVで WIN / LOSE / EXPIRED の決着
- 1銘柄1建玉制限 (トレード区間の非重複)
- 先読み禁止 (未来バー改変でエントリー不変)
- fixed / ATR 水準の切替
- 日足ハードゲートの遮断

_decide_signal はモジュール属性を差し替えて決定論化する
(fng_value==1 の日だけ BUY を返すフェイク。F&G注入で日単位に発火を制御)。
"""
import sys
import os
import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd

import backtest_crypto as bt

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


def _fake_decide(news_risk, is_falling_knife, has_divergence, is_sweep,
                 sweep_level, fng_value, current_rsi, regime, trend, adx):
    """fng_value==1 (テスト側がF&G注入で制御) の時だけBUY。"""
    if fng_value == 1:
        return "BUY", 0.55, "test_type", ["fake"]
    return "NEUTRAL", 0.0, None, []


START = datetime.datetime(2026, 1, 1, 0, 0, 0)


def _df4h(n=200, price=100.0):
    """フラットな4h足 (high=+1%, low=-1% → ATR≒2 = 2%)"""
    ts = pd.date_range(START, periods=n, freq='4h')
    return pd.DataFrame({
        'timestamp': ts,
        'open': [price] * n, 'high': [price * 1.01] * n,
        'low': [price * 0.99] * n, 'close': [price] * n,
        'volume': [1000.0] * n,
    })


def _df1h(days=60, price=100.0, spikes=None):
    """フラットな1h足。spikes: {datetime: (high, low)} で特定バーを上書き。"""
    n = days * 24
    ts = pd.date_range(START, periods=n, freq='h')
    df = pd.DataFrame({
        'timestamp': ts,
        'high': [price * 1.005] * n, 'low': [price * 0.995] * n,
    })
    if spikes:
        for t, (hi, lo) in spikes.items():
            df.loc[df['timestamp'] == t, ['high', 'low']] = [hi, lo]
    return df


def _df1d(n=250, start_price=100.0, step=0.0, end=None):
    """日足 (既定: 4h足の開始より十分前に終わる期間)。step<0で下降トレンド。"""
    end = end or (START - datetime.timedelta(days=1))
    ts = pd.date_range(end=end, periods=n, freq='D')
    closes = [start_price + i * step for i in range(n)]
    return pd.DataFrame({
        'timestamp': ts,
        'open': closes, 'high': [c * 1.01 for c in closes],
        'low': [c * 0.99 for c in closes], 'close': closes,
        'volume': [1000.0] * n,
    })


def _fng(dates_value_1):
    return {d: 1 for d in dates_value_1}


CFG = bt.BTConfig(label="test", window_bars=30, daily_gate=False,
                  atr_floor_pct=1.0, atr_cap_pct=4.0, sl_mult=2.0)

# ATR≒2 (2%) → stop = 100-2×2 = 96, target = 100+2×(100-96) = 108
ENTRY_DAY = "2026-01-10"
# 01-10 の最初の4h足 (00:00-04:00) がBUY → entry_time = 04:00
EXPECTED_ENTRY = datetime.datetime(2026, 1, 10, 4, 0, 0)


def test_win_lose_expired():
    print("[Test] WIN / LOSE / EXPIRED 決着")
    orig = bt._decide_signal
    bt._decide_signal = _fake_decide
    try:
        # WIN: エントリー30h後に high=110 (>108)
        win_spike = {EXPECTED_ENTRY + datetime.timedelta(hours=30): (110.0, 99.5)}
        trades = bt.run_symbol("TEST/USDT", _df4h(), _df1d(), _df1h(spikes=win_spike),
                               _fng([ENTRY_DAY]), CFG)
        check("1トレードのみ発生", len(trades) == 1)
        t = trades[0]
        check("entry_time = シグナルバー確定時刻", t["entry_time"] == EXPECTED_ENTRY)
        check("stop = 96 (2×ATR)", abs(t["stop"] - 96.0) < 0.1)
        check("target = 108 (2R)", abs(t["target"] - 108.0) < 0.2)
        check("WIN判定", t["status"] == "WIN")
        check("R = +2", t["r"] == 2.0)

        # LOSE: エントリー30h後に low=95 (<96)
        lose_spike = {EXPECTED_ENTRY + datetime.timedelta(hours=30): (100.5, 95.0)}
        trades = bt.run_symbol("TEST/USDT", _df4h(), _df1d(), _df1h(spikes=lose_spike),
                               _fng([ENTRY_DAY]), CFG)
        check("LOSE判定", len(trades) == 1 and trades[0]["status"] == "LOSE")
        check("R = -1", trades[0]["r"] == -1.0)

        # EXPIRED: スパイクなし (フラットのままSL/TP未到達で21日経過)
        trades = bt.run_symbol("TEST/USDT", _df4h(), _df1d(), _df1h(),
                               _fng([ENTRY_DAY]), CFG)
        check("EXPIRED判定", len(trades) == 1 and trades[0]["status"] == "EXPIRED")
        check("R = 0", trades[0]["r"] == 0.0)
    finally:
        bt._decide_signal = orig


def test_one_position_no_overlap():
    print("[Test] 1銘柄1建玉 (区間非重複)")
    orig = bt._decide_signal
    bt._decide_signal = _fake_decide
    try:
        # 全日程でBUY条件 → 建玉決着(EXPIRED)まで次を開かない
        all_days = [(START + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(60)]
        trades = bt.run_symbol("TEST/USDT", _df4h(), _df1d(), _df1h(),
                               _fng(all_days), CFG)
        check("複数トレードが発生 (EXPIRED後に再エントリー)", len(trades) >= 2)
        ok = all(trades[i + 1]["entry_time"] >= trades[i]["exit_time"]
                 for i in range(len(trades) - 1))
        check("トレード区間が重複しない", ok)
    finally:
        bt._decide_signal = orig


def test_no_lookahead():
    print("[Test] 先読み禁止")
    orig = bt._decide_signal
    bt._decide_signal = _fake_decide
    try:
        df_a = _df4h()
        trades_a = bt.run_symbol("TEST/USDT", df_a, _df1d(), _df1h(),
                                 _fng([ENTRY_DAY]), CFG)
        # エントリーバー以降の4h足を全て2倍に改変
        df_b = _df4h()
        mask = df_b['timestamp'] > EXPECTED_ENTRY
        for col in ('open', 'high', 'low', 'close'):
            df_b.loc[mask, col] *= 2
        trades_b = bt.run_symbol("TEST/USDT", df_b, _df1d(), _df1h(),
                                 _fng([ENTRY_DAY]), CFG)
        same = (len(trades_a) == len(trades_b) == 1 and
                trades_a[0]["entry_time"] == trades_b[0]["entry_time"] and
                trades_a[0]["entry"] == trades_b[0]["entry"] and
                trades_a[0]["stop"] == trades_b[0]["stop"] and
                trades_a[0]["target"] == trades_b[0]["target"])
        check("未来バー改変でエントリー内容が不変", same)
    finally:
        bt._decide_signal = orig


def test_fixed_levels():
    print("[Test] fixed水準 (atr_levels=False)")
    orig = bt._decide_signal
    bt._decide_signal = _fake_decide
    try:
        cfg = bt.BTConfig(label="fixed", window_bars=30, daily_gate=False,
                          atr_levels=False)
        trades = bt.run_symbol("TEST/USDT", _df4h(), _df1d(), _df1h(),
                               _fng([ENTRY_DAY]), cfg)
        check("stop = -12% (旧固定)", len(trades) == 1 and
              abs(trades[0]["stop_pct"] - (-12.0)) < 0.01)
        check("target = 124 (2R)", abs(trades[0]["target"] - 124.0) < 0.2)
    finally:
        bt._decide_signal = orig


def test_daily_gate():
    print("[Test] 日足ハードゲート")
    orig = bt._decide_signal
    bt._decide_signal = _fake_decide
    try:
        cfg = bt.BTConfig(label="gate", window_bars=30, daily_gate=True)
        # 日足下降 (250日, step<0) → 全遮断
        down = _df1d(start_price=250.0, step=-0.5)
        trades = bt.run_symbol("TEST/USDT", _df4h(), down, _df1h(),
                               _fng([ENTRY_DAY]), cfg)
        check("日足DOWN → 0トレード", len(trades) == 0)
        # 日足上昇 → 素通し
        up = _df1d(start_price=100.0, step=0.5)
        trades = bt.run_symbol("TEST/USDT", _df4h(), up, _df1h(),
                               _fng([ENTRY_DAY]), cfg)
        check("日足UP → トレード成立", len(trades) == 1)
        # 日足データ不足 (150日) → UNKNOWN → フェイルオープンで素通し
        short = _df1d(n=150, start_price=250.0, step=-0.5)
        trades = bt.run_symbol("TEST/USDT", _df4h(), short, _df1h(),
                               _fng([ENTRY_DAY]), cfg)
        check("日足不足(UNKNOWN) → 素通し", len(trades) == 1)
    finally:
        bt._decide_signal = orig


def test_summarize():
    print("[Test] summarize 集計")
    trades = [
        {"signal_type": "a", "status": "WIN", "r": 2.0, "holding_h": 10,
         "exit_time": datetime.datetime(2026, 1, 2)},
        {"signal_type": "a", "status": "LOSE", "r": -1.0, "holding_h": 5,
         "exit_time": datetime.datetime(2026, 1, 3)},
        {"signal_type": "b", "status": "EXPIRED", "r": 0.0, "holding_h": 504,
         "exit_time": datetime.datetime(2026, 1, 25)},
        {"signal_type": "b", "status": "OPEN", "r": 0.0, "holding_h": "",
         "exit_time": None},
    ]
    s = bt.summarize(trades)
    check("type a: 勝率50%", abs(s["per_type"]["a"]["win_rate"] - 0.5) < 1e-9)
    check("type a: 期待値 (2-1)/2=0.5R", abs(s["per_type"]["a"]["expectancy_r"] - 0.5) < 1e-9)
    check("type b: 勝率None (判定0件)", s["per_type"]["b"]["win_rate"] is None)
    check("合計: closed 3件で期待値 1/3R", abs(s["total"]["expectancy_r"] - 1 / 3) < 1e-9)
    check("累積R = +1", abs(s["total"]["cum_r"] - 1.0) < 1e-9)
    check("最大DD = 1R (WIN後のLOSE)", abs(s["total"]["max_drawdown_r"] - 1.0) < 1e-9)


if __name__ == "__main__":
    test_win_lose_expired()
    test_one_position_no_overlap()
    test_no_lookahead()
    test_fixed_levels()
    test_daily_gate()
    test_summarize()
    print("\n" + "=" * 40)
    print(f"Result: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
