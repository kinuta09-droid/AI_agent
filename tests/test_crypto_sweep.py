# -*- coding: utf-8 -*-
"""
Phase 3 (評価密度 + /scan フォーク解消) のユニットテスト:
- _evaluate_symbol が _decide_signal を通ること (独自RSI判定のフォーク解消)
- _record_scan_results の分岐 (BUY系=フル分析 / WATCH LIST=観測記録 / 他=無記録)
- OPEN重複時はフル分析を起動しない (課金を先に止める)
- 予算超過時もフル分析だけ止まり、無料の観測記録は続く
- _resolve_levels の ATR/固定% 切替
実API・実Vaultは使わない (TRADE_LOG_PATH / tool_analyze_crypto / SWEEP_COUNTER を差替え)。
"""
import sys
import os
import re
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd

import tools.tool_crypto as tc
from lib.quant_core import _compute_indicators, _decide_signal
from lib.cost_guard import GeminiBudgetExceeded

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


class TempLog:
    def __enter__(self):
        self._orig = tc.TRADE_LOG_PATH
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        os.unlink(tmp.name)
        tc.TRADE_LOG_PATH = tmp.name
        self.path = tmp.name
        return self

    def __exit__(self, *exc):
        tc.TRADE_LOG_PATH = self._orig
        for p in (self.path, self.path + ".lock"):
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


class FakeCounter:
    """SWEEP_COUNTER の差替え。limit 回まで通し、以降は予算超過を送出。"""

    def __init__(self, limit=99):
        self.limit = limit
        self.count = 0

    def check_and_increment(self, n=1):
        if self.count >= self.limit:
            raise GeminiBudgetExceeded("test", self.limit)
        self.count += n
        return self.count


def make_df(prices: list) -> pd.DataFrame:
    """終値リストから4h足DataFrameを組む (高値/安値は終値±0.5%)。"""
    return pd.DataFrame({
        "timestamp": range(len(prices)),
        "open": prices,
        "high": [p * 1.005 for p in prices],
        "low": [p * 0.995 for p in prices],
        "close": prices,
        "volume": [1000.0] * len(prices),
    })


def declining_df(n=300) -> pd.DataFrame:
    """緩やかな下降で終わる系列 (RSIが30を割る)。"""
    prices = [100.0 + (i % 7) * 0.2 for i in range(n - 30)]
    prices += [prices[-1] * (1 - 0.004 * (i + 1)) for i in range(30)]
    return make_df(prices)


# ---------------------------------------------------------

def test_no_signal_fork():
    print("[Test] スキャンの判定が _decide_signal と一致する (フォーク解消)")
    df = declining_df()
    fng = 15  # 旧スキャンなら RSI<30 かつ F&G<20 で無条件 STRONG BUY

    ind = _compute_indicators(df)
    expected, _conf, expected_type, _r = _decide_signal(
        "NEUTRAL", ind["is_falling_knife"], ind["has_divergence"],
        ind["is_sweep"], ind["sweep_level"], fng, ind["rsi"],
        ind["regime"], ind["trend"], ind["adx"])

    r = tc._evaluate_symbol("BTC/USDT", df, fng)
    check(f"RSI={ind['rsi']:.1f} で共有ロジックと同じ判定 ({r['signal']})",
          r["signal"] == expected)
    check("signal_type も共有ロジック由来", r["signal_type"] == expected_type)
    check("旧ルール (RSI<30 かつ F&G<20 = 無条件 STRONG BUY) ではない",
          not (ind["rsi"] < 30 and fng < 20 and r["signal"] == "STRONG BUY"
               and expected != "STRONG BUY"))
    check("レジームを保持 (レポート列用)", r["regime"] in
          ("TRENDING", "SIDEWAYS", "VOLATILE"))
    check("ATR値を保持 (観測記録のSL算出用)", r["atr_value"] >= 0)


