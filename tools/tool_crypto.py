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


# ---------------------------------------------------------
# Next-Gen Quant Helpers
# ---------------------------------------------------------

def _calculate_kelly(win_rate: float, reward_ratio: float = 2.0, fraction: float = 0.25) -> float:
    """フラクショナル・ケリー基準でポジションサイズ(%)を算出"""
    if win_rate <= 0:
        return 0.0
    q = 1 - win_rate
    kelly_f = (win_rate * reward_ratio - q) / reward_ratio
    return max(kelly_f * fraction * 100, 0.0)


def _detect_regime(df) -> tuple:
    """ADX + ATR% で市場レジームを判定 → (regime, adx, atr_pct, dmp, dmn)"""
    adx_s = ta_lib.adx(df['high'], df['low'], df['close'], length=14)
    atr_s = ta_lib.atr(df['high'], df['low'], df['close'], length=14)
    adx = float(adx_s['ADX_14'].iloc[-1]) if adx_s is not None and pd.notna(adx_s['ADX_14'].iloc[-1]) else 20.0
    dmp = float(adx_s['DMP_14'].iloc[-1]) if adx_s is not None and 'DMP_14' in adx_s.columns and pd.notna(adx_s['DMP_14'].iloc[-1]) else 0.0
    dmn = float(adx_s['DMN_14'].iloc[-1]) if adx_s is not None and 'DMN_14' in adx_s.columns and pd.notna(adx_s['DMN_14'].iloc[-1]) else 0.0
    atr_val = float(atr_s.iloc[-1]) if atr_s is not None and pd.notna(atr_s.iloc[-1]) else 0.0
    atr_pct = (atr_val / float(df['close'].iloc[-1])) * 100 if df['close'].iloc[-1] > 0 else 0.0

    if atr_pct > 3.0:
        regime = "VOLATILE"
    elif adx > 25:
        regime = "TRENDING"
    else:
        regime = "SIDEWAYS"
    return regime, adx, atr_pct, dmp, dmn


def _detect_liquidity_sweep(df, window: int = 20) -> tuple:
    """SMC: 直近安値をヒゲで割り込み終値で戻すパターン → (bool, sweep_level)"""
    if len(df) < window + 1:
        return False, 0.0
    recent_low = float(df['low'].iloc[-(window+1):-1].min())
    latest = df.iloc[-1]
    is_sweep = (float(latest['low']) < recent_low) and (float(latest['close']) > recent_low)
    return is_sweep, recent_low


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

    summary_lines = []
    sentiment_score = 0

    risk_keywords = ["hack", "sec", "ban", "bankruptcy", "fraud", "war", "collapse",
                     "exploit", "rug pull", "delisting", "shutdown"]
    fud_keywords = ["rumor", "correction", "profit taking", "liquidations",
                    "sell-off", "pullback", "fear"]

    for res in response.get("results", []):
        content = res.get('content', '').lower()
        title = res.get('title', 'No title')
        url = res.get('url', '')
        summary_lines.append(f"- [{title}]({url})")

        if any(w in content for w in risk_keywords):
            sentiment_score -= 2
        elif any(w in content for w in fud_keywords):
            sentiment_score -= 0.5

    if not summary_lines:
        return "関連ニュースが見つかりませんでした。", "NEUTRAL"

    if sentiment_score <= -2:
        risk_level = "SYSTEMIC_RISK"
    elif sentiment_score < 0:
        risk_level = "NORMAL_CORRECTION"
    else:
        risk_level = "NEUTRAL"

    return "\n".join(summary_lines), risk_level


