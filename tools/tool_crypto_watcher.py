# -*- coding: utf-8 -*-
"""
Crypto Event Watcher (Phase 2: イベント駆動監視)

15分間隔の無料監視層。ccxt fetch_tickers (1コール・API課金なし) で
全スキャン銘柄の急変を検知し、閾値超過時のみ tool_analyze_crypto
(Gemini + Tavily = 有料層) を起動する。

「検知(無料・高頻度)」と「分析(有料・低頻度)」の分離が設計の骨子:
- トリガA: 15分変動が WATCH_DELTA_15M (既定±2.5%) 超 → フル分析
- トリガB: 1時間変動が WATCH_DELTA_1H (既定±4.0%) 超 → フル分析
- トリガC: OPEN予測のSL/TPに現在値が接近 → Discord通知のみ (無料)

暴走ガード:
- 銘柄別クールダウン (既定6h): 同一銘柄の連続分析を抑止
- cost_guard.WATCHER_COUNTER (既定8回/日): Gemini全体予算とは別枠の分析上限
"""
import os
import datetime

import ccxt

from lib.lib_shared import load_json, save_json, TRADE_LOG_PATH
from lib.cost_guard import WATCHER_COUNTER, GeminiBudgetExceeded
from lib.watchdog import notify_discord
from tools.tool_crypto import SCAN_SYMBOLS, tool_analyze_crypto

WATCH_STATE_PATH = os.path.join("C:\\AI_agent", "logs", "crypto_watch_state.json")

# スナップショット保持数 (15分×8 = 2時間分。1hトリガ判定に必要な60-90分+余裕)
SNAPSHOT_KEEP = 8


