# -*- coding: utf-8 -*-
"""
Crypto Performance Stats (Phase 4: 観測レイヤー)

trade_log.json の読み取り専用集計。シグナル・水準・ゲートのロジックには
一切触れない (Backtest Gate 非該当)。Gemini呼び出しなし = API課金ゼロ。

- tool_crypto_stats()        : /crypto-stats の実体 (累積成績 + OPEN一覧)
- tool_weekly_crypto_report(): 週次成績レポート (日曜22:50 scheduler起動)

純関数 (compute_stats / build_weekly_report / _build_stats_report) と
I/Oツールを分離する (tool_crypto_watcher._decide_watch_actions と同じ流儀)。
"""
import os
import datetime

import ccxt

from lib.lib_shared import VAULT_ROOT, CRYPTO_DIR_NAME
from tools.tool_crypto import (
    _load_trade_log, MIN_AUDIT_SAMPLES,
    RECORD_KIND_SIGNAL, RECORD_KIND_OBSERVATION,
)

# R定義はバックテスト (scripts/backtest_crypto.py R_BY_STATUS) と同一に揃える。
# scripts側は pandas/bt_data を引き込むため import せず、算式のみ一致させる:
# WIN=+2R (target = entry + 2×risk 固定設計) / LOSE=-1R / EXPIRED=0R。
# 期待値R の分母は closed (WIN+LOSE+EXPIRED) — EXPIREDも機会コストとして含める
R_BY_STATUS = {"WIN": 2.0, "LOSE": -1.0, "EXPIRED": 0.0}

WEEKLY_DIR_NAME = "Weekly_Report"


# ---------------------------------------------------------
# Pure Functions (I/Oなし・テスト対象)
# ---------------------------------------------------------

def _parse_ts(s):
    """ISO文字列/日付文字列→datetime。パース不能は None (ログ破損に耐える)。"""
    try:
        return datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _event_dt(trade: dict, status: str):
    """集計上の発生時刻: OPEN=予測作成時刻 / 決着済み=監査日(なければ作成時刻)。"""
    if status != "OPEN":
        dt = _parse_ts(trade.get("audit_date"))
        if dt is not None:
            return dt
    return _parse_ts(trade.get("timestamp"))


def split_by_kind(logs: list) -> tuple:
    """レコードを (売買コール, 観測レコード) に分ける。

    record_kind 未設定の旧レコードは売買コール扱い (後方互換)。
    成績表を分けないと、様子見(WATCH LIST)の追跡結果が実際の売買コールの
    勝率に混ざり、Kellyが参照する数字と表示が食い違う。
    """
    signals, observations = [], []
    for t in logs:
        if not isinstance(t, dict):
            continue
        if t.get("record_kind", RECORD_KIND_SIGNAL) == RECORD_KIND_OBSERVATION:
            observations.append(t)
        else:
            signals.append(t)
    return signals, observations


def compute_stats(logs: list, since: datetime.datetime = None) -> dict:
    """signal_type別 + 合計の成績バケットを集計する。

    since 指定時は「その時刻以降に発生したイベント」のみ対象
    (OPEN=作成日、決着=監査日で判定。日付不明レコードは除外) → 週次で再利用。
    Returns: {"per_type": {stype: bucket}, "total": bucket}
      bucket = {wins, losses, expired, open, sum_r, win_rate, expectancy_r}
    """
    def _bucket():
        return {"wins": 0, "losses": 0, "expired": 0, "open": 0, "sum_r": 0.0,
                "win_rate": None, "expectancy_r": None}

    per_type = {}
    total = _bucket()
    for t in logs:
        if not isinstance(t, dict):
            continue
        status = t.get("status")
        if status not in ("WIN", "LOSE", "EXPIRED", "OPEN"):
            continue
        if since is not None:
            dt = _event_dt(t, status)
            if dt is None or dt < since:
                continue
        stype = t.get("signal_type") or "legacy"
        for b in (per_type.setdefault(stype, _bucket()), total):
            if status == "WIN":
                b["wins"] += 1
            elif status == "LOSE":
                b["losses"] += 1
            elif status == "EXPIRED":
                b["expired"] += 1
            else:
                b["open"] += 1
            b["sum_r"] += R_BY_STATUS.get(status, 0.0)

    for b in [total, *per_type.values()]:
        decided = b["wins"] + b["losses"]
        closed = decided + b["expired"]
        b["win_rate"] = b["wins"] / decided if decided else None
        b["expectancy_r"] = b["sum_r"] / closed if closed else None
    return {"per_type": per_type, "total": total}