def _check_divergence(df, rsi_col: str = 'rsi', window: int = 20) -> bool:
    """
    強気RSIダイバージェンス検知:
    価格は安値を更新（または同等）だが、RSIは切り上がっている場合にTrue。
    """
    if len(df) < window + 1 or rsi_col not in df.columns:
        return False

    lookback = df.iloc[-(window+1):-1]
    if lookback['low'].isna().all() or lookback[rsi_col].isna().all():
        return False

    last_low_idx = lookback['low'].idxmin()
    if pd.isna(last_low_idx):
        return False

    current_low = float(df['low'].iloc[-1])
    prev_low = float(df['low'].loc[last_low_idx])
    prev_rsi = df[rsi_col].loc[last_low_idx]
    current_rsi = df[rsi_col].iloc[-1]

    if pd.isna(prev_rsi) or pd.isna(current_rsi):
        return False

    return (current_low <= float(prev_low)) and (float(current_rsi) > float(prev_rsi) + 3)


def _decide_signal(news_risk: str, is_falling_knife: bool, has_divergence: bool,
                   is_sweep: bool, sweep_level: float, fng_value: int,
                   current_rsi: float, regime: str, trend: str, adx: float) -> tuple:
    """
    Strategy 2.0 統合判定ロジック（純関数）。
    Returns: (signal, confidence, signal_type, reasons)
    signal_type は最初に成立したブランチで確定する
    (優先順位: smc_sweep > panic_reversal > trend_pullback > rsi_oversold)。
    """
    signal = "NEUTRAL"
    confidence = 0.0
    signal_type = None
    reasons = []

    # 0: ファンダメンタルズ・リスク（最優先 - テクニカル無効化）
    if news_risk == "SYSTEMIC_RISK":
        signal = "STAY AWAY"
        confidence = 0.0
        reasons.append("⚠️ ファンダメンタルズに深刻な悪材料あり。テクニカル無効。")

    # A: 落ちるナイフ回避（ダイバージェンスなしの強い下落局面）
    elif is_falling_knife and not has_divergence:
        signal = "WAIT"
        confidence = 0.0
        reasons.append(f"📉 下落トレンドが強すぎます (ADX: {adx:.1f})。底打ち確認（ダイバージェンス）まで待機。")

    else:
        # B: SMC Liquidity Sweep
        if is_sweep:
            signal = "STRONG BUY"
            confidence = 0.65
            signal_type = "smc_sweep"
            reasons.append(f"流動性回収を検知: 直近安値 ${sweep_level:,.2f} をヒゲで割込み反発（SMC）")

        # C: パニック・リバーサル（ダイバージェンスで確度UP）
        if fng_value < 20 and current_rsi < 25:
            if has_divergence:
                signal = "STRONG BUY"
                confidence = max(confidence, 0.65)
                if signal_type is None:
                    signal_type = "panic_reversal"
                reasons.append(f"極度の恐怖 (F&G: {fng_value}) + 売られすぎ (RSI: {current_rsi:.1f}) + ダイバージェンス確認")
            else:
                if signal == "NEUTRAL":
                    signal = "WATCH LIST"
                    confidence = max(confidence, 0.35)
                    if signal_type is None:
                        signal_type = "panic_reversal"
                reasons.append(f"極度の恐怖 (F&G: {fng_value}) + 売られすぎ (RSI: {current_rsi:.1f})。反転兆候待ち。")

        # D: トレンドフォロー押し目
        if signal == "NEUTRAL" and regime == "TRENDING" and trend == "UP" and current_rsi < 45:
            signal = "BUY"
            confidence = 0.55
            signal_type = "trend_pullback"
            reasons.append(f"上昇トレンド中の押し目 (RSI: {current_rsi:.1f}, ADX: {adx:.1f})")

        # E: RSI判定（ダイバージェンスで確度UP）
        if signal == "NEUTRAL":
            if current_rsi < 30:
                if has_divergence:
                    signal = "BUY"
                    confidence = 0.55
                    signal_type = "rsi_oversold"
                    reasons.append(f"RSI売られすぎ ({current_rsi:.1f}) + ダイバージェンス確認。反転の可能性。")
                else:
                    signal = "WATCH LIST"
                    confidence = 0.35
                    signal_type = "rsi_oversold"
                    reasons.append(f"RSI売られすぎ水準 ({current_rsi:.1f}) だが反転兆候なし。監視継続。")
            elif current_rsi > 70:
                signal = "OVERBOUGHT"
                confidence = 0.45
                reasons.append(f"RSIが買われすぎ水準 ({current_rsi:.1f})")

    return signal, confidence, signal_type, reasons


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
# 実測勝率の上限クランプ（少数サンプルの偶然の連勝でポジションが暴走しない保険）
EMPIRICAL_WIN_RATE_CAP = 0.75


