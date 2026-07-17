# -*- coding: utf-8 -*-
"""
Phase 2 (feat/crypto-event-watcher) のユニットテスト:
- _reference_price: 年齢範囲フィルタ / target最近傍選択 / 破損ts耐性
- _decide_watch_actions: 15m/1hトリガ閾値 / クールダウン / SL/TP接近 / 通知重複抑止
実API・実ファイルは使わない (全て純関数のテスト)。
"""
import sys
import os
import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from tools.tool_crypto_watcher import _reference_price, _decide_watch_actions

PASS = 0
FAIL = 0

NOW = datetime.datetime(2026, 7, 17, 12, 0, 0)


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _snap(minutes_ago: float, prices: dict) -> dict:
    ts = NOW - datetime.timedelta(minutes=minutes_ago)
    return {"ts": ts.isoformat(), "prices": prices}


def test_reference_price():
    print("[Test] _reference_price")

    # 年齢15分のスナップショット → 15mウィンドウ[5,25]で採用される
    snaps = [_snap(15, {"BTC/USDT": 100.0})]
    check("15分前を採用", _reference_price(snaps, "BTC/USDT", NOW, 15, 5, 25) == 100.0)

    # 年齢120分 → 15mウィンドウ外 (スケジューラ停止明けの誤発火防止)
    snaps = [_snap(120, {"BTC/USDT": 100.0})]
    check("古すぎるスナップショットは不採用",
          _reference_price(snaps, "BTC/USDT", NOW, 15, 5, 25) is None)

    # 複数候補 → targetに最も近い年齢を選ぶ
    snaps = [_snap(24, {"BTC/USDT": 90.0}), _snap(16, {"BTC/USDT": 95.0}),
             _snap(6, {"BTC/USDT": 99.0})]
    check("target=15に最近傍(16分前)を選択",
          _reference_price(snaps, "BTC/USDT", NOW, 15, 5, 25) == 95.0)

    # ts破損レコードはスキップして続行
    snaps = [{"ts": "broken", "prices": {"BTC/USDT": 1.0}},
             _snap(15, {"BTC/USDT": 100.0})]
    check("破損tsをスキップ", _reference_price(snaps, "BTC/USDT", NOW, 15, 5, 25) == 100.0)

    # 該当銘柄がスナップショットに無い → None
    snaps = [_snap(15, {"ETH/USDT": 100.0})]
    check("銘柄欠落 → None", _reference_price(snaps, "BTC/USDT", NOW, 15, 5, 25) is None)


def test_delta_triggers():
    print("[Test] _decide_watch_actions — 15m/1hトリガ")
    base = dict(cooldowns={}, open_trades=[], sltp_notified={}, now=NOW,
                delta_15m=2.5, delta_1h=4.0)

    # 15分で+3% → 発火
    snaps = [_snap(15, {"BTC/USDT": 100.0})]
    targets, _ = _decide_watch_actions({"BTC/USDT": 103.0}, snaps, **base)
    check("15m +3.0% ≥ 2.5% → 発火",
          len(targets) == 1 and "15分変動" in targets[0]["reason"])

    # 15分で-3% (下落方向) も発火
    targets, _ = _decide_watch_actions({"BTC/USDT": 97.0}, snaps, **base)
    check("15m -3.0% も発火 (絶対値判定)", len(targets) == 1)

    # +2.4% → 閾値未満で発火しない
    targets, _ = _decide_watch_actions({"BTC/USDT": 102.4}, snaps, **base)
    check("15m +2.4% < 2.5% → 発火しない", targets == [])

    # 1hで+5% (15m変動は閾値未満) → 1hトリガで発火
    snaps = [_snap(60, {"BTC/USDT": 100.0}), _snap(15, {"BTC/USDT": 104.0})]
    targets, _ = _decide_watch_actions({"BTC/USDT": 105.0}, snaps, **base)
    check("1h +5.0% ≥ 4.0% → 発火", len(targets) == 1 and "1時間変動" in targets[0]["reason"])

    # スナップショットなし → 発火せずクラッシュもしない
    targets, _ = _decide_watch_actions({"BTC/USDT": 105.0}, [], **base)
    check("履歴なし → 発火なし", targets == [])

    # 複数銘柄: 超過した銘柄のみ発火
    snaps = [_snap(15, {"BTC/USDT": 100.0, "ETH/USDT": 100.0})]
    targets, _ = _decide_watch_actions(
        {"BTC/USDT": 103.0, "ETH/USDT": 101.0}, snaps, **base)
    check("超過銘柄のみ発火", [t["symbol"] for t in targets] == ["BTC/USDT"])