def test_no_local_signal_rule_left():
    print("[Test] tool_crypto.py に独自シグナル判定が残っていない (静的スキャン)")
    src = open(tc.__file__, encoding="utf-8").read()
    # 判定は lib/quant_core._decide_signal のみが行う。tool_crypto 側で
    # signal に BUY 系を代入していたら独自ルールの再混入
    assigns = re.findall(r'^\s*signal\s*=\s*["\'](?:STRONG )?BUY["\']',
                         src, re.MULTILINE)
    check("signal への BUY 系直接代入がない", not assigns)
    check("_decide_signal を経由している", "_decide_signal(" in src)


def test_record_branches():
    print("[Test] _record_scan_results の分岐")
    with TempLog():
        called = []
        orig_analyze, orig_counter = tc.tool_analyze_crypto, tc.SWEEP_COUNTER
        tc.tool_analyze_crypto = lambda s: called.append(s)
        tc.SWEEP_COUNTER = FakeCounter()
        try:
            results = [
                {"symbol": "BTC/USDT", "signal": "STRONG BUY",
                 "signal_type": "smc_sweep", "confidence": 0.65,
                 "price": 100.0, "atr_value": 2.0, "reasons": ["sweep"]},
                {"symbol": "ETH/USDT", "signal": "WATCH LIST",
                 "signal_type": "rsi_oversold", "confidence": 0.35,
                 "price": 50.0, "atr_value": 1.0, "reasons": ["rsi"]},
                {"symbol": "SOL/USDT", "signal": "NEUTRAL",
                 "signal_type": None, "confidence": 0.0,
                 "price": 20.0, "atr_value": 0.5, "reasons": []},
                {"symbol": "XRP/USDT", "signal": "STAY AWAY",
                 "signal_type": None, "confidence": 0.0,
                 "price": 1.0, "atr_value": 0.02, "reasons": []},
            ]
            rec = tc._record_scan_results(results)
            logs = tc._load_trade_log()

            check("BUY系はフル分析を起動", called == ["BTC/USDT"])
            check("フル分析起動が戻り値に載る", rec["triggered"] == ["BTC/USDT"])
            check("WATCH LIST を observation で記録", rec["observed"] == ["ETH/USDT"])
            check("記録は observation の1件のみ (BUY系の記録はフル分析側の責務)",
                  len(logs) == 1 and logs[0]["symbol"] == "ETH/USDT")
            check("observation の record_kind",
                  logs[0]["record_kind"] == tc.RECORD_KIND_OBSERVATION)
            check("SLがATR基準で入る (entry未満)",
                  0 < logs[0]["stop_loss"] < logs[0]["entry_price"])
            check("NEUTRAL/STAY AWAY は記録しない",
                  all(t["symbol"] != "SOL/USDT" and t["symbol"] != "XRP/USDT"
                      for t in logs))
        finally:
            tc.tool_analyze_crypto, tc.SWEEP_COUNTER = orig_analyze, orig_counter


def test_open_duplicate_skips_before_paying():
    print("[Test] OPEN重複はフル分析を起動する前に止める")
    with TempLog():
        called = []
        orig_analyze, orig_counter = tc.tool_analyze_crypto, tc.SWEEP_COUNTER
        tc.tool_analyze_crypto = lambda s: called.append(s)
        counter = FakeCounter()
        tc.SWEEP_COUNTER = counter
        try:
            tc._save_trade_log([{
                "id": "x", "symbol": "BTC/USDT", "status": "OPEN",
                "record_kind": tc.RECORD_KIND_SIGNAL,
            }])
            rec = tc._record_scan_results([
                {"symbol": "BTC/USDT", "signal": "BUY", "signal_type": "smc_sweep",
                 "confidence": 0.65, "price": 100.0, "atr_value": 2.0,
                 "reasons": []},
            ])
            check("フル分析は呼ばれない", called == [])
            check("予算カウンタも消費しない", counter.count == 0)
            check("スキップとして報告される", rec["skipped"] == ["BTC/USDT"])
        finally:
            tc.tool_analyze_crypto, tc.SWEEP_COUNTER = orig_analyze, orig_counter