def _get_signal_stats(logs: list = None) -> dict:
    """
    判定済み(WIN/LOSE)レコードをsignal_type別に集計する。
    EXPIREDは観測窓の限界によるクローズであり勝敗ではないため除外。
    signal_type未設定の旧レコードは "legacy" として分離集計。
    Returns: {signal_type: {"wins": int, "losses": int, "win_rate": float}}
    """
    if logs is None:
        logs = _load_trade_log()
    stats = {}
    for t in logs:
        if t.get("status") not in ("WIN", "LOSE"):
            continue
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


def _resolve_win_rate(signal_type: str, default_confidence: float) -> tuple:
    """
    Kellyに渡す勝率を決定する。
    判定済みサンプルが MIN_AUDIT_SAMPLES 以上ある種別は実測勝率
    （EMPIRICAL_WIN_RATE_CAP でクランプ）、それ未満は既定confidence。
    Returns: (win_rate, basis, sample_count)  basis: "実測" | "既定値"
    """
    if not signal_type:
        return default_confidence, "既定値", 0
    stats = _get_signal_stats().get(signal_type)
    if not stats:
        return default_confidence, "既定値", 0
    n = stats["wins"] + stats["losses"]
    if n < MIN_AUDIT_SAMPLES:
        return default_confidence, "既定値", n
    return min(stats["win_rate"], EMPIRICAL_WIN_RATE_CAP), "実測", n