def _fmt(v, pct=False):
    if v is None:
        return "—"
    return f"{v * 100:.1f}%" if pct else f"{v:+.2f}"


def _fmt_price(p: float) -> str:
    """$1未満のアルト (DOGE等) は4桁小数、それ以外は2桁で表示する。"""
    return f"${p:,.4f}" if 0 < p < 1 else f"${p:,.2f}"


def format_type_table(stats: dict, with_kelly: bool = False) -> str:
    """compute_stats の結果をMarkdown表にする。

    with_kelly: n>=MIN_AUDIT_SAMPLES で実測勝率がKellyに採用される状況列を付ける
    (tool_audit_predictions の累積実績表と同じ規約)。
    """
    header = "| 種別 | WIN | LOSE | EXP | OPEN | 勝率 | 期待値R |"
    sep = "|---|---|---|---|---|---|---|"
    if with_kelly:
        header += " Kelly実測 |"
        sep += "---|"
    rows = [header, sep]
    items = sorted(stats["per_type"].items()) + [("**合計**", stats["total"])]
    for name, b in items:
        row = (f"| {name} | {b['wins']} | {b['losses']} | {b['expired']} | "
               f"{b['open']} | {_fmt(b['win_rate'], pct=True)} | "
               f"{_fmt(b['expectancy_r'])} |")
        if with_kelly:
            if name == "**合計**":
                row += " — |"
            else:
                n = b["wins"] + b["losses"]
                applied = ("✅ 適用中" if n >= MIN_AUDIT_SAMPLES
                           else f"⏳ あと{MIN_AUDIT_SAMPLES - n}件")
                row += f" {applied} |"
        rows.append(row)
    return "\n".join(rows)


def format_open_positions(open_trades: list, prices: dict) -> str:
    """OPEN中の予測をSL/TP距離付きで一覧化する。価格が無い銘柄は距離を省略。"""
    lines = []
    for t in open_trades:
        sym = t.get("symbol", "?")
        entry = t.get("entry_price") or 0
        line = (f"- {sym} {t.get('signal', '?')} @{_fmt_price(entry)} "
                f"({(t.get('timestamp') or '')[:10]}, {t.get('signal_type', '?')})")
        price = prices.get(sym)
        tp, sl = t.get("target_price"), t.get("stop_loss")
        if price and tp and sl:
            dist_tp = (tp - price) / price * 100
            dist_sl = (price - sl) / price * 100
            line += (f"\n  → 現在 {_fmt_price(price)} | TPまで {dist_tp:+.1f}% / "
                     f"SLまで {dist_sl:.1f}%")
        lines.append(line)
    return "\n".join(lines)


def _recent_closed(logs: list, limit: int = 5) -> list:
    """決着済みレコードを新しい順に最大limit件返す。"""
    closed = [t for t in logs if isinstance(t, dict)
              and t.get("status") in ("WIN", "LOSE", "EXPIRED")]
    closed.sort(key=lambda t: (t.get("audit_date") or "", t.get("timestamp") or ""),
                reverse=True)
    return closed[:limit]


