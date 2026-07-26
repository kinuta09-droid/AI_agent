# -*- coding: utf-8 -*-
"""
Antigravity Crypto Analysis Module
agent_hello.py から抽出された暗号通貨分析ツール。
クオンツ分析、マーケットスキャン、予測監査を提供する。
"""

import os
import datetime
import time
import uuid
import ccxt
import pandas as pd
import pandas_ta as ta_lib
import requests as req

from lib.lib_shared import (
    client, tavily_client, MODEL_NAME, retry,
    VAULT_ROOT, CRYPTO_DIR_NAME, TRADE_LOG_PATH,
    fetch_fear_greed_index, load_json, save_json,
)
from lib.file_lock import FileLock
from lib.cost_guard import SWEEP_COUNTER, GeminiBudgetExceeded
from lib.bot_errors import is_failure_result
# 純関数群は lib/quant_core.py に分離 (バックテスターと共有)。
# ここで再輸入することで既存の呼び出し・テスト (tc._decide_signal 等) は不変
from lib.quant_core import (
    _calculate_kelly, _detect_regime, _detect_liquidity_sweep,
    _check_divergence, _decide_signal, _compute_levels, get_float_env,
    _daily_trend, _apply_daily_gate, _compute_indicators, classify_news_risk,
    _resolve_trade,
)


# ---------------------------------------------------------
# Next-Gen Quant Helpers
# ---------------------------------------------------------

def _analyze_market_drivers(symbol: str, change_pct_24h: float) -> tuple:
    """
    価格変動の「理由」をWeb検索し、ネガティブ要因の深刻度を判定する。
    Returns: (news_summary, risk_level)
    risk_level: "SYSTEMIC_RISK" | "NORMAL_CORRECTION" | "NEUTRAL"
    """
    if abs(change_pct_24h) < 3.0:
        return "直近24hの変動が小さいため、ニュース検索をスキップしました。", "NEUTRAL"

    coin_name = symbol.split('/')[0]
    direction = "drop" if change_pct_24h < 0 else "rise"
    query = f"Why is {coin_name} price {direction} today crypto news"

    print(f"  🕵️ Searching reason for {symbol} ({change_pct_24h:+.1f}%)...")

    try:
        response = tavily_client.search(query=query, topic="news", max_results=3)
    except Exception as e:
        print(f"  ⚠️ Market driver search failed: {e}")
        return f"ニュース検索に失敗しました: {e}", "NEUTRAL"

    # 判定は lib/quant_core.classify_news_risk に委譲 (純関数・テスト可能)。
    # ここは Tavily 取得と整形だけを担当する
    summary_lines, risk_level = classify_news_risk(
        response.get("results", []), coin_name)

    if not summary_lines:
        return "関連ニュースが見つかりませんでした。", "NEUTRAL"

    return "\n".join(summary_lines), risk_level


# ---------------------------------------------------------
# Trade Log (Prediction Audit System)
# ---------------------------------------------------------

def _load_trade_log() -> list:
    """trade_log.json を読み込む。存在しなければ空リストを返す。"""
    return load_json(TRADE_LOG_PATH, default=[])


def _save_trade_log(logs: list):
    """trade_log.json を保存する。"""
    save_json(TRADE_LOG_PATH, logs)


def _apply_audit_updates(logs: list, updates: dict) -> list:
    """監査結果(id→更新フィールド)をログへ差分適用する(純関数・インプレース)。

    全体上書きではなく id 一致レコードのみ更新することで、監査中
    (ネットワーク往復で分単位)に bot/watcher が追記した新規予測を潰さない。
    updates に対応レコードが無い id は無視される(監査後に手動削除された等)。
    """
    for t in logs:
        u = updates.get(t.get("id"))
        if u:
            t.update(u)
    return logs


def _update_trade_log(mutator):
    """trade_log.json をプロセス間ロック下で load→mutate→save する。

    bot(/crypto)・scheduler(22:30監査)・watcher(15分毎) の3プロセスが
    同一ファイルを read-modify-write するため、系列全体をロックしないと
    追記と更新が互いを潰す (save_json の原子性は破損防止であって
    lost-update 防止ではない)。
    mutator は logs(list) をインプレース変更する callable。戻り値を透過する。
    """
    with FileLock(TRADE_LOG_PATH):
        logs = load_json(TRADE_LOG_PATH, default=[])
        result = mutator(logs)
        save_json(TRADE_LOG_PATH, logs)
        return result


# 実測勝率をKellyに採用する最低サンプル数。これ未満は既定confidenceを使う
MIN_AUDIT_SAMPLES = 20

# レコード種別。
#   signal      = 実際に BUY/STRONG BUY を出したコール。Kellyの実測勝率ソース
#   observation = WATCH LIST (divergence 未確認の rsi_oversold / panic_reversal) を
#                 「仮にエントリーしていたら」で追跡する対照群。Kellyには使わない
# 種別を分けずに全部混ぜると、様子見コールの成績が売買コールの勝率を汚染する。
# フィールドが無い旧レコードは signal 扱い (後方互換)。
RECORD_KIND_SIGNAL = "signal"
RECORD_KIND_OBSERVATION = "observation"
# 実測勝率の上限クランプ（少数サンプルの偶然の連勝でポジションが暴走しない保険）
EMPIRICAL_WIN_RATE_CAP = 0.75


def _get_signal_stats(logs: list = None) -> dict:
    """
    判定済み(WIN/LOSE)レコードをsignal_type別に集計する。
    EXPIREDは観測窓の限界によるクローズであり勝敗ではないため除外。
    record_kind=observation (WATCH LISTの追跡) も除外 — Kellyに渡る勝率は
    実際に売買コールを出した signal レコードだけで構成する。
    signal_type未設定の旧レコードは "legacy" として分離集計。
    Returns: {signal_type: {"wins": int, "losses": int, "win_rate": float}}
    """
    if logs is None:
        logs = _load_trade_log()
    stats = {}
    for t in logs:
        if t.get("status") not in ("WIN", "LOSE"):
            continue
        if t.get("record_kind", RECORD_KIND_SIGNAL) != RECORD_KIND_SIGNAL:
            continue  # observation (様子見の追跡) はKellyの勝率に混ぜない
        stype = t.get("signal_type") or "legacy"
        s = stats.setdefault(stype, {"wins": 0, "losses": 0, "win_rate": 0.0})
        if t["status"] == "WIN":
            s["wins"] += 1
        else:
            s["losses"] += 1
    for s in stats.values():
        total = s["wins"] + s["losses"]
        s["win_rate"] = s["wins"] / total if total else 0.0
    return stats


