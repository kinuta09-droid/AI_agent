# -*- coding: utf-8 -*-
"""
Crypto Backtest Engine (Phase 3)

ライブ分析 (tools/tool_crypto.py) と同一の純関数 (lib/quant_core.py) で
過去データをリプレイし、「監査(22:30)が測っているものと同じトレード」
(シグナル価格エントリー / SL / 2Rターゲット / 21日期限) をシミュレートする。

CLAUDE.md Backtest Gate: lib/quant_core.py / tools/tool_crypto.py の
ロジック変更時は本スクリプトで before/after 比較が必須。

使い方:
  py scripts/backtest_crypto.py                              # 既定3銘柄×730日
  py scripts/backtest_crypto.py --compare fixed-vs-atr       # 旧固定% vs ATR水準
  py scripts/backtest_crypto.py --compare gate-vs-nogate     # 日足ゲート有無
  py scripts/backtest_crypto.py --compare window-100-vs-300  # EMA200バグ影響
  py scripts/backtest_crypto.py --symbols BTC/USDT --days 365 --sl-mult 1.5

既知の制約 (レポートにも自動記載):
- news_risk は常に NEUTRAL (Tavilyニュース検索は過去に遡って再現できない)
  → ライブの SYSTEMIC_RISK 遮断が効かない分、バックテストはやや楽観的
- 評価は4h足の確定毎。ライブは 08:30定時 + 急変ウォッチャー駆動で密度が異なる
- 同一1hバー内の SL/TP 両到達は SL 優先 (悲観側、ライブ監査と同じ)
"""
import os
import sys
import csv
import time
import argparse
import datetime
from dataclasses import dataclass, replace

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd

from lib.quant_core import (
    _compute_indicators, _decide_signal, _daily_trend, _apply_daily_gate,
    _compute_levels, _resolve_trade,
)
from lib import bt_data
from lib.paths import LOGS_DIR

BACKTEST_OUT_DIR = os.path.join(LOGS_DIR, "backtest")

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


@dataclass
class BTConfig:
    """既定値はライブ (tool_analyze_crypto の .env 既定) と一致させること。"""
    label: str = "default"
    atr_levels: bool = True          # False = 旧固定% (Stop -12%)
    sl_mult: float = 2.5
    dca_mults: tuple = (1.5, 3.0, 4.5)
    atr_floor_pct: float = 1.0
    atr_cap_pct: float = 4.0
    daily_gate: bool = False         # ライブ既定OFF (Step5のA/B結果に基づく)
    expiry_days: int = 21
    cooldown_hours: float = 0.0
    window_bars: int = 300           # 4h足の指標計算窓 (100=旧liveのEMA200バグ再現)


# R倍数: ターゲットは2R設計なので WIN=+2R / LOSE=-1R / EXPIRED=0R
R_BY_STATUS = {"WIN": 2.0, "LOSE": -1.0, "EXPIRED": 0.0, "OPEN": 0.0}


