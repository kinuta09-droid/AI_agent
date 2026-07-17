# -*- coding: utf-8 -*-
"""
Phase 1 (feat/crypto-audit-loop) のユニットテスト:
- _get_signal_stats: 種別別勝率集計 / EXPIRED除外 / legacy分離
- _resolve_win_rate: 20件閾値での既定値→実測値切替 / 0.75クランプ
- _decide_signal: signal_type の優先順位 (smc_sweep > panic_reversal > ...)
実API・実Vaultは使わない（TRADE_LOG_PATHを一時ファイルへ差替え）。
"""
import sys
import os
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

import tools.tool_crypto as tc

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


def _record(status, signal_type=None):
    rec = {"status": status}
    if signal_type is not None:
        rec["signal_type"] = signal_type
    return rec


def test_get_signal_stats():
    print("[Test] _get_signal_stats")
    logs = (
        [_record("WIN", "smc_sweep")] * 3
        + [_record("LOSE", "smc_sweep")] * 1
        + [_record("WIN", "trend_pullback")] * 1
        + [_record("LOSE", "trend_pullback")] * 3
        + [_record("EXPIRED", "smc_sweep")] * 5      # 除外されるべき
        + [_record("OPEN", "smc_sweep")] * 2          # 除外されるべき
        + [_record("WIN")] * 2                        # signal_typeなし → legacy
    )
    stats = tc._get_signal_stats(logs)

    check("smc_sweep 3W/1L", stats["smc_sweep"]["wins"] == 3 and stats["smc_sweep"]["losses"] == 1)
    check("smc_sweep 勝率0.75", abs(stats["smc_sweep"]["win_rate"] - 0.75) < 1e-9)
    check("trend_pullback 勝率0.25", abs(stats["trend_pullback"]["win_rate"] - 0.25) < 1e-9)
    check("EXPIRED/OPENが件数に混入しない",
          stats["smc_sweep"]["wins"] + stats["smc_sweep"]["losses"] == 4)
    check("legacy分離集計", stats["legacy"]["wins"] == 2)


def test_resolve_win_rate():
    print("[Test] _resolve_win_rate")
    original_path = tc.TRADE_LOG_PATH
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    os.unlink(tmp.name)  # 存在しない状態から開始
    tc.TRADE_LOG_PATH = tmp.name
    try:
        # ログなし → 既定値
        wr, basis, n = tc._resolve_win_rate("smc_sweep", 0.65)
        check("ログなし → 既定値", wr == 0.65 and basis == "既定値" and n == 0)

        # signal_typeなし → 既定値
        wr, basis, n = tc._resolve_win_rate(None, 0.55)
        check("signal_type=None → 既定値", wr == 0.55 and basis == "既定値")

        # n=19 (12W/7L) → まだ既定値
        logs = [_record("WIN", "smc_sweep")] * 12 + [_record("LOSE", "smc_sweep")] * 7
        tc._save_trade_log(logs)
        wr, basis, n = tc._resolve_win_rate("smc_sweep", 0.65)
        check("n=19 → 既定値のまま", wr == 0.65 and basis == "既定値" and n == 19)

        # n=20 (12W/8L=0.60) → 実測値に切替
        logs.append(_record("LOSE", "smc_sweep"))
        tc._save_trade_log(logs)
        wr, basis, n = tc._resolve_win_rate("smc_sweep", 0.65)
        check("n=20 → 実測値0.60に切替", abs(wr - 0.60) < 1e-9 and basis == "実測" and n == 20)

        # 実測0.9 → 0.75にクランプ
        logs = [_record("WIN", "panic_reversal")] * 18 + [_record("LOSE", "panic_reversal")] * 2
        tc._save_trade_log(logs)
        wr, basis, n = tc._resolve_win_rate("panic_reversal", 0.65)
        check("実測0.90 → 0.75にクランプ", abs(wr - 0.75) < 1e-9 and basis == "実測")

        # 別種別はサンプル不足のまま既定値
        wr, basis, n = tc._resolve_win_rate("trend_pullback", 0.55)
        check("他種別は影響を受けない", wr == 0.55 and basis == "既定値" and n == 0)
    finally:
        tc.TRADE_LOG_PATH = original_path
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def test_decide_signal_priority():
    print("[Test] _decide_signal — signal_type優先順位")
    base = dict(news_risk="NEUTRAL", is_falling_knife=False, has_divergence=True,
                sweep_level=50000.0, regime="TRENDING", trend="UP", adx=30.0)

    # sweep + panic 同時成立 → smc_sweep が優先
    sig, conf, stype, reasons = tc._decide_signal(
        is_sweep=True, fng_value=10, current_rsi=20.0, **base)
    check("sweep+panic同時 → smc_sweep", sig == "STRONG BUY" and stype == "smc_sweep")
    check("両方の理由が残る", len(reasons) >= 2)

    # panic単独 (divergenceあり) → panic_reversal
    sig, conf, stype, _ = tc._decide_signal(
        is_sweep=False, fng_value=10, current_rsi=20.0, **base)
    check("panic単独 → panic_reversal / STRONG BUY", sig == "STRONG BUY" and stype == "panic_reversal")

    # トレンド押し目 → trend_pullback
    sig, conf, stype, _ = tc._decide_signal(
        is_sweep=False, fng_value=50, current_rsi=40.0, **base)
    check("押し目 → trend_pullback / BUY", sig == "BUY" and stype == "trend_pullback")

    # RSI売られすぎ+divergence (レジームSIDEWAYSで押し目条件を外す) → rsi_oversold
    args = dict(base, regime="SIDEWAYS")
    sig, conf, stype, _ = tc._decide_signal(
        is_sweep=False, fng_value=50, current_rsi=25.0, **args)
    check("RSI<30+div → rsi_oversold / BUY", sig == "BUY" and stype == "rsi_oversold")

    # WATCH LIST (divergenceなし) にも種別タグは付く
    args = dict(base, has_divergence=False, regime="SIDEWAYS")
    sig, conf, stype, _ = tc._decide_signal(
        is_sweep=False, fng_value=50, current_rsi=25.0, **args)
    check("RSI<30 divなし → WATCH LIST / rsi_oversold", sig == "WATCH LIST" and stype == "rsi_oversold")

    # SYSTEMIC_RISK は全てを無効化
    args = dict(base, news_risk="SYSTEMIC_RISK")
    sig, conf, stype, _ = tc._decide_signal(
        is_sweep=True, fng_value=10, current_rsi=20.0, **args)
    check("SYSTEMIC_RISK → STAY AWAY / 種別なし", sig == "STAY AWAY" and stype is None)

    # 落ちるナイフ (divergenceなし) → WAIT
    args = dict(base, is_falling_knife=True, has_divergence=False)
    sig, conf, stype, _ = tc._decide_signal(
        is_sweep=True, fng_value=50, current_rsi=40.0, **args)
    check("落ちるナイフ → WAIT / 種別なし", sig == "WAIT" and stype is None)


if __name__ == "__main__":
    test_get_signal_stats()
    test_resolve_win_rate()
    test_decide_signal_priority()
    print(f"\n{'='*40}\nResult: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