def _resolve_win_rate(signal_type: str, default_confidence: float,
                      logs: list = None) -> tuple:
    """
    Kellyに渡す勝率を決定する。
    判定済みサンプルが MIN_AUDIT_SAMPLES 以上ある種別は実測勝率
    （EMPIRICAL_WIN_RATE_CAP でクランプ）、それ未満は既定confidence。
    logs 省略時は trade_log を読み直す。銘柄ループから呼ぶときは読み込み済みの
    ログを渡すこと (20銘柄×毎回ファイル読み = N+1 になる)。
    Returns: (win_rate, basis, sample_count)  basis: "実測" | "既定値"
    """
    if not signal_type:
        return default_confidence, "既定値", 0
    stats = _get_signal_stats(logs).get(signal_type)
    if not stats:
        return default_confidence, "既定値", 0
    n = stats["wins"] + stats["losses"]
    if n < MIN_AUDIT_SAMPLES:
        return default_confidence, "既定値", n
    return min(stats["win_rate"], EMPIRICAL_WIN_RATE_CAP), "実測", n


def _has_open_record(logs: list, symbol: str, record_kind: str) -> bool:
    """同一銘柄・同一種別のOPENレコードが既にあるか (純関数)。"""
    for t in logs:
        if not isinstance(t, dict):
            continue
        if (t.get("status") == "OPEN" and t.get("symbol") == symbol
                and t.get("record_kind", RECORD_KIND_SIGNAL) == record_kind):
            return True
    return False


def _save_prediction(symbol: str, signal: str, entry_price: float,
                     stop_loss: float, kelly_pct: float, reasons: list,
                     signal_type: str = "unknown", extra: dict = None,
                     record_kind: str = RECORD_KIND_SIGNAL) -> str:
    """予測/観測をログに記録する。既に同種のOPENがあればスキップ。

    record_kind: RECORD_KIND_SIGNAL (売買コール) / RECORD_KIND_OBSERVATION
                 (WATCH LIST の対照群追跡)。
    extra: 追加の記録フィールド (ATR水準のトレーサビリティ等)。
    スキーマは追加のみ — 監査/watcher/Kellyは既存フィールドしか読まないため
    旧レコードと混在しても安全。
    Returns: 記録した場合は record id、重複でスキップした場合は None。

    「同一銘柄・同一種別のOPENは1件まで」はバックテスター
    (scripts/backtest_crypto.run_symbol の1銘柄1建玉制限) と同じ制約。
    ライブとバックテストで母集団の定義を揃えるためで、同時に評価密度を
    上げても記録が膨張しないキャップにもなる。

    重複チェックは **必ずロック内で** 行う (check→append を別々にすると
    bot/scheduler/watcher の3プロセスが同時に「無い」と判断して全員が
    書き込む。lib/pid_guard で踏んだのと同じ非原子ガードの罠)。
    """
    risk = entry_price - stop_loss
    target_price = entry_price + (risk * 2) if risk > 0 else entry_price * 1.10

    record = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.datetime.now().isoformat(),
        "symbol": symbol,
        "signal": signal,
        "signal_type": signal_type or "unknown",
        "record_kind": record_kind,
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "target_price": round(target_price, 2),
        "kelly_pct": round(kelly_pct, 1),
        "reason": "; ".join(reasons) if reasons else "N/A",
        "status": "OPEN",
        "audit_note": ""
    }
    if extra:
        record.update(extra)

    def _append_if_new(logs):
        if _has_open_record(logs, symbol, record_kind):
            return False
        logs.append(record)
        return True

    if not _update_trade_log(_append_if_new):
        print(f"  ⏭️ Skipped ({record_kind}): {symbol} は既にOPENあり")
        return None

    print(f"  📝 Logged [{record_kind}]: {record['id']} "
          f"({symbol} {signal} @ ${entry_price:,.2f})")
    return record["id"]


def _resolve_levels(price: float, atr_value: float) -> dict:
    """DCA/SL 水準を .env 設定に従って算出する (フル分析とスイープで共用)。

    Phase3: ATR倍数ベースの動的水準 (旧固定-5/-10/-15%はEXPIRED濫発のため廃止。
    バックテスト730日×3銘柄: EXPIRED 45→10件, 平均保有335h→138h)。
    CRYPTO_ATR_LEVELS=0 で旧固定%へ即時復帰可能 (再起動不要のキルスイッチ)。

    Returns: {dca_entry1..3, dca_stop, levels(生の_compute_levels結果 or None), mode}
    """
    if get_float_env("CRYPTO_ATR_LEVELS", 1.0) != 0.0:
        levels = _compute_levels(
            price, atr_value,
            sl_mult=get_float_env("CRYPTO_ATR_SL_MULT", 2.5),
            dca_mults=(get_float_env("CRYPTO_ATR_DCA1_MULT", 1.5),
                       get_float_env("CRYPTO_ATR_DCA2_MULT", 3.0),
                       get_float_env("CRYPTO_ATR_DCA3_MULT", 4.5)),
            atr_floor_pct=get_float_env("CRYPTO_ATR_FLOOR_PCT", 1.0),
            atr_cap_pct=get_float_env("CRYPTO_ATR_CAP_PCT", 4.0))
        return {
            "dca_entry1": levels["dca_entry1"],
            "dca_entry2": levels["dca_entry2"],
            "dca_entry3": levels["dca_entry3"],
            "dca_stop": levels["dca_stop"],
            "levels": levels,
            "mode": "atr",
        }
    return {
        "dca_entry1": price * 0.95,
        "dca_entry2": price * 0.90,
        "dca_entry3": price * 0.85,
        "dca_stop": price * 0.88,
        "levels": None,
        "mode": "fixed",
    }


# ---------------------------------------------------------
# Main Analysis Tool
# ---------------------------------------------------------

