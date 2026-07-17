# -*- coding: utf-8 -*-
"""
Phase 4 (観測レイヤー) のユニットテスト:
- compute_stats: 空リスト / legacy混在 / sinceフィルタ / EXPIRED除外 / 期待値R算式
- build_weekly_report: 週次窓の切り出し / サマリ行
- _build_watchlist_view: 稼働ヘルス判定 / クールダウン表示 / 閾値マーク
実API・実ファイルは使わない (全て純関数のテスト)。
"""
import sys
import os
import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from tools.tool_crypto_stats import (
    compute_stats, build_weekly_report, _build_stats_report,
    format_type_table, format_open_positions, _fmt_price,
)
from tools.tool_crypto_watcher import _build_watchlist_view

PASS = 0
FAIL = 0

NOW = datetime.datetime(2026, 7, 18, 12, 0, 0)


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def _trade(status="OPEN", stype="smc_sweep", symbol="BTC/USDT",
           days_ago=1, audit_days_ago=None, **kw):
    t = {
        "id": "t1", "symbol": symbol, "signal": "BUY", "signal_type": stype,
        "status": status, "entry_price": 100.0, "stop_loss": 95.0,
        "target_price": 110.0, "kelly_pct": 5.0,
        "timestamp": (NOW - datetime.timedelta(days=days_ago)).isoformat(),
    }
    if audit_days_ago is not None:
        t["audit_date"] = (NOW - datetime.timedelta(days=audit_days_ago)).strftime("%Y-%m-%d")
    t.update(kw)
    return t


def test_compute_stats():
    print("[Test] compute_stats")

    # 空リスト → 空のper_type、totalは全ゼロ・率はNone
    s = compute_stats([])
    check("空リスト: per_type空", s["per_type"] == {})
    check("空リスト: 勝率None", s["total"]["win_rate"] is None)
    check("空リスト: 期待値None", s["total"]["expectancy_r"] is None)

    # 基本集計: WIN=+2R, LOSE=-1R, EXPIRED=0R(分母には入る)
    logs = [_trade("WIN", audit_days_ago=1), _trade("WIN", audit_days_ago=1),
            _trade("LOSE", audit_days_ago=2), _trade("EXPIRED", audit_days_ago=3),
            _trade("OPEN")]
    s = compute_stats(logs)
    t = s["total"]
    check("WIN/LOSE/EXP/OPENカウント",
          (t["wins"], t["losses"], t["expired"], t["open"]) == (2, 1, 1, 1))
    check("勝率 = 2/3 (EXPIRED・OPEN除外)", abs(t["win_rate"] - 2 / 3) < 1e-9)
    check("sum_r = 2+2-1+0 = 3", abs(t["sum_r"] - 3.0) < 1e-9)
    check("期待値R = 3/4 (分母=closed、OPEN除外)",
          abs(t["expectancy_r"] - 0.75) < 1e-9)

    # signal_type未設定の旧レコード → "legacy" バケット
    s = compute_stats([_trade("WIN", stype=None, audit_days_ago=1)])
    check("signal_typeなし → legacy", "legacy" in s["per_type"])

    # 不正レコード (非dict / 未知status) はスキップ
    s = compute_stats(["broken", {"status": "???"}, _trade("WIN", audit_days_ago=1)])
    check("破損レコードをスキップ", s["total"]["wins"] == 1)

    # sinceフィルタ: 決着は監査日、OPENは作成日で判定
    since = NOW - datetime.timedelta(days=7)
    logs = [
        _trade("WIN", days_ago=20, audit_days_ago=2),    # 古い予測だが今週決着 → 含む
        _trade("LOSE", days_ago=20, audit_days_ago=10),  # 先週決着 → 除外
        _trade("OPEN", days_ago=3),                      # 今週作成 → 含む
        _trade("OPEN", days_ago=10),                     # 先週作成 → 除外
    ]
    s = compute_stats(logs, since=since)
    t = s["total"]
    check("since: 今週決着のWINを採用", t["wins"] == 1)
    check("since: 先週決着のLOSEを除外", t["losses"] == 0)
    check("since: OPENは作成日で判定", t["open"] == 1)

    # audit_date欠落の決着済みレコード → 作成日(timestamp)にフォールバック
    s = compute_stats([_trade("WIN", days_ago=2)], since=since)
    check("since: audit_dateなし → timestampで判定", s["total"]["wins"] == 1)
    s = compute_stats([_trade("WIN", days_ago=10)], since=since)
    check("since: audit_dateなし+窓外 → 除外", s["total"]["wins"] == 0)