def _build_stats_report(logs: list, prices: dict,
                        now: datetime.datetime) -> str:
    """/crypto-stats のレポート本文を組み立てる (純関数)。"""
    logs, observations = split_by_kind(logs)
    stats = compute_stats(logs)
    t = stats["total"]
    open_trades = [x for x in logs
                   if isinstance(x, dict) and x.get("status") == "OPEN"]
    decided = t["wins"] + t["losses"]

    lines = [
        f"📊 **Crypto予測成績ダッシュボード** ({now.strftime('%Y-%m-%d %H:%M')})",
        f"記録 {t['wins'] + t['losses'] + t['expired'] + t['open']}件: "
        f"判定済み {decided} (WIN {t['wins']} / LOSE {t['losses']}) | "
        f"EXPIRED {t['expired']} | OPEN {t['open']} | 累積R {t['sum_r']:+.1f}",
        "",
        "**累積成績 (signal_type別)**",
        format_type_table(stats, with_kelly=True),
    ]

    if open_trades:
        lines += ["", f"**OPEN中の予測 ({len(open_trades)}件)**",
                  format_open_positions(open_trades, prices)]

    if observations:
        obs = compute_stats(observations)["total"]
        obs_decided = obs["wins"] + obs["losses"]
        lines += [
            "",
            "**👀 観測レコード (WATCH LIST を仮にエントリーしていたら)**",
            f"記録 {obs_decided + obs['expired'] + obs['open']}件: "
            f"判定済み {obs_decided} (WIN {obs['wins']} / LOSE {obs['losses']}) | "
            f"EXPIRED {obs['expired']} | OPEN {obs['open']} | "
            f"勝率 {_fmt(obs['win_rate'], pct=True)} | "
            f"期待値R {_fmt(obs['expectancy_r'])}",
            "→ 見送った判断が正しかったかの対照群。Kellyの実測勝率には使わない。",
        ]

    recent = _recent_closed(logs)
    if recent:
        lines += ["", "**直近の決着 (最大5件)**"]
        icons = {"WIN": "✅", "LOSE": "❌", "EXPIRED": "⌛"}
        for r in recent:
            status = r.get("status")
            lines.append(
                f"- {icons.get(status, '❓')} {status} {r.get('symbol', '?')} "
                f"{r.get('signal_type', 'legacy')} "
                f"(監査 {r.get('audit_date', '?')}, R {R_BY_STATUS.get(status, 0):+.0f})")
    return "\n".join(lines)


def build_weekly_report(logs: list, now: datetime.datetime) -> tuple:
    """週次レポート本文とサマリ行を組み立てる (純関数)。

    Returns: (report_md, summary_line)
    """
    since = now - datetime.timedelta(days=7)
    logs, observations = split_by_kind(logs)
    week = compute_stats(logs, since=since)
    cumulative = compute_stats(logs)
    wt, ct = week["total"], cumulative["total"]
    week_decided = wt["wins"] + wt["losses"] + wt["expired"]

    # 今週決着した予測の明細 (監査日ベース)
    closed_week = [t for t in logs if isinstance(t, dict)
                   and t.get("status") in ("WIN", "LOSE", "EXPIRED")
                   and (_event_dt(t, t["status"]) or datetime.datetime.min) >= since]
    closed_week.sort(key=lambda t: t.get("audit_date") or "")
    # 今週作成された予測 (現ステータス不問)
    new_week = [t for t in logs if isinstance(t, dict)
                and t.get("status") in ("WIN", "LOSE", "EXPIRED", "OPEN")
                and (_parse_ts(t.get("timestamp")) or datetime.datetime.min) >= since]

    date_str = now.strftime('%Y-%m-%d')
    report = (
        f"# 📅 Weekly Crypto Report ({date_str})\n\n"
        f"> [!SUMMARY] 今週の成績 ({since.strftime('%m-%d')}〜{now.strftime('%m-%d')})\n"
        f"> 決着 {week_decided}件 (WIN {wt['wins']} / LOSE {wt['losses']} / "
        f"EXPIRED {wt['expired']}) | 週間R **{wt['sum_r']:+.1f}** | "
        f"新規予測 {len(new_week)}件 | 現在OPEN {ct['open']}件\n\n"
        f"## 今週の成績 (signal_type別)\n"
        f"{format_type_table(week)}\n\n"
    )

    if closed_week:
        report += "## 今週決着した予測\n"
        icons = {"WIN": "✅", "LOSE": "❌", "EXPIRED": "⌛"}
        for t in closed_week:
            status = t["status"]
            report += (
                f"- {icons.get(status, '❓')} **{status}** {t.get('symbol', '?')} "
                f"{t.get('signal', '?')} ({t.get('signal_type', 'legacy')}) — "
                f"エントリー {(t.get('timestamp') or '')[:10]} → "
                f"監査 {t.get('audit_date', '?')} (R {R_BY_STATUS.get(status, 0):+.0f})\n"
            )
        report += "\n"

    report += (
        f"## 累積成績 (全期間)\n"
        f"{format_type_table(cumulative, with_kelly=True)}\n\n"
        f"累積R: **{ct['sum_r']:+.1f}** / 判定済み {ct['wins'] + ct['losses']}件 / "
        f"EXPIRED {ct['expired']}件\n"
    )

    if observations:
        obs = compute_stats(observations)["total"]
        obs_week = compute_stats(observations, since=since)["total"]
        report += (
            f"\n## 👀 観測レコード (WATCH LIST を仮にエントリーしていたら)\n"
            f"今週の決着 {obs_week['wins'] + obs_week['losses'] + obs_week['expired']}件 | "
            f"累積 判定済み {obs['wins'] + obs['losses']}件 "
            f"(勝率 {_fmt(obs['win_rate'], pct=True)} / "
            f"期待値R {_fmt(obs['expectancy_r'])})\n\n"
            f"{format_type_table(compute_stats(observations))}\n\n"
            f"見送った判断が正しかったかを測る対照群。Kellyの実測勝率には使わない。\n"
        )

    summary = (f"Weekly Crypto Report: 今週の決着 {week_decided}件 "
               f"(WIN {wt['wins']} / LOSE {wt['losses']} / EXP {wt['expired']}, "
               f"週間R {wt['sum_r']:+.1f}) | 新規予測 {len(new_week)}件 | "
               f"累積期待値R {_fmt(ct['expectancy_r'])}")
    return report, summary