def tool_analyze_crypto(symbol: str = "BTC/USDT") -> str:
    """
    次世代クオンツ分析: レジーム検知 + SMC + ケリー基準を統合。
    結果を03_Cryptoにレポート保存し、強いシグナルがあればn8nへ通知する。
    """
    symbol = symbol.upper()
    if "/" not in symbol:
        symbol += "/USDT"
    
    print(f"[Tool] 🪙 Analyzing Crypto: {symbol}")

    exchange = ccxt.binance({'enableRateLimit': True})

    # 1. 価格データの取得 (4時間足, 300本)
    # 300本必須: 旧100本ではEMA200がNaN→現在価格フォールバック→trendが常時
    # "DOWN"となり trend_pullback ブランチが一度も発火できないバグがあった
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '4h', limit=300)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    except Exception as e:
        return f"Error fetching {symbol} data: {e}"

    # 2-4. テクニカル指標の一括計算 (バックテスターと同一の共有純関数。
    # Backtest Gate: ライブ側だけの指標フォークを作らないこと)
    ind = _compute_indicators(df)
    current_price = ind["price"]
    current_rsi = ind["rsi"]
    ema50 = ind["ema50"]
    ema200 = ind["ema200"]
    regime = ind["regime"]
    adx = ind["adx"]
    atr_pct = ind["atr_pct"]
    atr_value = ind["atr_value"]
    is_sweep = ind["is_sweep"]
    sweep_level = ind["sweep_level"]
    has_divergence = ind["has_divergence"]
    is_falling_knife = ind["is_falling_knife"]
    trend = ind["trend"]
    change_24h = ind["change_24h"]

    print(f"  📈 Regime: {regime} (ADX={adx:.1f}, ATR%={atr_pct:.2f})")
    if is_sweep:
        print(f"  🎯 Liquidity Sweep detected at {sweep_level:.2f}")
    if has_divergence:
        print(f"  📊 RSI Bullish Divergence detected")
    if is_falling_knife:
        print(f"  🔪 Falling Knife warning (ADX={adx:.1f}, DM-={ind['dmn']:.1f} > DM+={ind['dmp']:.1f})")

    # 3. Fear & Greed Index の取得
    fng_value, fng_class = fetch_fear_greed_index()

    # 4b. マーケットドライバー分析 ("Why")
    news_summary, news_risk = _analyze_market_drivers(symbol, change_24h)
    print(f"  🕵️ News Risk: {news_risk}")

    # 5. Strategy 2.0 統合判定ロジック（→ _decide_signal に抽出済み）
    signal, confidence, signal_type, reasons = _decide_signal(
        news_risk, is_falling_knife, has_divergence, is_sweep, sweep_level,
        fng_value, current_rsi, regime, trend, adx)

    # 5b. 日足ハードゲート (Phase3): 日足EMA200下抜け中は BUY/STRONG BUY を
    # WATCH LIST に降格 (n8n通知・trade_log記録とも抑止される)。
    # バックテスト(730日×3銘柄)では逆張り主体の本戦略と相性が悪く期待値を
    # 悪化させた (nogate -0.01R vs gate -0.15R) ため既定OFF。
    # トレンドフォロー寄りに調整する場合は .env CRYPTO_DAILY_GATE=1 で有効化
    daily_gate_on = get_float_env("CRYPTO_DAILY_GATE", 0.0) != 0.0
    daily_trend_str = None
    if daily_gate_on:
        try:
            ohlcv_1d = exchange.fetch_ohlcv(symbol, '1d', limit=250)
            df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high',
                                                    'low', 'close', 'volume'])
            daily_trend_str = _daily_trend(df_1d)
        except Exception as e:
            print(f"  ⚠️ Daily trend fetch failed: {e}")
            daily_trend_str = "UNKNOWN"  # フェイルオープン (ゲート素通し)
        signal, gate_note = _apply_daily_gate(signal, daily_trend_str)
        if gate_note:
            reasons.append(gate_note)
            print(f"  ⛔ Daily gate: {signal} に降格 (日足 {daily_trend_str})")

    # 補足情報
    if current_price > ema200:
        reasons.append("価格がEMA200の上（上昇トレンド）")
    else:
        reasons.append("価格がEMA200の下（下降トレンド）")
    if ema50 > ema200:
        reasons.append("EMA50 > EMA200（ゴールデンクロス圏内）")
    else:
        reasons.append("EMA50 < EMA200（デッドクロス圏内）")
    if news_risk == "NORMAL_CORRECTION":
        reasons.append("ニュース分析: 一時的な調整局面（構造的リスクなし）")

    # 6. ケリー基準でポジションサイズ算出
    # 監査済みサンプルが十分な種別は実測勝率、それ以外は既定confidenceを使う
    win_rate_used, win_rate_basis, sample_n = _resolve_win_rate(signal_type, confidence)
    if win_rate_basis == "実測":
        win_rate_label = f"{win_rate_used*100:.0f}% (実測 n={sample_n})"
    else:
        win_rate_label = f"{win_rate_used*100:.0f}% (既定値)"
    kelly_pct = _calculate_kelly(win_rate_used)
    if regime == "VOLATILE":
        kelly_pct *= 0.5
        reasons.append(f"高ボラティリティ環境のためポジション半減 (ATR%: {atr_pct:.2f}%)")

    # 6b. DCA計画（分割エントリー水準）— 算出は _resolve_levels に集約
    lv = _resolve_levels(current_price, atr_value)
    dca_entry1 = lv["dca_entry1"]
    dca_entry2 = lv["dca_entry2"]
    dca_entry3 = lv["dca_entry3"]
    dca_stop = lv["dca_stop"]
    levels = lv["levels"]
    levels_mode = lv["mode"]

    def _pct(level: float) -> float:
        return (level / current_price - 1) * 100 if current_price else 0.0

    signal_emoji = {
        "STRONG BUY": "🔥", "BUY": "🟢", "WATCH LIST": "👀",
        "WAIT": "⏳", "STAY AWAY": "⛔",
        "OVERBOUGHT": "🔴", "NEUTRAL": "⚪"
    }.get(signal, "⚪")
    analysis_text = "\n".join([f"- {r}" for r in reasons]) if reasons else "- 特筆すべきシグナルなし。様子見推奨。"

    # 7. Geminiで初心者向け要約を生成
    print(f"  🤖 Generating beginner-friendly summary...")
    beginner_summary = ""
    dca_text = (f"DCA Entry1: ${dca_entry1:,.2f}({_pct(dca_entry1):+.1f}%), "
                f"Entry2: ${dca_entry2:,.2f}({_pct(dca_entry2):+.1f}%), "
                f"Entry3: ${dca_entry3:,.2f}({_pct(dca_entry3):+.1f}%), "
                f"Stop: ${dca_stop:,.2f}({_pct(dca_stop):+.1f}%)")
    try:
        beginner_prompt = f"""あなたは仮想通貨の初心者向けアドバイザーです。
以下のテクニカル分析結果を、仮想通貨をこれから始めたい人でもわかるように、やさしい日本語で解説してください。

【分析データ】
- 通貨: {symbol}
- 現在価格: ${current_price:,.2f}（24h変動: {change_24h:+.1f}%）
- シグナル: {signal}（推定勝率: {win_rate_label}）
- 市場レジーム: {regime}（ADX={adx:.1f}, ボラティリティ={atr_pct:.2f}%）
- トレンド: {trend}
- RSI: {current_rsi:.1f}（30以下=売られすぎ、70以上=買われすぎ）
- EMA50: ${ema50:,.2f} / EMA200: ${ema200:,.2f}
- Fear & Greed Index: {fng_value} ({fng_class})
- ニュースリスク判定: {news_risk}
- 流動性回収（ストップ狩り）: {'検知あり' if is_sweep else 'なし'}
- RSIダイバージェンス（反転の兆し）: {'検知あり' if has_divergence else 'なし'}
- 落ちるナイフ判定: {'⚠️危険' if is_falling_knife else '安全'}
- 推奨ポジションサイズ: 資金の{kelly_pct:.1f}%（ケリー基準）
- DCA計画: {dca_text}
- 判定理由: {', '.join(reasons) if reasons else '特になし'}

【執筆ルール】
- 以下の見出し構成で書く（見出しは「**太字**」で表現する）:
  **今の状況** … 現在の価格・トレンド・なぜ動いているか（ニュース分析結果を踏まえて）を2〜3文で。
  **なぜこのシグナルか** … 判定理由を噛み砕いて2〜3文で。落ちるナイフやダイバージェンスも触れる。
  **具体的にどうすればいい？** … ケリー基準のポジションサイズとDCA（分割購入）戦略を初心者向けに2〜3文で。
  **初心者へのアドバイス** … 具体的にどうすべきか1〜2文で。
- 各見出しの後は1行空けて段落を分ける。
- 専門用語は必ず（）内に簡単な説明を添える。例: RSI（相場の過熱感を測る指標）
- 投資は自己責任であることを最後に一言添える。
- 絵文字は使わない。"""
        beginner_resp = client.models.generate_content(
            model=MODEL_NAME, contents=beginner_prompt
        )
        beginner_summary = beginner_resp.text.strip()
        print(f"  ✅ Beginner summary: {len(beginner_summary)} chars")
    except Exception as e:
        print(f"  ⚠️ Beginner summary generation failed: {e}")
        beginner_summary = "（初心者向け要約の生成に失敗しました）"

    beginner_callout = "\n".join(
        f"> {line}" if line.strip() else ">"
        for line in beginner_summary.split("\n")
    )

    # 8. レポート作成
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    sweep_str = f"検知あり (${sweep_level:,.2f})" if is_sweep else "なし"
    div_str = "検知あり ✅" if has_divergence else "なし"
    knife_str = "⚠️ 危険" if is_falling_knife else "安全"
    risk_label = {"SYSTEMIC_RISK": "⛔ 構造的リスク", "NORMAL_CORRECTION": "📉 一時的調整",
                  "NEUTRAL": "✅ 特になし"}.get(news_risk, "✅ 特になし")

    report = (
        f"# {signal_emoji} Crypto Analysis: {symbol} ({today_str})\n\n"
        f"> [!SUMMARY] 判定結果\n"
        f"> **Signal: {signal}** | Regime: {regime} | Trend: {trend} | Price: ${current_price:,.2f} ({change_24h:+.1f}%)\n"
        f"> 推定勝率: {win_rate_label} | 推奨ポジション: 資金の{kelly_pct:.1f}% | リスク: {risk_label}\n\n"
        f"> [!TIP] 初心者向けまとめ\n"
        f"{beginner_callout}\n\n"
        f"## 🕵️ マーケットドライバー (Why)\n"
        f"{news_summary}\n\n"
        f"## 📊 テクニカル指標\n"
        f"| 指標 | 値 | 基準 |\n"
        f"|---|---|---|\n"
        f"| Price | ${current_price:,.2f} ({change_24h:+.1f}%) | - |\n"
        f"| RSI (14) | {current_rsi:.1f} | <30: 買い / >70: 売り |\n"
        f"| EMA 50 | ${ema50:,.2f} | - |\n"
        f"| EMA 200 | ${ema200:,.2f} | - |\n"
        f"| Fear & Greed | {fng_value} ({fng_class}) | <20: 極度の恐怖 |\n"
        f"| ADX (14) | {adx:.1f} | >25: トレンド有 |\n"
        f"| ATR% | {atr_pct:.2f}% | >3%: 高ボラ |\n\n"
        f"## 🧠 構造分析 (Strategy 2.0)\n"
        f"| 項目 | 結果 |\n"
        f"|---|---|\n"
        f"| 市場レジーム | {regime} |\n"
        + (f"| 日足トレンド (ゲート有効) | {daily_trend_str} |\n"
           if daily_trend_str else "") +
        f"| ニュースリスク | {risk_label} |\n"
        f"| 落ちるナイフ | {knife_str} |\n"
        f"| RSIダイバージェンス | {div_str} |\n"
        f"| 流動性回収 (SMC) | {sweep_str} |\n"
        f"| 推定勝率 | {win_rate_label} |\n"
        f"| 推奨ポジション (Kelly) | 資金の**{kelly_pct:.1f}%** |\n\n"
    )

    # DCA計画セクション（BUY系シグナルのみ）
    if signal in ["STRONG BUY", "BUY", "WATCH LIST"]:
        if levels_mode == "atr":
            mode_note = (f"ATRベース (実効ATR {levels['atr_used_pct']:.2f}% × "
                         f"SL{levels['sl_mult']}倍)")
        else:
            mode_note = "固定% (レガシーモード)"
        report += (
            f"## 📋 DCA計画 (分割エントリー) — {mode_note}\n"
            f"| レベル | 価格 | 現在値から |\n"
            f"|---|---|---|\n"
            f"| Entry 1 | ${dca_entry1:,.2f} | {_pct(dca_entry1):+.1f}% |\n"
            f"| Entry 2 | ${dca_entry2:,.2f} | {_pct(dca_entry2):+.1f}% |\n"
            f"| Entry 3 | ${dca_entry3:,.2f} | {_pct(dca_entry3):+.1f}% |\n"
            f"| Stop Loss | ${dca_stop:,.2f} | {_pct(dca_stop):+.1f}% |\n\n"
        )

    report += f"## 📝 分析\n{analysis_text}\n"

    # 9. 03_Cryptoに保存
    clean_symbol = symbol.replace('/', '_')
    filename = f"Crypto_{clean_symbol}_{today_str}.md"
    save_dir = os.path.join(VAULT_ROOT, CRYPTO_DIR_NAME)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)

    with open(save_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✅ Saved: {save_path}")

    # 10. BUY/STRONG BUY なら n8n Webhook に通知
    if signal in ["BUY", "STRONG BUY"]:
        webhook_url = os.getenv("N8N_WEBHOOK_URL", "")
        if webhook_url:
            try:
                payload = {
                    "text": f"{signal_emoji} {symbol} **{signal}**\n"
                            f"Price: ${current_price:,.2f} ({change_24h:+.1f}%) | Regime: {regime}\n"
                            f"RSI: {current_rsi:.1f} | F&G: {fng_value} | Risk: {news_risk}\n"
                            f"Kelly: {kelly_pct:.1f}% | Divergence: {'Yes' if has_divergence else 'No'}",
                    "signal": signal,
                    "signal_type": signal_type or "unknown",
                    "symbol": symbol,
                    "price": current_price,
                    "change_24h": round(change_24h, 2),
                    "rsi": round(current_rsi, 1),
                    "fng": fng_value,
                    "regime": regime,
                    "kelly_pct": round(kelly_pct, 1),
                    "sweep": is_sweep,
                    "divergence": has_divergence,
                    "falling_knife": is_falling_knife,
                    "news_risk": news_risk
                }
                req.post(webhook_url, json=payload, timeout=10)
                print(f"  🚨 n8n notification sent!")
            except Exception as e:
                print(f"  ⚠️ n8n notification failed: {e}")
        else:
            print(f"  ⚠️ N8N_WEBHOOK_URL not set, skipping notification")

    # 11. trade_log への記録（Prediction Audit System）
    # BUY/STRONG BUY = signal (売買コール。Kellyの実測勝率ソース)
    # signal_type付きの WATCH LIST = observation (divergence未確認で見送った
    #   ケースを「仮にエントリーしていたら」で追跡する対照群。Kellyには使わない)
    # NEUTRAL/WAIT/STAY AWAY/OVERBOUGHT は検証したい仮説が紐付かないため記録しない
    record_kind = None
    if signal in ["BUY", "STRONG BUY"]:
        record_kind = RECORD_KIND_SIGNAL
    elif signal == "WATCH LIST" and signal_type:
        record_kind = RECORD_KIND_OBSERVATION

    if record_kind:
        try:
            extra = {"levels_mode": levels_mode}
            if levels is not None:
                extra.update({
                    "atr_value": round(atr_value, 6),
                    "atr_used_pct": round(levels["atr_used_pct"], 2),
                    "sl_atr_mult": levels["sl_mult"],
                })
            _save_prediction(symbol, signal, current_price, dca_stop, kelly_pct,
                             reasons, signal_type=signal_type, extra=extra,
                             record_kind=record_kind)
        except Exception as e:
            print(f"  ⚠️ Prediction log failed: {e}")

    return f"Success: {symbol} = **{signal}** (RSI:{current_rsi:.1f}, F&G:{fng_value}, Risk:{news_risk}). Report: [[{CRYPTO_DIR_NAME}/{filename}]] (Path: {save_path})"


# 主要アルトコインのスキャン対象リスト
SCAN_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "POL/USDT",
    "ATOM/USDT", "UNI/USDT", "LTC/USDT", "NEAR/USDT", "APT/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "FIL/USDT", "INJ/USDT",
]