def _save_prediction(symbol: str, signal: str, entry_price: float,
                     stop_loss: float, kelly_pct: float, reasons: list,
                     signal_type: str = "unknown") -> str:
    """BUY系シグナル発生時に予測をログに記録する。"""
    risk = entry_price - stop_loss
    target_price = entry_price + (risk * 2) if risk > 0 else entry_price * 1.10

    record = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.datetime.now().isoformat(),
        "symbol": symbol,
        "signal": signal,
        "signal_type": signal_type or "unknown",
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "target_price": round(target_price, 2),
        "kelly_pct": round(kelly_pct, 1),
        "reason": "; ".join(reasons) if reasons else "N/A",
        "status": "OPEN",
        "audit_note": ""
    }

    _update_trade_log(lambda logs: logs.append(record))
    print(f"  📝 Prediction logged: {record['id']} ({symbol} {signal} @ ${entry_price:,.2f})")
    return record["id"]


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

    # 1. 価格データの取得 (4時間足, 100本)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '4h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    except Exception as e:
        return f"Error fetching {symbol} data: {e}"

    # 2. テクニカル指標の計算
    df['rsi'] = ta_lib.rsi(df['close'], length=14)
    df['ema50'] = ta_lib.ema(df['close'], length=50)
    df['ema200'] = ta_lib.ema(df['close'], length=200)
    macd_df = ta_lib.macd(df['close'])
    if macd_df is not None:
        df = pd.concat([df, macd_df], axis=1)

    latest = df.iloc[-1]
    current_price = float(latest['close'])
    current_rsi = float(latest['rsi']) if pd.notna(latest['rsi']) else 50.0
    ema50 = float(latest['ema50']) if pd.notna(latest['ema50']) else current_price
    ema200 = float(latest['ema200']) if pd.notna(latest['ema200']) else current_price

    # 3. Fear & Greed Index の取得
    fng_value, fng_class = fetch_fear_greed_index()

    # 4. Next-Gen: レジーム検知 + SMC + ダイバージェンス + マーケットドライバー
    regime, adx, atr_pct, dmp, dmn = _detect_regime(df)
    is_sweep, sweep_level = _detect_liquidity_sweep(df)
    has_divergence = _check_divergence(df)
    print(f"  📈 Regime: {regime} (ADX={adx:.1f}, ATR%={atr_pct:.2f})")
    if is_sweep:
        print(f"  🎯 Liquidity Sweep detected at {sweep_level:.2f}")
    if has_divergence:
        print(f"  📊 RSI Bullish Divergence detected")

    # 4b. 24h変動率 + マーケットドライバー分析 ("Why")
    price_24h_ago = float(df['close'].iloc[-7]) if len(df) >= 7 else current_price
    change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100
    news_summary, news_risk = _analyze_market_drivers(symbol, change_24h)
    print(f"  🕵️ News Risk: {news_risk}")

    # 4c. 落ちるナイフ判定 (ADX > 30 + 下降モメンタム)
    is_falling_knife = (adx > 30) and (dmn > dmp)
    if is_falling_knife:
        print(f"  🔪 Falling Knife warning (ADX={adx:.1f}, DM-={dmn:.1f} > DM+={dmp:.1f})")

    # 5. Strategy 2.0 統合判定ロジック（→ _decide_signal に抽出済み）
    trend = "UP" if current_price > ema200 else "DOWN"
    signal, confidence, signal_type, reasons = _decide_signal(
        news_risk, is_falling_knife, has_divergence, is_sweep, sweep_level,
        fng_value, current_rsi, regime, trend, adx)

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

    # 6b. DCA計画（分割エントリー水準）
    dca_entry1 = current_price * 0.95
    dca_entry2 = current_price * 0.90
    dca_entry3 = current_price * 0.85
    dca_stop = current_price * 0.88

    signal_emoji = {
        "STRONG BUY": "🔥", "BUY": "🟢", "WATCH LIST": "👀",
        "WAIT": "⏳", "STAY AWAY": "⛔",
        "OVERBOUGHT": "🔴", "NEUTRAL": "⚪"
    }.get(signal, "⚪")
    analysis_text = "\n".join([f"- {r}" for r in reasons]) if reasons else "- 特筆すべきシグナルなし。様子見推奨。"

    # 7. Geminiで初心者向け要約を生成
    print(f"  🤖 Generating beginner-friendly summary...")
    beginner_summary = ""
    dca_text = (f"DCA Entry1: ${dca_entry1:,.2f}(-5%), Entry2: ${dca_entry2:,.2f}(-10%), "
                f"Entry3: ${dca_entry3:,.2f}(-15%), Stop: ${dca_stop:,.2f}(-12%)")
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
        f"| ニュースリスク | {risk_label} |\n"
        f"| 落ちるナイフ | {knife_str} |\n"
        f"| RSIダイバージェンス | {div_str} |\n"
        f"| 流動性回収 (SMC) | {sweep_str} |\n"
        f"| 推定勝率 | {win_rate_label} |\n"
        f"| 推奨ポジション (Kelly) | 資金の**{kelly_pct:.1f}%** |\n\n"
    )

    # DCA計画セクション（BUY系シグナルのみ）
    if signal in ["STRONG BUY", "BUY", "WATCH LIST"]:
        report += (
            f"## 📋 DCA計画 (分割エントリー)\n"
            f"| レベル | 価格 | 現在値から |\n"
            f"|---|---|---|\n"
            f"| Entry 1 | ${dca_entry1:,.2f} | -5% |\n"
            f"| Entry 2 | ${dca_entry2:,.2f} | -10% |\n"
            f"| Entry 3 | ${dca_entry3:,.2f} | -15% |\n"
            f"| Stop Loss | ${dca_stop:,.2f} | -12% |\n\n"
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

    # 11. 売買シグナル(BUY/STRONG BUY)のみ予測をtrade_logに記録（Prediction Audit System）
    # WATCH LISTは「様子見」であり売買コールではないため、勝率統計を汚染しないよう除外
    if signal in ["BUY", "STRONG BUY"]:
        try:
            _save_prediction(symbol, signal, current_price, dca_stop, kelly_pct,
                             reasons, signal_type=signal_type)
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

    results = []
    for symbol in SCAN_SYMBOLS:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '4h', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            rsi_series = ta_lib.rsi(df['close'], length=14)
            current_rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and pd.notna(rsi_series.iloc[-1]) else 50.0
            current_price = float(df['close'].iloc[-1])

            # 出来高変化率（直近5本 vs 前20本平均）
            recent_vol = df['volume'].iloc[-5:].mean()
            past_vol = df['volume'].iloc[-25:-5].mean()
            vol_change = ((recent_vol / past_vol) - 1) * 100 if past_vol > 0 else 0.0

            # EMA200 トレンド判定
            ema200_s = ta_lib.ema(df['close'], length=200)
            ema200 = float(ema200_s.iloc[-1]) if ema200_s is not None and pd.notna(ema200_s.iloc[-1]) else current_price
            trend = "UP" if current_price > ema200 else "DOWN"

            # シグナル判定
            if current_rsi < 30 and fng_value < 20:
                signal = "STRONG BUY"
            elif current_rsi < 30:
                signal = "BUY"
            elif current_rsi > 70:
                signal = "OVERBOUGHT"
            else:
                signal = "NEUTRAL"

            # スコアリング（低いほど注目: RSI低い＋出来高急増＋恐怖 = 高スコア）
            score = 0
            if current_rsi < 30:
                score += 3
            elif current_rsi < 40:
                score += 1
            elif current_rsi > 70:
                score += 2  # 過熱も注目
            if vol_change > 50:
                score += 2
            elif vol_change > 20:
                score += 1
            if fng_value < 20:
                score += 2

            results.append({
                "symbol": symbol,
                "price": current_price,
                "rsi": current_rsi,
                "vol_change": vol_change,
                "trend": trend,
                "signal": signal,
                "score": score,
            })
            print(f"  ✅ {symbol}: RSI={current_rsi:.1f}, Vol={vol_change:+.1f}%, {signal}")
            time.sleep(0.3)

        except Exception as e:
            print(f"  ⚠️ {symbol} skipped: {e}")

    if not results:
        return "Error: Could not fetch any crypto data."

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
    signal_emoji_map = {"STRONG BUY": "🔥", "BUY": "🟢", "OVERBOUGHT": "🔴", "NEUTRAL": "⚪"}

    table_rows = ""
    for r in results:
        emoji = signal_emoji_map.get(r['signal'], "⚪")
        table_rows += (
            f"| {emoji} {r['symbol']} | ${r['price']:,.2f} | "
            f"{r['rsi']:.1f} | {r['vol_change']:+.1f}% | "
            f"{r['trend']} | {r['signal']} |\n"
        )

    report = (
        f"# 🔭 Market Scan ({today_str})\n\n"
        f"> [!SUMMARY] 市場概況\n"
        f"> Fear & Greed: **{fng_value}** ({fng_class}) | スキャン銘柄数: {len(results)}\n\n"
        f"> [!TIP] 初心者向け：今日の注目ポイント\n"
        f"{overview_callout}\n\n"
        f"## 📊 全銘柄スキャン結果\n"
        f"| 銘柄 | 価格 | RSI | 出来高変化 | トレンド | シグナル |\n"
        f"|---|---|---|---|---|---|\n"
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

def tool_audit_predictions() -> str:
    """
    過去の予測ログ(trade_log.json)を読み込み、
    Binanceの実績価格と照合してWIN/LOSEを判定する。
    LOSEの場合はTavilyでニュース検索し敗因を分析。
    結果をDev_Logs/Audit_Report_{date}.mdに保存する。
    """
    print("[Tool] 🕵️ Auditing past predictions...")

    logs = _load_trade_log()
    open_trades = [t for t in logs if t["status"] == "OPEN"]

    if not open_trades:
        return "評価待ちの予測（OPEN）はありません。"

    exchange = ccxt.binance({'enableRateLimit': True})
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')

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

    for trade in open_trades:
        symbol = trade["symbol"]
        entry_time = datetime.datetime.fromisoformat(trade["timestamp"])
        since_ts = int(entry_time.timestamp() * 1000)

        print(f"  📊 Checking {symbol} (ID: {trade['id']}, entered {trade['timestamp'][:10]})...")

        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '1h', since=since_ts, limit=500)
            if not ohlcv:
                print(f"    ⚠️ No data returned for {symbol}")
                continue
            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        except Exception as e:
            report += f"- ⚠️ {symbol} (ID: {trade['id']}): データ取得失敗 ({e})\n"
            print(f"    ⚠️ Data fetch error: {e}")
            continue

        lowest = float(df['low'].min())
        highest = float(df['high'].max())
        current_close = float(df['close'].iloc[-1])
        hours_elapsed = len(df)

        new_status = "OPEN"
        audit_comment = ""

        # 判定: SLヒット → LOSE, TPヒット → WIN
        if lowest <= trade["stop_loss"]:
            new_status = "LOSE"
            lose_count += 1

            # 敗因分析: Tavily で当時のニュースを検索
            coin_name = symbol.split('/')[0]
            query = f"Why did {coin_name} price drop crypto news {entry_time.strftime('%Y-%m')}"
            try:
                search_res = tavily_client.search(query=query, topic="news", max_results=3)
                news_items = []
                for r in search_res.get('results', []):
                    title = r.get('title', 'No title')
                    url = r.get('url', '')
                    news_items.append(f"[{title}]({url})")
                news_text = "; ".join(news_items) if news_items else "関連ニュース見つからず"
            except Exception as e:
                news_text = f"ニュース検索失敗: {e}"

            loss_pct = ((trade["stop_loss"] - trade["entry_price"]) / trade["entry_price"]) * 100
            audit_comment = f"損切りライン(${trade['stop_loss']:,.2f})に到達。損失: {loss_pct:+.1f}%。敗因: {news_text}"

        elif highest >= trade["target_price"]:
            new_status = "WIN"
            win_count += 1
            gain_pct = ((trade["target_price"] - trade["entry_price"]) / trade["entry_price"]) * 100
            audit_comment = f"利確目標(${trade['target_price']:,.2f})達成。利益: +{gain_pct:.1f}%。予測通りの値動き。"

        # 判定: 21日経過でも未到達 → EXPIRED
        # (fetch_ohlcv 1h×500本 ≒ 20.8日が観測窓の上限。これを超えたOPENは
        #  以降のSL/TPヒットを正しく検出できないため期限切れでクローズする)
        elif (datetime.datetime.now() - entry_time).days >= 21:
            new_status = "EXPIRED"
            expired_count += 1
            audit_comment = (f"エントリーから21日経過してもSL/TP未到達のため期限切れクローズ。"
                             f"観測窓の最終価格: ${current_close:,.2f}（現在値ではない）。勝率統計からは除外。")

        else:
            # まだ決着がついていない
            dist_to_tp = ((trade["target_price"] - current_close) / current_close) * 100
            dist_to_sl = ((current_close - trade["stop_loss"]) / current_close) * 100
            audit_comment = f"未決着 ({hours_elapsed}h経過)。現在値: ${current_close:,.2f} (TP まで {dist_to_tp:+.1f}%, SL まで {dist_to_sl:.1f}%)"

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
            report += (
                f"## {icon} {symbol} — {new_status} (ID: {trade['id']})\n"
                f"| 項目 | 値 |\n|---|---|\n"
                f"| シグナル | {trade['signal']} |\n"
                f"| シグナル種別 | {trade.get('signal_type', 'legacy')} |\n"
                f"| エントリー | ${trade['entry_price']:,.2f} |\n"
                f"| ターゲット | ${trade['target_price']:,.2f} |\n"
                f"| ストップ | ${trade['stop_loss']:,.2f} |\n"
                f"| Kelly% | {trade['kelly_pct']}% |\n"
                f"| 予測日 | {trade['timestamp'][:10]} |\n"
                f"| 判定理由 | {trade.get('reason', 'N/A')[:80]} |\n\n"
                f"**監査結果**: {audit_comment}\n\n"
            )
            print(f"    {icon} {new_status}: {audit_comment[:80]}...")
        else:
            print(f"    ⏳ Still OPEN: {audit_comment[:80]}...")

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