def test_budget_exhaustion():
    print("[Test] 予算超過でもフル分析だけ止まり無料の観測記録は続く")
    with TempLog():
        called = []
        orig_analyze, orig_counter = tc.tool_analyze_crypto, tc.SWEEP_COUNTER
        tc.tool_analyze_crypto = lambda s: called.append(s)
        tc.SWEEP_COUNTER = FakeCounter(limit=1)
        try:
            results = [
                {"symbol": "BTC/USDT", "signal": "BUY", "signal_type": "smc_sweep",
                 "confidence": 0.65, "price": 100.0, "atr_value": 2.0, "reasons": []},
                {"symbol": "ETH/USDT", "signal": "BUY", "signal_type": "smc_sweep",
                 "confidence": 0.65, "price": 50.0, "atr_value": 1.0, "reasons": []},
                {"symbol": "SOL/USDT", "signal": "WATCH LIST",
                 "signal_type": "rsi_oversold", "confidence": 0.35,
                 "price": 20.0, "atr_value": 0.5, "reasons": []},
            ]
            rec = tc._record_scan_results(results)
            check("1件目だけフル分析が走る", called == ["BTC/USDT"])
            check("予算超過が報告される", rec["budget_hit"] is True)
            check("無料の観測記録は続行", rec["observed"] == ["SOL/USDT"])
        finally:
            tc.tool_analyze_crypto, tc.SWEEP_COUNTER = orig_analyze, orig_counter


def test_resolve_levels():
    print("[Test] _resolve_levels (フル分析とスイープで共用)")
    orig = os.environ.get("CRYPTO_ATR_LEVELS")
    try:
        os.environ.pop("CRYPTO_ATR_LEVELS", None)
        lv = tc._resolve_levels(100.0, 2.0)
        check("既定は ATR モード", lv["mode"] == "atr")
        check("SL は entry 未満", 0 < lv["dca_stop"] < 100.0)
        check("DCA は3段とも entry 未満で降順",
              100.0 > lv["dca_entry1"] > lv["dca_entry2"] > lv["dca_entry3"])
        check("生の levels を保持 (トレーサビリティ用)",
              lv["levels"] is not None and "atr_used_pct" in lv["levels"])

        os.environ["CRYPTO_ATR_LEVELS"] = "0"
        fixed = tc._resolve_levels(100.0, 2.0)
        check("キルスイッチで固定%へ復帰", fixed["mode"] == "fixed")
        check("固定%の SL は -12%", abs(fixed["dca_stop"] - 88.0) < 1e-9)
        check("固定%では levels=None", fixed["levels"] is None)
    finally:
        os.environ.pop("CRYPTO_ATR_LEVELS", None)
        if orig is not None:
            os.environ["CRYPTO_ATR_LEVELS"] = orig


def test_scheduler_registration():
    print("[Test] スケジューラ登録 (4h足クローズ直後・silent)")
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "agent_scheduler.py"), encoding="utf-8").read()
    check("tool_crypto_sweep を import", "tool_crypto_sweep" in src)
    # UTC 0/4/8/12/16/20 の確定直後 = JST 09/13/17/21/01/05。
    # 09:05 は既存の 09:00 Market Scan が兼ねるため登録しない
    for t in ("01:05", "05:05", "13:05", "17:05", "21:05"):
        check(f"{t} に登録", f'"{t}"' in src)
    check("09:05 は重複登録しない", '"09:05"' not in src)


if __name__ == "__main__":
    print("=" * 60)
    print("Crypto Sweep / スキャンのフォーク解消 テスト")
    print("=" * 60)
    test_no_signal_fork()
    test_no_local_signal_rule_left()
    test_record_branches()
    test_open_duplicate_skips_before_paying()
    test_budget_exhaustion()
    test_resolve_levels()
    test_scheduler_registration()
    print("=" * 60)
    print(f"PASS: {PASS} / FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)