def _evaluate_symbol(symbol: str, df, fng_value: int) -> dict:
    """1銘柄を本戦略のロジックで評価する (LLM・Tavily・I/Oなし)。

    ライブのフル分析 (tool_analyze_crypto) と同じ _compute_indicators →
    _decide_signal を通す。以前スキャンは `RSI<30 なら BUY` という独自判定を
    持っており、同じ銘柄が /scan と /crypto で違うシグナルを出していた
    (CLAUDE.md「ロジックのフォーク禁止」に抵触)。

    news_risk は "NEUTRAL" 固定 — スキャンは Tavily を叩かないため。
    BUY系が出た銘柄は tool_analyze_crypto を起動し直すので、ニュースゲートは
    そこで適用される (売買コールがゲートを素通りすることはない)。
    """
    ind = _compute_indicators(df)
    signal, confidence, signal_type, reasons = _decide_signal(
        "NEUTRAL", ind["is_falling_knife"], ind["has_divergence"],
        ind["is_sweep"], ind["sweep_level"], fng_value, ind["rsi"],
        ind["regime"], ind["trend"], ind["adx"])

    # 出来高変化率（直近5本 vs 前20本平均）— スキャン表示専用の指標
    recent_vol = df['volume'].iloc[-5:].mean()
    past_vol = df['volume'].iloc[-25:-5].mean()
    vol_change = ((recent_vol / past_vol) - 1) * 100 if past_vol > 0 else 0.0

    # スコアリング（RSI低い＋出来高急増＋恐怖 = 高スコア）
    score = 0
    if ind["rsi"] < 30:
        score += 3
    elif ind["rsi"] < 40:
        score += 1
    elif ind["rsi"] > 70:
        score += 2  # 過熱も注目
    if vol_change > 50:
        score += 2
    elif vol_change > 20:
        score += 1
    if fng_value < 20:
        score += 2
    if signal in ("BUY", "STRONG BUY"):
        score += 3

    return {
        "symbol": symbol,
        "price": ind["price"],
        "rsi": ind["rsi"],
        "vol_change": vol_change,
        "trend": ind["trend"],
        "regime": ind["regime"],
        "signal": signal,
        "signal_type": signal_type,
        "confidence": confidence,
        "reasons": reasons,
        "atr_value": ind["atr_value"],
        "score": score,
    }