def _get_float_env(name: str, default: float) -> float:
    """環境変数からfloatを読む。未設定/パース不能は default。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_dt(s):
    """ISO文字列→datetime。パース不能は None (状態ファイル破損に耐える)。"""
    try:
        return datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _reference_price(snapshots: list, symbol: str, now: datetime.datetime,
                     target_min: float, min_age_min: float, max_age_min: float):
    """年齢が [min_age, max_age] 分に収まるスナップショットのうち、
    target_min に最も近いものの価格を返す (該当なしは None)。

    年齢範囲を強制するのは、スケジューラ停止などで古いスナップショット
    しか無い場合に、それを「15分前の価格」として評価すると数時間分の
    変動を15分変動と誤認して誤発火するため。
    """
    best_price = None
    best_diff = None
    for snap in snapshots:
        ts = _parse_dt(snap.get("ts"))
        if ts is None:
            continue
        age_min = (now - ts).total_seconds() / 60.0
        if not (min_age_min <= age_min <= max_age_min):
            continue
        price = snap.get("prices", {}).get(symbol)
        if not price:
            continue
        diff = abs(age_min - target_min)
        if best_diff is None or diff < best_diff:
            best_price = float(price)
            best_diff = diff
    return best_price


def _decide_watch_actions(prices: dict, snapshots: list, cooldowns: dict,
                          open_trades: list, sltp_notified: dict,
                          now: datetime.datetime,
                          delta_15m: float = 2.5, delta_1h: float = 4.0,
                          proximity_pct: float = 1.5) -> tuple:
    """トリガ判定ロジック (純関数・I/Oなし)。

    Returns: (analyze_targets, sltp_alerts)
      analyze_targets: [{"symbol", "reason"}]  — フル分析を起動すべき銘柄
      sltp_alerts: [{"trade_id", "symbol", "kind": "SL"|"TP", "level", "price"}]
    """
    today_str = now.strftime("%Y-%m-%d")

    # --- トリガA/B: 急変検知 ---
    analyze_targets = []
    for symbol, price in prices.items():
        until = _parse_dt(cooldowns.get(symbol))
        if until is not None and until > now:
            continue  # クールダウン中

        reason = None
        ref15 = _reference_price(snapshots, symbol, now, 15, 5, 25)
        if ref15:
            chg = (price - ref15) / ref15 * 100
            if abs(chg) >= delta_15m:
                reason = f"15分変動 {chg:+.1f}% (閾値±{delta_15m}%)"
        if reason is None:
            ref1h = _reference_price(snapshots, symbol, now, 60, 45, 90)
            if ref1h:
                chg = (price - ref1h) / ref1h * 100
                if abs(chg) >= delta_1h:
                    reason = f"1時間変動 {chg:+.1f}% (閾値±{delta_1h}%)"
        if reason:
            analyze_targets.append({"symbol": symbol, "reason": reason})

    # --- トリガC: OPEN予測のSL/TP接近 ---
    sltp_alerts = []
    for t in open_trades:
        if t.get("status") != "OPEN":
            continue
        tid = t.get("id")
        symbol = t.get("symbol")
        price = prices.get(symbol)
        if not price or not tid:
            continue
        if sltp_notified.get(tid) == today_str:
            continue  # 同一予測は1日1回まで
        sl = t.get("stop_loss") or 0
        tp = t.get("target_price") or 0
        kind = None
        level = None
        if sl and price <= sl * (1 + proximity_pct / 100):
            kind, level = "SL", sl
        elif tp and price >= tp * (1 - proximity_pct / 100):
            kind, level = "TP", tp
        if kind:
            sltp_alerts.append({"trade_id": tid, "symbol": symbol,
                                "kind": kind, "level": level, "price": price})

    return analyze_targets, sltp_alerts


def tool_crypto_watch() -> str:
    """15分毎の監視サイクル1回分を実行する (agent_scheduler から呼ばれる)。

    scheduler側は silent=True 登録のため、定型通知は飛ばない。
    イベント発生時のみ本関数が自前で Discord へ通知する。
    """
    now = datetime.datetime.now()
    delta_15m = _get_float_env("WATCH_DELTA_15M", 2.5)
    delta_1h = _get_float_env("WATCH_DELTA_1H", 4.0)
    cooldown_h = _get_float_env("WATCH_COOLDOWN_HOURS", 6.0)
    proximity = _get_float_env("WATCH_SLTP_PROXIMITY_PCT", 1.5)

    # 1. 無料層: 全銘柄ティッカーを1コールで取得
    exchange = ccxt.binance({'enableRateLimit': True})
    try:
        tickers = exchange.fetch_tickers(SCAN_SYMBOLS)
    except Exception as e:
        return f"Watcher: ティッカー取得失敗 ({e})"

    prices = {}
    for sym, ticker in tickers.items():
        last = ticker.get("last")
        if last:
            prices[sym] = float(last)
    if not prices:
        return "Watcher: 価格データなし"

    # 2. 状態ロード (破損は load_json が default で吸収)
    state = load_json(WATCH_STATE_PATH, default={})
    if not isinstance(state, dict):
        state = {}
    snapshots = state.get("snapshots", [])
    cooldowns = state.get("cooldowns", {})
    sltp_notified = state.get("sltp_notified", {})

    trade_log = load_json(TRADE_LOG_PATH, default=[])
    open_trades = [t for t in trade_log
                   if isinstance(t, dict) and t.get("status") == "OPEN"]

    # 3. トリガ判定 (純関数)
    analyze_targets, sltp_alerts = _decide_watch_actions(
        prices, snapshots, cooldowns, open_trades, sltp_notified, now,
        delta_15m=delta_15m, delta_1h=delta_1h, proximity_pct=proximity)

    today_str = now.strftime("%Y-%m-%d")

    # 4. トリガC: SL/TP接近通知 (無料・分析なし)
    for alert in sltp_alerts:
        label = "🔻 損切りライン" if alert["kind"] == "SL" else "🎯 利確目標"
        notify_discord(
            f"⏰ **SL/TP接近**: {alert['symbol']} が{label} "
            f"${alert['level']:,.2f} に接近 (現在値 ${alert['price']:,.2f}, "
            f"予測ID: {alert['trade_id']})。今夜22:30の監査で決着見込み。")
        sltp_notified[alert["trade_id"]] = today_str
        print(f"  ⏰ SL/TP alert: {alert['symbol']} {alert['kind']}")

    # 5. トリガA/B: フル分析 (有料層・二重ガード付き)
    analyzed = []
    budget_hit = False
    for target in analyze_targets:
        symbol = target["symbol"]
        try:
            WATCHER_COUNTER.check_and_increment()
        except GeminiBudgetExceeded:
            # 初回到達時は cost_guard がDiscord通知済み。無料トリガCは影響なし
            budget_hit = True
            break
        # クールダウンは分析の成否に関わらず設定する (失敗の連続再試行で
        # 15分毎にAPIを叩き続けるのを防ぐ)
        cooldowns[symbol] = (now + datetime.timedelta(hours=cooldown_h)).isoformat()
        print(f"  ⚡ Trigger: {symbol} — {target['reason']}")
        try:
            result = tool_analyze_crypto(symbol)
        except Exception as e:
            result = f"分析失敗: {e}"
        notify_discord(
            f"⚡ **急変検知**: {symbol} — {target['reason']}\n{str(result)[:1500]}")
        analyzed.append(symbol)

    # 6. 状態更新 (期限切れクールダウン・過去日の通知記録はここで掃除)
    snapshots.append({"ts": now.isoformat(), "prices": prices})
    new_state = {
        "snapshots": snapshots[-SNAPSHOT_KEEP:],
        "cooldowns": {s: u for s, u in cooldowns.items()
                      if (_parse_dt(u) or now) > now},
        "sltp_notified": {tid: d for tid, d in sltp_notified.items()
                          if d == today_str},
    }
    save_json(WATCH_STATE_PATH, new_state)

    summary = (f"Watcher: {len(prices)}銘柄監視 / 分析発火 {len(analyzed)}件"
               f"{'（' + ', '.join(analyzed) + '）' if analyzed else ''}"
               f" / SL/TP通知 {len(sltp_alerts)}件")
    if budget_hit:
        summary += " ※日次分析上限に到達 (無料監視は継続)"
    print(f"[Watcher] {summary}")
    return summary