# ---------------------------------------------------------
# I/O Tools
# ---------------------------------------------------------

def _fetch_prices(symbols: list) -> dict:
    """OPEN銘柄の現在値を1コールで取得する (無料)。失敗は空dict (フェイルソフト)。

    trade_log由来のシンボルは上場廃止済みの可能性がある (実例: MATIC→POL移行)。
    fetch_tickers(symbols) は未知シンボル1件で BadSymbol 全滅するため、
    失敗時は全銘柄取得(同じく1コール)に切り替えて正常銘柄だけ拾う。
    """
    if not symbols:
        return {}
    try:
        exchange = ccxt.binance({'enableRateLimit': True})
        try:
            tickers = exchange.fetch_tickers(symbols)
        except Exception:
            tickers = exchange.fetch_tickers()
        return {s: float(tickers[s]["last"]) for s in symbols
                if tickers.get(s, {}).get("last")}
    except Exception as e:
        print(f"[CryptoStats] ⚠️ ticker fetch failed: {e}")
        return {}


def tool_crypto_stats() -> str:
    """予測成績ダッシュボード (/crypto-stats の実体。読み取り専用・課金なし)。"""
    print("[Tool] 📊 Crypto Stats...")
    logs = _load_trade_log()
    if not logs:
        return ("📊 まだ予測記録がありません。BUY/STRONG BUYシグナル発生時に"
                "自動記録され、毎晩22:30の監査で勝敗判定されます。")

    open_symbols = sorted({t.get("symbol") for t in logs
                           if isinstance(t, dict) and t.get("status") == "OPEN"
                           and t.get("symbol")})
    prices = _fetch_prices(open_symbols)
    return _build_stats_report(logs, prices, datetime.datetime.now())


def tool_weekly_crypto_report() -> str:
    """週次成績レポート生成 → Obsidian保存 (日曜22:50 scheduler起動)。

    scheduler は非silent登録のため、この戻り値がそのままDiscord定型通知に載る
    (22:30監査と同じ通知経路)。
    """
    print("[Tool] 📅 Weekly Crypto Report...")
    now = datetime.datetime.now()
    logs = _load_trade_log()
    if not logs:
        # データ皆無の週にファイルを量産しない (稼働初期は未記録が正常)
        return ("Weekly Crypto Report: 予測記録がまだありません"
                "(BUYシグナル発生時に自動記録されます)。レポート保存はスキップ。")

    report, summary = build_weekly_report(logs, now)

    filename = f"Weekly_Report_{now.strftime('%Y-%m-%d')}.md"
    save_dir = os.path.join(VAULT_ROOT, CRYPTO_DIR_NAME, WEEKLY_DIR_NAME)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✅ Saved: {save_path}")

    return (f"{summary}。レポート: "
            f"[[{CRYPTO_DIR_NAME}/{WEEKLY_DIR_NAME}/{filename}]]")