def _scan_all_symbols(exchange, fng_value: int) -> list:
    """SCAN_SYMBOLS を4h足で評価する (課金ゼロ: Binance 公開APIのみ)。"""
    results = []
    for symbol in SCAN_SYMBOLS:
        try:
            # 300本必須: 100本ではEMA200がNaN→トレンド列が全銘柄"DOWN"固定になる
            ohlcv = exchange.fetch_ohlcv(symbol, '4h', limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high',
                                              'low', 'close', 'volume'])
            r = _evaluate_symbol(symbol, df, fng_value)
            results.append(r)
            print(f"  ✅ {symbol}: RSI={r['rsi']:.1f}, "
                  f"Vol={r['vol_change']:+.1f}%, {r['signal']}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  ⚠️ {symbol} skipped: {e}")
    return results


def _record_scan_results(results: list) -> dict:
    """スキャン結果を trade_log に反映する。

    - BUY/STRONG BUY … tool_analyze_crypto を起動し直す。記録もニュースゲートも
      あちら側で行われるため、売買コールがゲートを素通りすることはない
    - signal_type付きの WATCH LIST … observation として直接記録 (課金ゼロ)
    - それ以外 … 検証したい仮説が紐付かないため記録しない

    同一銘柄に既にOPENの売買コールがあれば、フル分析を起動する前にスキップする
    (どうせ記録されないものに Gemini/Tavily を払わない)。
    """
    logs = _load_trade_log()
    triggered, observed, skipped, failed = [], [], [], []
    budget_hit = False

    for r in results:
        symbol, signal = r["symbol"], r["signal"]

        if signal in ("BUY", "STRONG BUY"):
            if _has_open_record(logs, symbol, RECORD_KIND_SIGNAL):
                skipped.append(symbol)
                continue
            try:
                SWEEP_COUNTER.check_and_increment()
            except GeminiBudgetExceeded:
                # 初回到達時は cost_guard がDiscord通知済み。観測記録は無料なので続行
                budget_hit = True
                continue
            print(f"  🔎 BUY候補: {symbol} — フル分析を起動")
            try:
                result = tool_analyze_crypto(symbol)
                # ツール層は例外を投げずに "Error: ..." を返す規約なので、
                # 例外が出なかった＝成功と決めつけない
                if is_failure_result(result):
                    failed.append(symbol)
                    print(f"  ❌ {symbol} フル分析が失敗を返した: {str(result)[:120]}")
                else:
                    triggered.append(symbol)
            except Exception as e:
                failed.append(symbol)
                print(f"  ❌ {symbol} フル分析で例外: {e}")

        elif signal == "WATCH LIST" and r.get("signal_type"):
            if _has_open_record(logs, symbol, RECORD_KIND_OBSERVATION):
                continue
            lv = _resolve_levels(r["price"], r["atr_value"])
            win_rate, _basis, _n = _resolve_win_rate(r["signal_type"],
                                                    r["confidence"], logs)
            extra = {"levels_mode": lv["mode"]}
            if lv["levels"] is not None:
                extra.update({
                    "atr_value": round(r["atr_value"], 6),
                    "atr_used_pct": round(lv["levels"]["atr_used_pct"], 2),
                    "sl_atr_mult": lv["levels"]["sl_mult"],
                })
            try:
                rid = _save_prediction(
                    symbol, signal, r["price"], lv["dca_stop"],
                    _calculate_kelly(win_rate), r["reasons"],
                    signal_type=r["signal_type"], extra=extra,
                    record_kind=RECORD_KIND_OBSERVATION)
                if rid:
                    observed.append(symbol)
            except Exception as e:
                print(f"  ⚠️ {symbol} 観測記録失敗: {e}")

    return {"triggered": triggered, "observed": observed, "skipped": skipped,
            "failed": failed, "budget_hit": budget_hit}