def run_symbol(symbol: str, df4h: pd.DataFrame, df1d: pd.DataFrame,
               df1h: pd.DataFrame, fng_history: dict, cfg: BTConfig,
               eval_start=None) -> list:
    """1銘柄分のリプレイ。4h足の確定毎に評価し、トレードのリストを返す。

    1銘柄1建玉制限: 建玉が決着するまで新規エントリーしない
    (ライブでも同一銘柄の重複予測は6hクールダウンで抑止されている)。
    """
    trades = []
    blocked_until = None
    far_future = df4h['timestamp'].iloc[-1] + datetime.timedelta(days=365)

    for i in range(cfg.window_bars, len(df4h)):
        bar_open = df4h['timestamp'].iloc[i]
        bar_close_time = bar_open + datetime.timedelta(hours=4)
        if eval_start is not None and bar_close_time < eval_start:
            continue
        if blocked_until is not None and bar_close_time < blocked_until:
            continue

        window = df4h.iloc[i - cfg.window_bars + 1: i + 1]
        ind = _compute_indicators(window)
        # F&G はバー開始日(UTC)の公表値 = その時点で公表済みの最新値
        fng = fng_history.get(bar_open.strftime("%Y-%m-%d"), 50)

        signal, confidence, signal_type, reasons = _decide_signal(
            "NEUTRAL", ind["is_falling_knife"], ind["has_divergence"],
            ind["is_sweep"], ind["sweep_level"], fng, ind["rsi"],
            ind["regime"], ind["trend"], ind["adx"])

        if signal not in ("BUY", "STRONG BUY"):
            continue

        gated = False
        if cfg.daily_gate:
            # バー確定時点で「確定済み」の日足のみ参照 (先読み禁止)
            closed_1d = df1d[df1d['timestamp'] + datetime.timedelta(days=1)
                             <= bar_close_time]
            dtrend = _daily_trend(closed_1d)
            signal, gate_note = _apply_daily_gate(signal, dtrend, enabled=True)
            if signal not in ("BUY", "STRONG BUY"):
                gated = True
        if gated:
            continue

        entry = ind["price"]
        if cfg.atr_levels:
            lv = _compute_levels(entry, ind["atr_value"], cfg.sl_mult,
                                 cfg.dca_mults, cfg.atr_floor_pct, cfg.atr_cap_pct)
            stop = lv["dca_stop"]
            atr_used_pct = lv["atr_used_pct"]
        else:
            stop = entry * 0.88  # 旧固定%
            atr_used_pct = 0.0
        risk = entry - stop
        target = entry + risk * 2 if risk > 0 else entry * 1.10

        # 決着判定用1h足: エントリー以降・期限+1日分のみ渡す (走査コスト削減)
        resolve_slice = df1h[
            (df1h['timestamp'] > bar_open) &
            (df1h['timestamp'] <= bar_close_time
             + datetime.timedelta(days=cfg.expiry_days + 1))]
        status, exit_time, note = _resolve_trade(
            entry, stop, target, resolve_slice, bar_close_time, cfg.expiry_days)

        holding_h = ((exit_time - bar_close_time).total_seconds() / 3600
                     if exit_time is not None else None)
        trades.append({
            "symbol": symbol,
            "signal": signal,
            "signal_type": signal_type or "unknown",
            "entry_time": bar_close_time,
            "entry": round(entry, 4),
            "stop": round(stop, 4),
            "target": round(target, 4),
            "stop_pct": round((stop / entry - 1) * 100, 2),
            "atr_used_pct": round(atr_used_pct, 2),
            "status": status,
            "exit_time": exit_time,
            "holding_h": round(holding_h, 1) if holding_h is not None else "",
            "r": R_BY_STATUS[status],
        })

        if exit_time is not None:
            blocked_until = exit_time + datetime.timedelta(hours=cfg.cooldown_hours)
        else:
            blocked_until = far_future

    return trades


def _fetch_all(symbols: list, days: int, cfg: BTConfig) -> dict:
    """全銘柄の 4h(+指標warmup) / 1d(+EMA200warmup) / 1h データを取得する。"""
    now = datetime.datetime.now()
    eval_start = now - datetime.timedelta(days=days)
    since_4h = int((eval_start - datetime.timedelta(hours=4 * (cfg.window_bars + 5))
                    ).timestamp() * 1000)
    since_1d = int((eval_start - datetime.timedelta(days=320)).timestamp() * 1000)
    since_1h = int(eval_start.timestamp() * 1000)

    data = {}
    for sym in symbols:
        print(f"  📥 Loading {sym} (4h/1d/1h)...")
        data[sym] = {
            "4h": bt_data.get_ohlcv(sym, "4h", since_4h),
            "1d": bt_data.get_ohlcv(sym, "1d", since_1d),
            "1h": bt_data.get_ohlcv(sym, "1h", since_1h),
        }
    return data, eval_start


def run_backtest(symbols: list, days: int, cfg: BTConfig,
                 data: dict = None, eval_start=None,
                 fng_history: dict = None) -> dict:
    """バックテスト実行 → {"config", "trades", "summary"}。

    data/fng_history はテスト注入用 (Noneなら bt_data から取得)。
    """
    if data is None:
        data, eval_start = _fetch_all(symbols, days, cfg)
    if fng_history is None:
        fng_history = bt_data.get_fng_history()

    all_trades = []
    for sym in symbols:
        d = data[sym]
        if len(d["4h"]) <= cfg.window_bars:
            print(f"  ⚠️ {sym}: 4hデータ不足 ({len(d['4h'])}本) — スキップ")
            continue
        t0 = time.time()
        trades = run_symbol(sym, d["4h"], d["1d"], d["1h"], fng_history, cfg,
                            eval_start=eval_start)
        print(f"  ✅ {sym} [{cfg.label}]: {len(trades)} trades ({time.time()-t0:.1f}s)")
        all_trades.extend(trades)

    return {"config": cfg, "trades": all_trades,
            "summary": summarize(all_trades)}