def test_cooldown():
    print("[Test] _decide_watch_actions — クールダウン")
    snaps = [_snap(15, {"BTC/USDT": 100.0})]
    prices = {"BTC/USDT": 105.0}
    base = dict(open_trades=[], sltp_notified={}, now=NOW,
                delta_15m=2.5, delta_1h=4.0)

    # クールダウン中 → 発火抑止
    cd = {"BTC/USDT": (NOW + datetime.timedelta(hours=1)).isoformat()}
    targets, _ = _decide_watch_actions(prices, snaps, cd, **base)
    check("クールダウン中 → 抑止", targets == [])

    # クールダウン期限切れ → 発火
    cd = {"BTC/USDT": (NOW - datetime.timedelta(minutes=1)).isoformat()}
    targets, _ = _decide_watch_actions(prices, snaps, cd, **base)
    check("クールダウン切れ → 発火", len(targets) == 1)

    # クールダウン値が破損 → クールダウンなし扱いで発火 (安全側は発火)
    targets, _ = _decide_watch_actions(prices, snaps, {"BTC/USDT": "broken"}, **base)
    check("破損クールダウン → 発火", len(targets) == 1)


def test_sltp_alerts():
    print("[Test] _decide_watch_actions — SL/TP接近")
    base = dict(snapshots=[], cooldowns={}, now=NOW,
                delta_15m=2.5, delta_1h=4.0, proximity_pct=1.5)

    trade = {"id": "abc123", "symbol": "BTC/USDT", "status": "OPEN",
             "stop_loss": 88.0, "target_price": 120.0}

    # SLの+1.5%以内に接近 → SLアラート
    _, alerts = _decide_watch_actions(
        {"BTC/USDT": 89.0}, open_trades=[trade], sltp_notified={}, **base)
    check("SL接近 (89.0 ≤ 88×1.015) → アラート",
          len(alerts) == 1 and alerts[0]["kind"] == "SL")

    # SL割れ済みでもアラートは出る (監査前の先行通知)
    _, alerts = _decide_watch_actions(
        {"BTC/USDT": 87.0}, open_trades=[trade], sltp_notified={}, **base)
    check("SL割れ済みもアラート", len(alerts) == 1 and alerts[0]["kind"] == "SL")

    # TPの-1.5%以内 → TPアラート
    _, alerts = _decide_watch_actions(
        {"BTC/USDT": 118.5}, open_trades=[trade], sltp_notified={}, **base)
    check("TP接近 (118.5 ≥ 120×0.985) → アラート",
          len(alerts) == 1 and alerts[0]["kind"] == "TP")

    # SL/TPどちらからも遠い中間帯 → アラートなし
    _, alerts = _decide_watch_actions(
        {"BTC/USDT": 100.0}, open_trades=[trade], sltp_notified={}, **base)
    check("中間帯 → アラートなし", alerts == [])

    # 本日通知済み → 重複抑止
    _, alerts = _decide_watch_actions(
        {"BTC/USDT": 89.0}, open_trades=[trade],
        sltp_notified={"abc123": NOW.strftime("%Y-%m-%d")}, **base)
    check("本日通知済み → 抑止", alerts == [])

    # 昨日通知 → 今日は再度アラート
    _, alerts = _decide_watch_actions(
        {"BTC/USDT": 89.0}, open_trades=[trade],
        sltp_notified={"abc123": "2026-07-16"}, **base)
    check("昨日通知分 → 本日再アラート", len(alerts) == 1)

    # OPEN以外のステータス → 対象外
    closed = dict(trade, status="LOSE")
    _, alerts = _decide_watch_actions(
        {"BTC/USDT": 89.0}, open_trades=[closed], sltp_notified={}, **base)
    check("非OPEN → 対象外", alerts == [])

    # 価格が取れていない銘柄の予測 → スキップ (クラッシュしない)
    other = dict(trade, symbol="XXX/USDT")
    _, alerts = _decide_watch_actions(
        {"BTC/USDT": 100.0}, open_trades=[other], sltp_notified={}, **base)
    check("価格なし銘柄 → スキップ", alerts == [])


if __name__ == "__main__":
    test_reference_price()
    test_delta_triggers()
    test_cooldown()
    test_sltp_alerts()
    print(f"\n{'='*40}\nResult: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