def tool_crypto_sweep() -> str:
    """4h足クローズ毎の全銘柄スイープ (scheduler から silent 起動)。

    本戦略は4h足だが、ライブは長らく「08:30 の BTC 1本/日 + watcher の急変時」
    しか評価しておらず、バックテスト (4h足ごと×全銘柄で年36件/銘柄) に対して
    機会の数十分の一しかサンプルできていなかった。結果 trade_log は5ヶ月ゼロ件。
    ここで密度を揃える。

    評価そのものは Binance 公開APIのみで課金ゼロ。Gemini/Tavily を使うのは
    BUY系候補にフル分析を起動するときだけ (SWEEP_COUNTER で日次上限)。
    """
    print(f"[Tool] 🌀 Crypto Sweep ({len(SCAN_SYMBOLS)} symbols)...")
    exchange = ccxt.binance({'enableRateLimit': True})
    fng_value, _fng_class = fetch_fear_greed_index()

    results = _scan_all_symbols(exchange, fng_value)
    if not results:
        return "Error: Could not fetch any crypto data."

    rec = _record_scan_results(results)
    parts = [f"{len(results)}銘柄を評価",
             f"フル分析起動 {len(rec['triggered'])}件",
             f"観測記録 {len(rec['observed'])}件"]
    if rec["skipped"]:
        parts.append(f"OPEN重複スキップ {len(rec['skipped'])}件")
    if rec["budget_hit"]:
        parts.append("⚠️ 分析予算上限に到達")
    # 失敗を成功サマリに埋もれさせない (ツール層の "Error:" 規約に合わせる)
    if rec["failed"]:
        return (f"Error: Sweep: {', '.join(rec['failed'])} のフル分析に失敗 | "
                + " | ".join(parts))
    return "Success: Sweep: " + " | ".join(parts)


