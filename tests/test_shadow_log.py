# -*- coding: utf-8 -*-
"""
Phase 2 (記録層の復活) のユニットテスト:
- _has_open_record / _save_prediction: 1銘柄1建玉のロック内重複抑止
- record_kind: observation が Kelly の実測勝率を汚染しないこと
- split_by_kind: ダッシュボード/週次の集計分離
- 監査の決着判定が lib/quant_core._resolve_trade に統一されたこと
実API・実Vaultは使わない (TRADE_LOG_PATH を一時ファイルへ差替え)。
"""
import sys
import os
import tempfile
import datetime
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd

import tools.tool_crypto as tc
from tools.tool_crypto_stats import split_by_kind, compute_stats
from lib.quant_core import _resolve_trade

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
    """TRADE_LOG_PATH を一時ファイルに差し替えるコンテキスト。"""

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


def _save(symbol="BTC/USDT", signal="BUY", kind=tc.RECORD_KIND_SIGNAL,
          entry=100.0, stop=90.0):
    return tc._save_prediction(symbol, signal, entry, stop, 5.0, ["test"],
                               signal_type="smc_sweep", record_kind=kind)


# ---------------------------------------------------------

def test_has_open_record():
    print("[Test] _has_open_record")
    logs = [
        {"symbol": "BTC/USDT", "status": "OPEN", "record_kind": "signal"},
        {"symbol": "ETH/USDT", "status": "WIN", "record_kind": "signal"},
        {"symbol": "SOL/USDT", "status": "OPEN", "record_kind": "observation"},
        {"symbol": "XRP/USDT", "status": "OPEN"},  # 旧レコード = signal 扱い
        "破損レコード",
    ]
    check("同一銘柄・同一種別のOPENを検出",
          tc._has_open_record(logs, "BTC/USDT", "signal"))
    check("決着済みは重複扱いしない",
          not tc._has_open_record(logs, "ETH/USDT", "signal"))
    check("種別違いは別枠",
          not tc._has_open_record(logs, "SOL/USDT", "signal"))
    check("observation側は検出",
          tc._has_open_record(logs, "SOL/USDT", "observation"))
    check("record_kind欠落は signal 扱い (後方互換)",
          tc._has_open_record(logs, "XRP/USDT", "signal"))
    check("破損レコードでクラッシュしない",
          not tc._has_open_record(logs, "DOGE/USDT", "signal"))


def test_dedup_on_save():
    print("[Test] _save_prediction の重複抑止")
    with TempLog():
        first = _save()
        second = _save()
        logs = tc._load_trade_log()
        check("1件目は記録される", first is not None)
        check("2件目はスキップ (None を返す)", second is None)
        check("ログは1件のまま", len(logs) == 1)
        check("record_kind が入る", logs[0]["record_kind"] == "signal")

        # 種別が違えば同一銘柄でも記録できる (対照群は別枠)
        obs = _save(kind=tc.RECORD_KIND_OBSERVATION, signal="WATCH LIST")
        check("observation は別枠で記録される", obs is not None)
        check("ログは2件", len(tc._load_trade_log()) == 2)

        # 決着させれば同じ種別で再記録できる
        logs = tc._load_trade_log()
        logs[0]["status"] = "LOSE"
        tc._save_trade_log(logs)
        third = _save()
        check("決着後は同一銘柄でも再記録できる", third is not None)


def test_dedup_is_atomic():
    print("[Test] 重複チェックがロック内で行われる (並行実行)")
    with TempLog():
        results = []
        lock = threading.Lock()

        def worker():
            rid = _save()
            with lock:
                results.append(rid)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        logs = tc._load_trade_log()
        saved = [r for r in results if r is not None]
        check("8スレッド同時でも記録は1件だけ", len(logs) == 1)
        check("id を返したスレッドも1つだけ", len(saved) == 1)