def summarize(trades: list) -> dict:
    """signal_type別 + 合計の集計 (勝率・期待値R・最大DD・平均保有時間)。"""
    def _bucket():
        return {"wins": 0, "losses": 0, "expired": 0, "open": 0,
                "trades": 0, "sum_r": 0.0, "sum_holding_h": 0.0, "n_holding": 0}

    per_type = {}
    total = _bucket()
    for t in trades:
        for b in (per_type.setdefault(t["signal_type"], _bucket()), total):
            b["trades"] += 1
            b["sum_r"] += t["r"]
            if t["status"] == "WIN":
                b["wins"] += 1
            elif t["status"] == "LOSE":
                b["losses"] += 1
            elif t["status"] == "EXPIRED":
                b["expired"] += 1
            else:
                b["open"] += 1
            if t["holding_h"] != "":
                b["sum_holding_h"] += t["holding_h"]
                b["n_holding"] += 1

    def _finalize(b):
        decided = b["wins"] + b["losses"]
        closed = decided + b["expired"]
        b["win_rate"] = b["wins"] / decided if decided else None
        b["expectancy_r"] = b["sum_r"] / closed if closed else None
        b["avg_holding_h"] = (b["sum_holding_h"] / b["n_holding"]
                              if b["n_holding"] else None)
        return b

    for b in per_type.values():
        _finalize(b)
    _finalize(total)

    # 最大ドローダウン (exit_time順の累積R)
    resolved = sorted([t for t in trades if t["exit_time"] is not None],
                      key=lambda t: t["exit_time"])
    peak = cum = 0.0
    max_dd = 0.0
    for t in resolved:
        cum += t["r"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    total["max_drawdown_r"] = max_dd
    total["cum_r"] = cum

    return {"per_type": per_type, "total": total}


# ---------------------------------------------------------
# レポート出力
# ---------------------------------------------------------

def _fmt(v, pct=False):
    if v is None:
        return "—"
    return f"{v*100:.1f}%" if pct else f"{v:.2f}"


def _summary_table(summary: dict) -> str:
    rows = ("| 種別 | Trades | WIN | LOSE | EXP | OPEN | 勝率 | 期待値R |\n"
            "|---|---|---|---|---|---|---|---|\n")
    items = sorted(summary["per_type"].items()) + [("**合計**", summary["total"])]
    for name, b in items:
        rows += (f"| {name} | {b['trades']} | {b['wins']} | {b['losses']} | "
                 f"{b['expired']} | {b['open']} | {_fmt(b['win_rate'], pct=True)} | "
                 f"{_fmt(b['expectancy_r'])} |\n")
    t = summary["total"]
    rows += (f"\n累積R: **{t['cum_r']:+.1f}** / 最大DD: {t['max_drawdown_r']:.1f}R / "
             f"平均保有: {_fmt(t['avg_holding_h'])}h\n")
    return rows


def _config_line(cfg: BTConfig) -> str:
    lv = (f"ATR (SL={cfg.sl_mult}×, DCA={cfg.dca_mults}, "
          f"floor={cfg.atr_floor_pct}%, cap={cfg.atr_cap_pct}%)"
          if cfg.atr_levels else "固定% (Stop -12%)")
    return (f"levels={lv} / daily_gate={'ON' if cfg.daily_gate else 'OFF'} / "
            f"window={cfg.window_bars}本 / expiry={cfg.expiry_days}日 / "
            f"cooldown={cfg.cooldown_hours}h")


def write_outputs(results: list, symbols: list, days: int) -> str:
    """結果(1つ以上=比較)を CSV + Markdown レポートに書き出す。"""
    os.makedirs(BACKTEST_OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")

    for res in results:
        cfg = res["config"]
        csv_path = os.path.join(BACKTEST_OUT_DIR,
                                f"bt_{stamp}_{cfg.label}_trades.csv")
        fields = ["symbol", "signal", "signal_type", "entry_time", "entry",
                  "stop", "target", "stop_pct", "atr_used_pct", "status",
                  "exit_time", "holding_h", "r"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(res["trades"])
        res["csv_path"] = csv_path

    report = (f"# 🧪 Crypto Backtest Report ({stamp})\n\n"
              f"- 銘柄: {', '.join(symbols)}\n"
              f"- 期間: 直近{days}日 (4h足評価 / 1h足決着 / news_risk=NEUTRAL固定)\n\n")
    for res in results:
        cfg = res["config"]
        report += (f"## [{cfg.label}] {_config_line(cfg)}\n\n"
                   f"{_summary_table(res['summary'])}\n"
                   f"Trades CSV: `{res['csv_path']}`\n\n")

    if len(results) == 2:
        a, b = results[0], results[1]
        ta, tb = a["summary"]["total"], b["summary"]["total"]
        report += (f"## ⚖️ 比較サマリ ({a['config'].label} vs {b['config'].label})\n\n"
                   f"| 指標 | {a['config'].label} | {b['config'].label} |\n|---|---|---|\n"
                   f"| Trades | {ta['trades']} | {tb['trades']} |\n"
                   f"| 勝率 | {_fmt(ta['win_rate'], pct=True)} | {_fmt(tb['win_rate'], pct=True)} |\n"
                   f"| 期待値R | {_fmt(ta['expectancy_r'])} | {_fmt(tb['expectancy_r'])} |\n"
                   f"| 累積R | {ta['cum_r']:+.1f} | {tb['cum_r']:+.1f} |\n"
                   f"| 最大DD (R) | {ta['max_drawdown_r']:.1f} | {tb['max_drawdown_r']:.1f} |\n"
                   f"| EXPIRED | {ta['expired']} | {tb['expired']} |\n\n")

    report += ("## ⚠️ 制約\n"
               "- news_risk=NEUTRAL固定 (SYSTEMIC_RISK遮断が効かない分やや楽観的)\n"
               "- 4h足確定毎の評価 (ライブは定時+急変駆動でシグナル密度が異なる)\n"
               "- 同一1hバー内SL/TP両到達はSL優先 (悲観側)\n")

    # 同一分内の連続実行で上書きしないよう、ラベルもファイル名に含める
    labels = "-".join(res["config"].label for res in results)
    report_path = os.path.join(BACKTEST_OUT_DIR, f"bt_{stamp}_{labels}_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    return report_path


# ---------------------------------------------------------
# CLI
# ---------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Crypto Backtest (Phase 3)")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--compare", default="none",
                   choices=["none", "fixed-vs-atr", "gate-vs-nogate",
                            "window-100-vs-300"])
    p.add_argument("--sl-mult", type=float, default=2.5)
    p.add_argument("--dca-mults", default="1.5,3.0,4.5")
    p.add_argument("--atr-floor-pct", type=float, default=1.0)
    p.add_argument("--atr-cap-pct", type=float, default=4.0)
    p.add_argument("--daily-gate", action="store_true",
                   help="日足ハードゲートを有効化 (既定OFF=ライブ既定と一致)")
    p.add_argument("--cooldown-hours", type=float, default=0.0)
    p.add_argument("--window-bars", type=int, default=300)
    p.add_argument("--label", default="default")
    args = p.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    dca_mults = tuple(float(x) for x in args.dca_mults.split(","))
    if len(dca_mults) != 3:
        p.error("--dca-mults はカンマ区切りで3値必須 (例: 1.5,3.0,4.5)")
    base = BTConfig(
        label=args.label,
        sl_mult=args.sl_mult,
        dca_mults=dca_mults,
        atr_floor_pct=args.atr_floor_pct,
        atr_cap_pct=args.atr_cap_pct,
        daily_gate=args.daily_gate,
        cooldown_hours=args.cooldown_hours,
        window_bars=args.window_bars,
    )

    if args.compare == "fixed-vs-atr":
        # 旧live相当 (固定%・ゲートなし) vs 提案構成
        configs = [replace(base, label="fixed", atr_levels=False, daily_gate=False),
                   replace(base, label="atr")]
    elif args.compare == "gate-vs-nogate":
        configs = [replace(base, label="nogate", daily_gate=False),
                   replace(base, label="gate", daily_gate=True)]
    elif args.compare == "window-100-vs-300":
        configs = [replace(base, label="win100", window_bars=100),
                   replace(base, label="win300", window_bars=300)]
    else:
        configs = [base]

    print(f"[Backtest] {len(symbols)}銘柄 × {args.days}日 × {len(configs)}構成")
    data, eval_start = _fetch_all(symbols, args.days,
                                  max(configs, key=lambda c: c.window_bars))
    fng = bt_data.get_fng_history()

    results = []
    for cfg in configs:
        print(f"[Backtest] Running config: {cfg.label}")
        results.append(run_backtest(symbols, args.days, cfg, data=data,
                                    eval_start=eval_start, fng_history=fng))

    report_path = write_outputs(results, symbols, args.days)
    print(f"\n✅ Report: {report_path}")
    for res in results:
        t = res["summary"]["total"]
        print(f"  [{res['config'].label}] trades={t['trades']} "
              f"win_rate={_fmt(t['win_rate'], pct=True)} "
              f"expectancy={_fmt(t['expectancy_r'])}R cum={t['cum_r']:+.1f}R")


if __name__ == "__main__":
    main()