def tool_scan_crypto_market() -> str:
    """
    主要20銘柄を一括スキャンし、RSI・出来高変化率・Fear&Greedから
    注目すべき通貨をランキング形式でピックアップする。
    結果を03_Crypto/Market_Scan_{date}.mdに保存。
    """
    print(f"[Tool] 🔭 Scanning Crypto Market ({len(SCAN_SYMBOLS)} symbols)...")

    exchange = ccxt.binance({'enableRateLimit': True})

    # Fear & Greed Index（全銘柄共通）
    fng_value, fng_class = fetch_fear_greed_index()

    results = _scan_all_symbols(exchange, fng_value)

    if not results:
        return "Error: Could not fetch any crypto data."

    # ここでは記録もフル分析の起動もしない。/scan は Discord から手動で叩く
    # 読み物であり、副作用として Gemini/Tavily を走らせると応答が数分単位に
    # 伸びる上に日次予算を食う。記録は tool_crypto_sweep の単一経路に寄せる
    # (09:05 の sweep が同じ4h足クローズを拾う)。

    # スコア降順 → RSI昇順でソート
    results.sort(key=lambda x: (-x['score'], x['rsi']))

    # Geminiで初心者向け解説を生成
    print(f"  🤖 Generating beginner-friendly market overview...")
    top5 = results[:5]
    top5_text = "\n".join([
        f"- {r['symbol']}: 価格=${r['price']:,.2f}, RSI={r['rsi']:.1f}, "
        f"出来高変化={r['vol_change']:+.1f}%, トレンド={r['trend']}, シグナル={r['signal']}"
        for r in top5
    ])
    beginner_overview = ""
    try:
        overview_prompt = f"""あなたは仮想通貨の初心者向けアドバイザーです。
以下は本日の仮想通貨市場スキャン結果のうち、注目度の高い上位5銘柄です。
仮想通貨をこれから始めたい人向けに、やさしい日本語で解説してください。

【市場の温度感】
Fear & Greed Index: {fng_value} ({fng_class})

【注目銘柄 TOP5】
{top5_text}

【執筆ルール】
- 以下の見出し構成で書く（見出しは「**太字**」で表現する）:
  **市場の温度感** … Fear & Greedの数値が何を意味するか1〜2文で。
  **注目銘柄ピックアップ** … 各銘柄1〜2文で「なぜ注目か」を説明。
  **アルトコイン投資の注意点** … 初心者向けの注意点を1〜2文で。
- 各見出しの後は1行空けて段落を分ける。
- 専門用語には必ず（）で簡単な説明を添える。
- 投資は自己責任であることを一言添える。
- 絵文字は使わない。"""
        overview_resp = client.models.generate_content(
            model=MODEL_NAME, contents=overview_prompt
        )
        beginner_overview = overview_resp.text.strip()
        print(f"  ✅ Overview: {len(beginner_overview)} chars")
    except Exception as e:
        print(f"  ⚠️ Overview generation failed: {e}")
        beginner_overview = "（初心者向け解説の生成に失敗しました）"

    # Callout用に各行に "> " を付与
    overview_callout = "\n".join(
        f"> {line}" if line.strip() else ">"
        for line in beginner_overview.split("\n")
    )

    # レポート作成
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    # フル分析 (tool_analyze_crypto) と同じ対応表。スキャンが _decide_signal を
    # 通すようになり WATCH LIST / WAIT / STAY AWAY も出るため揃える
    signal_emoji_map = {
        "STRONG BUY": "🔥", "BUY": "🟢", "WATCH LIST": "👀",
        "WAIT": "⏳", "STAY AWAY": "⛔", "OVERBOUGHT": "🔴", "NEUTRAL": "⚪",
    }

    table_rows = ""
    for r in results:
        emoji = signal_emoji_map.get(r['signal'], "⚪")
        table_rows += (
            f"| {emoji} {r['symbol']} | ${r['price']:,.2f} | "
            f"{r['rsi']:.1f} | {r['vol_change']:+.1f}% | "
            f"{r['trend']} | {r['regime']} | {r['signal']} |\n"
        )

    report = (
        f"# 🔭 Market Scan ({today_str})\n\n"
        f"> [!SUMMARY] 市場概況\n"
        f"> Fear & Greed: **{fng_value}** ({fng_class}) | スキャン銘柄数: {len(results)}\n\n"
        f"> [!TIP] 初心者向け：今日の注目ポイント\n"
        f"{overview_callout}\n\n"
        f"## 📊 全銘柄スキャン結果\n"
        f"| 銘柄 | 価格 | RSI | 出来高変化 | トレンド | レジーム | シグナル |\n"
        f"|---|---|---|---|---|---|---|\n"
        f"{table_rows}\n"
        f"## 🏆 注目銘柄 TOP5\n"
    )
    for i, r in enumerate(top5, 1):
        emoji = signal_emoji_map.get(r['signal'], "⚪")
        report += (
            f"### {i}. {emoji} {r['symbol']}\n"
            f"- 価格: ${r['price']:,.2f}\n"
            f"- RSI: {r['rsi']:.1f} / 出来高変化: {r['vol_change']:+.1f}%\n"
            f"- トレンド: {r['trend']} / シグナル: **{r['signal']}**\n\n"
        )

    # 保存
    filename = f"Market_Scan_{today_str}.md"
    save_dir = os.path.join(VAULT_ROOT, CRYPTO_DIR_NAME)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)

    with open(save_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✅ Saved: {save_path}")

    # 結果サマリー
    buy_signals = [r for r in results if r['signal'] in ["BUY", "STRONG BUY"]]
    summary = f"Scanned {len(results)} symbols. "
    if buy_signals:
        names = ", ".join([r['symbol'] for r in buy_signals])
        summary += f"Buy signals: {names}. "
    else:
        summary += "No buy signals found. "
    summary += f"F&G: {fng_value}. Report: [[{CRYPTO_DIR_NAME}/{filename}]]"

    return f"Success: {summary} (Path: {save_path})"


# ---------------------------------------------------------
# Prediction Audit Tool
# ---------------------------------------------------------

AUDIT_EXPIRY_DAYS = 21


def _search_loss_reason(symbol: str, entry_time: datetime.datetime) -> str:
    """LOSE判定の敗因を Tavily で調べて1行にまとめる。失敗しても文字列を返す。"""
    coin_name = symbol.split('/')[0]
    query = (f"Why did {coin_name} price drop crypto news "
             f"{entry_time.strftime('%Y-%m')}")
    try:
        search_res = tavily_client.search(query=query, topic="news", max_results=3)
    except Exception as e:
        return f"ニュース検索失敗: {e}"
    news_items = [f"[{r.get('title', 'No title')}]({r.get('url', '')})"
                  for r in search_res.get('results', [])]
    return "; ".join(news_items) if news_items else "関連ニュース見つからず"


def _to_utc_naive(local_dt: datetime.datetime) -> datetime.datetime:
    """ローカル時刻の naive datetime を UTC の naive datetime に変換する。

    trade_log の timestamp は `datetime.now().isoformat()` = ローカル(JST)naive、
    ccxt の足は epoch ms → `pd.to_datetime(unit='ms')` = UTC naive。
    この2つをそのまま比較すると JST 分(9時間)ずれ、_resolve_trade が
    「エントリー直後9時間の足」を entry より前とみなして捨てる
    (= その間にSL/TPへ到達したトレードを取りこぼす)。
    バックテスターは全て UTC naive で閉じているため、ライブ側を UTC に寄せる。
    """
    return datetime.datetime.fromtimestamp(
        local_dt.timestamp(), datetime.timezone.utc).replace(tzinfo=None)


def _fetch_audit_candles(exchange, symbol: str, since_dt: datetime.datetime):
    """監査用1h足を「その銘柄の最古エントリー以降」でまとめて1回取得する。

    以前は OPEN 1件ごとに fetch_ohlcv していたため、記録件数に比例して
    22:30 の監査が伸びた。銘柄単位にまとめることで API 呼び出しは
    OPEN件数ではなく銘柄数で頭打ちになる。
    limit は 21日(=504本)に余裕を持たせた 600本。
    """
    ohlcv = exchange.fetch_ohlcv(symbol, '1h',
                                 since=int(since_dt.timestamp() * 1000),
                                 limit=600)
    if not ohlcv:
        return None
    df = pd.DataFrame(ohlcv,
                      columns=['timestamp', 'open', 'high', 'low', 'close', 'vol'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df


def tool_audit_predictions() -> str:
    """
    過去の予測ログ(trade_log.json)を読み込み、
    Binanceの実績価格と照合してWIN/LOSEを判定する。
    LOSEの場合はTavilyでニュース検索し敗因を分析。
    結果をDev_Logs/Audit_Report_{date}.mdに保存する。
    """
    print("[Tool] 🕵️ Auditing past predictions...")

    logs = _load_trade_log()
    # 破損レコード1件で毎晩の監査が丸ごと落ちないよう .get で読む
    open_trades = [t for t in logs
                   if isinstance(t, dict) and t.get("status") == "OPEN"
                   and t.get("symbol")]

    if not open_trades:
        return "評価待ちの予測（OPEN）はありません。"

    exchange = ccxt.binance({'enableRateLimit': True})
    now = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    report = f"# 🕵️ AI Prediction Audit Report ({today_str})\n\n"
    report += f"> [!SUMMARY] 概要\n"
    report += f"> 評価対象: {len(open_trades)}件のOPENポジション\n\n"

    updated_count = 0
    win_count = 0
    lose_count = 0
    expired_count = 0
    # 監査はネットワーク往復で分単位かかるため、ロックは保存時のみ取得する。
    # ループ中は更新内容を id キーで貯めておき、最後にロック下で再読込→適用する
    # (監査中に bot/watcher が追記した新レコードを潰さないため)
    updates = {}

    # 銘柄単位にまとめて1回だけ市場データを取る
    by_symbol = {}
    for t in open_trades:
        by_symbol.setdefault(t["symbol"], []).append(t)

    for symbol, trades in by_symbol.items():
        entry_times = {}
        for t in trades:
            try:
                entry_times[t["id"]] = datetime.datetime.fromisoformat(t["timestamp"])
            except (TypeError, ValueError, KeyError):
                report += f"- ⚠️ {symbol} (ID: {t.get('id')}): timestamp破損のためスキップ\n"
        if not entry_times:
            continue

        # fetch は epoch 変換なのでローカル naive のまま渡す (.timestamp() が正しく効く)
        oldest = min(entry_times.values())
        print(f"  📊 Fetching {symbol} 1h since {oldest:%Y-%m-%d} "
              f"({len(entry_times)}件)...")
        try:
            df = _fetch_audit_candles(exchange, symbol, oldest)
        except Exception as e:
            report += f"- ⚠️ {symbol}: データ取得失敗 ({e})\n"
            print(f"    ⚠️ Data fetch error: {e}")
            continue
        if df is None or df.empty:
            print(f"    ⚠️ No data returned for {symbol}")
            continue

        current_close = float(df['close'].iloc[-1])

        for trade in trades:
            entry_time = entry_times.get(trade.get("id"))
            if entry_time is None:
                continue
            # 足は UTC naive なので、比較用に entry も UTC へ揃える
            entry_utc = _to_utc_naive(entry_time)
            kind = trade.get("record_kind", RECORD_KIND_SIGNAL)

            # 決着判定は lib/quant_core._resolve_trade に統一。
            # 旧実装は観測窓全体の min(low)/max(high) による SL優先の悲観近似で、
            # 「TP先着 → 後日SL」を LOSE と誤判定していた (バックテストは時系列
            # 走査なので同じ建玉の勝敗が食い違う = 判定のフォーク)。
            # 過去レコードがゼロの今が互換性を気にせず揃えられる唯一の機会。
            new_status, exit_time, note = _resolve_trade(
                trade["entry_price"], trade["stop_loss"], trade["target_price"],
                df, entry_utc, expiry_days=AUDIT_EXPIRY_DAYS)

            # データ端まで未決着でも実時間で期限を過ぎていれば期限切れにする
            # (取得窓が足りずEXPIREDバーが無いケース)
            if (new_status == "OPEN"
                    and (now - entry_time).days >= AUDIT_EXPIRY_DAYS):
                new_status, note = "EXPIRED", f"{AUDIT_EXPIRY_DAYS}日経過してもSL/TP未到達"

            if new_status == "LOSE":
                lose_count += 1
                loss_pct = ((trade["stop_loss"] - trade["entry_price"])
                            / trade["entry_price"]) * 100
                # 敗因のニュース検索は売買コールのみ。observation は対照群として
                # 大量に決着するため、ここで Tavily を叩くとクォータを浪費する
                if kind == RECORD_KIND_SIGNAL:
                    news_text = _search_loss_reason(symbol, entry_time)
                else:
                    news_text = "（観測レコードのため敗因検索はスキップ）"
                audit_comment = (f"損切りライン(${trade['stop_loss']:,.2f})に到達。"
                                 f"損失: {loss_pct:+.1f}%。敗因: {news_text}")
            elif new_status == "WIN":
                win_count += 1
                gain_pct = ((trade["target_price"] - trade["entry_price"])
                            / trade["entry_price"]) * 100
                audit_comment = (f"利確目標(${trade['target_price']:,.2f})達成。"
                                 f"利益: +{gain_pct:.1f}%。予測通りの値動き。")
            elif new_status == "EXPIRED":
                expired_count += 1
                audit_comment = (
                    f"エントリーから{AUDIT_EXPIRY_DAYS}日経過してもSL/TP未到達のため"
                    f"期限切れクローズ。観測窓の最終価格: ${current_close:,.2f}"
                    f"（現在値ではない）。勝率統計からは除外。")
            else:
                dist_to_tp = ((trade["target_price"] - current_close)
                              / current_close) * 100
                dist_to_sl = ((current_close - trade["stop_loss"])
                              / current_close) * 100
                hours_elapsed = int((now - entry_time).total_seconds() // 3600)
                audit_comment = (f"未決着 ({hours_elapsed}h経過)。現在値: "
                                 f"${current_close:,.2f} (TP まで {dist_to_tp:+.1f}%, "
                                 f"SL まで {dist_to_sl:.1f}%)")

            # ログ更新 (localのtradeはレポート描画用、updatesが保存時の正)
            if new_status != "OPEN":
                trade["status"] = new_status
                trade["audit_note"] = audit_comment
                trade["audit_date"] = today_str
                updates[trade["id"]] = {
                    "status": new_status,
                    "audit_note": audit_comment,
                    "audit_date": today_str,
                }
                updated_count += 1

                icon = {"WIN": "✅", "LOSE": "❌", "EXPIRED": "⌛"}.get(new_status, "❓")
                kind_label = ("売買コール" if kind == RECORD_KIND_SIGNAL
                              else "観測(対照群)")
                report += (
                    f"## {icon} {symbol} — {new_status} (ID: {trade['id']})\n"
                    f"| 項目 | 値 |\n|---|---|\n"
                    f"| レコード種別 | {kind_label} |\n"
                    f"| シグナル | {trade['signal']} |\n"
                    f"| シグナル種別 | {trade.get('signal_type', 'legacy')} |\n"
                    f"| エントリー | ${trade['entry_price']:,.2f} |\n"
                    f"| ターゲット | ${trade['target_price']:,.2f} |\n"
                    f"| ストップ | ${trade['stop_loss']:,.2f} |\n"
                    f"| Kelly% | {trade['kelly_pct']}% |\n"
                    f"| 予測日 | {trade['timestamp'][:10]} |\n"
                    f"| 決着 | {exit_time if exit_time is not None else '—'} ({note}) |\n"
                    f"| 判定理由 | {trade.get('reason', 'N/A')[:80]} |\n\n"
                    f"**監査結果**: {audit_comment}\n\n"
                )
                print(f"    {icon} {new_status} {trade['id']}: {audit_comment[:70]}...")
            else:
                print(f"    ⏳ Still OPEN {trade['id']}: {audit_comment[:70]}...")

        time.sleep(0.3)

    # JSONログ更新: ロック下で最新を再読込し、id一致レコードにのみ判定結果を適用
    logs = _update_trade_log(lambda all_logs: _apply_audit_updates(all_logs, updates))

    # 統計セクション（今回の監査分）
    # EXPIREDのみの回でも統計を出す（total_closedではなくupdated_countで判定）
    total_closed = win_count + lose_count
    if updated_count > 0:
        win_rate_str = f"{(win_count / total_closed) * 100:.1f}%" if total_closed > 0 else "—"
        report += (
            f"## 📈 統計（今回の監査分）\n"
            f"| 項目 | 値 |\n|---|---|\n"
            f"| 判定済み | {total_closed}件 |\n"
            f"| WIN | {win_count}件 |\n"
            f"| LOSE | {lose_count}件 |\n"
            f"| 勝率 | {win_rate_str} |\n"
            f"| EXPIRED (期限切れ) | {expired_count}件 |\n"
            f"| 未決着 | {len(open_trades) - updated_count}件 |\n\n"
        )

    # 観測レコード(WATCH LIST対照群)の累積。Kellyには使わないので別掲する
    obs = [t for t in logs if isinstance(t, dict)
           and t.get("record_kind") == RECORD_KIND_OBSERVATION
           and t.get("status") in ("WIN", "LOSE")]
    if obs:
        obs_win = sum(1 for t in obs if t["status"] == "WIN")
        report += (
            f"## 👀 観測レコード（WATCH LIST を仮にエントリーしていたら）\n"
            f"判定済み {len(obs)}件 / WIN {obs_win}件 "
            f"({obs_win / len(obs) * 100:.1f}%)。"
            f"Kellyの実測勝率には使わない対照群。\n\n"
        )

    # シグナル種別ごとの累積実績（Kellyの実測勝率ソース。n>=MIN_AUDIT_SAMPLESで実測に切替）
    signal_stats = _get_signal_stats(logs)
    if signal_stats:
        report += (
            f"## 🎯 シグナル種別ごとの累積実績\n"
            f"| 種別 | WIN | LOSE | 勝率 | Kelly実測適用 |\n"
            f"|---|---|---|---|---|\n"
        )
        for stype, s in sorted(signal_stats.items()):
            n = s["wins"] + s["losses"]
            applied = "✅ 適用中" if n >= MIN_AUDIT_SAMPLES else f"⏳ あと{MIN_AUDIT_SAMPLES - n}件"
            report += (
                f"| {stype} | {s['wins']} | {s['losses']} | "
                f"{s['win_rate']*100:.1f}% (n={n}) | {applied} |\n"
            )
        report += "\n"

    if updated_count == 0:
        still_open = len(open_trades)
        return f"OPEN {still_open}件を確認しましたが、まだSL/TPに到達していません。"

    # レポート保存
    report_filename = f"Audit_Report_{today_str}.md"
    report_dir = os.path.join(VAULT_ROOT, CRYPTO_DIR_NAME, "Audit_Report")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, report_filename)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✅ Audit report saved: {report_path}")

    return (f"Success: {updated_count}件を判定 (WIN:{win_count}, LOSE:{lose_count}, "
            f"EXPIRED:{expired_count})。レポート: [[{CRYPTO_DIR_NAME}/Audit_Report/{report_filename}]]")