def test_format_helpers():
    print("[Test] format helpers")

    check("$1未満は4桁小数", _fmt_price(0.2345) == "$0.2345")
    check("$1以上は2桁+カンマ", _fmt_price(118000.5) == "$118,000.50")

    stats = compute_stats([_trade("WIN", audit_days_ago=1)])
    table = format_type_table(stats, with_kelly=True)
    check("Kelly列: n<20は残数表示", "あと19件" in table)
    check("合計行あり", "**合計**" in table)

    # 現在値ありのOPEN → TP/SL距離を表示、価格なし銘柄は距離省略
    open_trades = [_trade("OPEN"), _trade("OPEN", symbol="XXX/USDT")]
    text = format_open_positions(open_trades, {"BTC/USDT": 100.0})
    check("TP距離 +10.0%", "TPまで +10.0%" in text)
    check("SL距離 5.0%", "SLまで 5.0%" in text)
    check("価格なし銘柄は距離なし", "XXX/USDT" in text and text.count("現在") == 1)


def test_stats_report():
    print("[Test] _build_stats_report")

    logs = [_trade("WIN", audit_days_ago=1), _trade("OPEN")]
    report = _build_stats_report(logs, {"BTC/USDT": 100.0}, NOW)
    check("ヘッダあり", "Crypto予測成績ダッシュボード" in report)
    check("OPEN一覧あり", "OPEN中の予測 (1件)" in report)
    check("直近の決着あり", "直近の決着" in report)


def test_weekly_report():
    print("[Test] build_weekly_report")

    logs = [
        _trade("WIN", days_ago=20, audit_days_ago=2),   # 今週決着
        _trade("LOSE", days_ago=20, audit_days_ago=10),  # 先週決着 (累積のみ)
        _trade("OPEN", days_ago=3),                      # 今週の新規予測
    ]
    report, summary = build_weekly_report(logs, NOW)
    check("今週の決着1件", "決着 1件" in report)
    check("新規予測1件", "新規予測 1件" in report)
    check("今週決着の明細にWIN", "今週決着した予測" in report and "**WIN**" in report)
    check("累積テーブルに先週分も含む", "LOSE" in report)
    check("サマリ行に週間R", "週間R +2.0" in summary)

    # 全決着が窓外・新規なし週でもクラッシュしない
    report, summary = build_weekly_report(
        [_trade("LOSE", days_ago=30, audit_days_ago=20)], NOW)
    check("静かな週: 決着0件", "決着 0件" in report)


def test_watchlist_view():
    print("[Test] _build_watchlist_view")

    def _snap(minutes_ago, prices):
        ts = NOW - datetime.timedelta(minutes=minutes_ago)
        return {"ts": ts.isoformat(), "prices": prices}

    base = dict(now=NOW, delta_15m=2.5, delta_1h=4.0, cooldown_h=6.0,
                analyses_today=2, analyses_limit=8)

    # 新鮮なスナップショット → 稼働中表示 + 15分変動を算出
    snaps = [_snap(15, {"BTC/USDT": 100.0}), _snap(5, {"BTC/USDT": 102.0})]
    view = _build_watchlist_view({"BTC/USDT": 103.0}, snaps, {}, [], **base)
    check("稼働中ヘルス表示", "✅ ウォッチャー稼働中" in view)
    check("15分変動 +3.0% と⚡マーク", "15m +3.0%⚡" in view)
    check("1h参照なし → —", "1h —" in view)
    check("発火カウンタ表示", "2/8回" in view)

    # 古いスナップショットのみ → 停止警告
    view = _build_watchlist_view({"BTC/USDT": 100.0}, [_snap(45, {})], {}, [], **base)
    check("45分経過 → 停止警告", "停止中の可能性" in view)

    # スナップショットなし → 未稼働警告
    view = _build_watchlist_view({"BTC/USDT": 100.0}, [], {}, [], **base)
    check("スナップショットなし → 未稼働警告", "未稼働の可能性" in view)

    # 未来ts (時計ずれ) → 負の経過分を0にクランプ
    view = _build_watchlist_view({"BTC/USDT": 100.0}, [_snap(-3, {})], {}, [], **base)
    check("未来ts → 0分前表示 (負値なし)", "0分前" in view and "-3分前" not in view)

    # クールダウン中の銘柄表示 (期限切れは非表示)
    cooldowns = {"BTC/USDT": (NOW + datetime.timedelta(hours=3)).isoformat(),
                 "ETH/USDT": (NOW - datetime.timedelta(hours=1)).isoformat()}
    view = _build_watchlist_view(
        {"BTC/USDT": 100.0, "ETH/USDT": 200.0}, [], cooldowns, [], **base)
    check("クールダウン中表示 (残3.0h)", "あと3.0h" in view)
    check("期限切れクールダウンは非表示", view.count("クールダウン中") == 1)

    # OPEN予測セクション
    trade = {"symbol": "BTC/USDT", "signal": "BUY", "signal_type": "smc_sweep",
             "status": "OPEN", "entry_price": 100.0, "stop_loss": 95.0,
             "target_price": 110.0, "timestamp": NOW.isoformat()}
    view = _build_watchlist_view({"BTC/USDT": 100.0}, [], {}, [trade], **base)
    check("OPEN予測セクションあり", "OPEN中の予測 (1件)" in view)


if __name__ == "__main__":
    test_compute_stats()
    test_format_helpers()
    test_stats_report()
    test_weekly_report()
    test_watchlist_view()
    print(f"\n{'='*40}\nResult: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