def test_observation_excluded_from_kelly():
    print("[Test] observation が Kelly の実測勝率を汚染しない")
    logs = (
        [{"status": "WIN", "signal_type": "smc_sweep",
          "record_kind": "signal"}] * 3
        + [{"status": "LOSE", "signal_type": "smc_sweep",
            "record_kind": "signal"}] * 1
        # observation は 100% 勝っていても混ざってはいけない
        + [{"status": "WIN", "signal_type": "smc_sweep",
            "record_kind": "observation"}] * 20
        + [{"status": "WIN", "signal_type": "rsi_oversold"}]  # 旧=signal扱い
    )
    stats = tc._get_signal_stats(logs)
    check("smc_sweep は signal の 3W/1L のみ",
          stats["smc_sweep"]["wins"] == 3 and stats["smc_sweep"]["losses"] == 1)
    check("勝率は 0.75 のまま (observation で 0.96 にならない)",
          abs(stats["smc_sweep"]["win_rate"] - 0.75) < 1e-9)
    check("record_kind欠落は集計に含まれる (後方互換)",
          stats["rsi_oversold"]["wins"] == 1)

    # _resolve_win_rate も observation で n>=20 に達しない
    with TempLog():
        tc._save_trade_log(logs)
        wr, basis, n = tc._resolve_win_rate("smc_sweep", 0.65)
        check("n=4 のままなので既定値を使う",
              basis == "既定値" and n == 4 and wr == 0.65)


def test_split_by_kind():
    print("[Test] split_by_kind (ダッシュボード集計の分離)")
    logs = [
        {"status": "WIN", "signal_type": "a", "record_kind": "signal"},
        {"status": "LOSE", "signal_type": "a", "record_kind": "observation"},
        {"status": "OPEN", "signal_type": "b"},  # 旧レコード
        "破損",
    ]
    sig, obs = split_by_kind(logs)
    check("signal 2件 (旧レコード込み)", len(sig) == 2)
    check("observation 1件", len(obs) == 1)
    check("破損レコードは除外", all(isinstance(x, dict) for x in sig + obs))
    check("signal側の勝率に observation の LOSE が入らない",
          compute_stats(sig)["total"]["losses"] == 0)


def test_audit_resolution_shared():
    print("[Test] 監査の決着判定がバックテストと同じ純関数を通る")
    # TP 先着 → 後日 SL のケース。旧監査は窓全体の min/max で SL 優先だったため
    # LOSE と誤判定していた (バックテストは時系列走査なので WIN)
    base = datetime.datetime(2026, 1, 1)
    candles = pd.DataFrame({
        "timestamp": [base + datetime.timedelta(hours=i) for i in range(4)],
        "high": [105.0, 121.0, 110.0, 105.0],
        "low": [99.0, 100.0, 95.0, 80.0],
    })
    status, _exit, _note = _resolve_trade(100.0, 90.0, 120.0, candles, base)
    check("TP先着→後日SL は WIN (旧実装は LOSE だった)", status == "WIN")

    # SL 先着なら LOSE
    candles2 = pd.DataFrame({
        "timestamp": [base + datetime.timedelta(hours=i) for i in range(3)],
        "high": [105.0, 106.0, 130.0],
        "low": [99.0, 85.0, 100.0],
    })
    status2, _e2, _n2 = _resolve_trade(100.0, 90.0, 120.0, candles2, base)
    check("SL先着は LOSE", status2 == "LOSE")

    check("tool_crypto が _resolve_trade を保持している",
          hasattr(tc, "_resolve_trade"))
    check("EXPIRY は21日で共有", tc.AUDIT_EXPIRY_DAYS == 21)


if __name__ == "__main__":
    print("=" * 60)
    print("記録層 (record_kind / 重複抑止 / 監査統一) テスト")
    print("=" * 60)
    test_has_open_record()
    test_dedup_on_save()
    test_dedup_is_atomic()
    test_observation_excluded_from_kelly()
    test_split_by_kind()
    test_audit_resolution_shared()
    print("=" * 60)
    print(f"PASS: {PASS} / FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)
