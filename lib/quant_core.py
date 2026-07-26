# -*- coding: utf-8 -*-
"""
Quant Core — 純関数層 (ネットワーク・lib_shared 非依存)

tools/tool_crypto.py (ライブ分析) と scripts/backtest_crypto.py (バックテスト) が
「完全に同一のロジック」を共有するための分離モジュール。

設計制約:
- 依存は pandas / pandas_ta のみ。Gemini/Tavily クライアントや .env 必須の
  lib_shared を import しない (lib_shared は APIキー無しで sys.exit(1) するため、
  オフラインのバックテスターから使えない)
- I/O・ネットワーク・print以外の副作用を持たない純関数のみを置く
- ここのロジックを変更したら CLAUDE.md の Backtest Gate に従い
  before/after バックテスト比較が必須
"""
import os
import re
import datetime

import pandas as pd
import pandas_ta as ta_lib


def get_float_env(name: str, default: float) -> float:
    """環境変数からfloatを読む。未設定/パース不能は default。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _calculate_kelly(win_rate: float, reward_ratio: float = 2.0, fraction: float = 0.25) -> float:
    """フラクショナル・ケリー基準でポジションサイズ(%)を算出"""
    if win_rate <= 0:
        return 0.0
    q = 1 - win_rate
    kelly_f = (win_rate * reward_ratio - q) / reward_ratio
    return max(kelly_f * fraction * 100, 0.0)


def _detect_regime(df) -> tuple:
    """ADX + ATR% で市場レジームを判定 → (regime, adx, atr_pct, dmp, dmn, atr_value)

    atr_value は生ATR (クオート通貨建て)。ATRベースの動的DCA/SL水準の
    計算 (_compute_levels) に使う。
    """
    adx_s = ta_lib.adx(df['high'], df['low'], df['close'], length=14)
    atr_s = ta_lib.atr(df['high'], df['low'], df['close'], length=14)
    adx = float(adx_s['ADX_14'].iloc[-1]) if adx_s is not None and pd.notna(adx_s['ADX_14'].iloc[-1]) else 20.0
    dmp = float(adx_s['DMP_14'].iloc[-1]) if adx_s is not None and 'DMP_14' in adx_s.columns and pd.notna(adx_s['DMP_14'].iloc[-1]) else 0.0
    dmn = float(adx_s['DMN_14'].iloc[-1]) if adx_s is not None and 'DMN_14' in adx_s.columns and pd.notna(adx_s['DMN_14'].iloc[-1]) else 0.0
    atr_value = float(atr_s.iloc[-1]) if atr_s is not None and pd.notna(atr_s.iloc[-1]) else 0.0
    atr_pct = (atr_value / float(df['close'].iloc[-1])) * 100 if df['close'].iloc[-1] > 0 else 0.0

    if atr_pct > 3.0:
        regime = "VOLATILE"
    elif adx > 25:
        regime = "TRENDING"
    else:
        regime = "SIDEWAYS"
    return regime, adx, atr_pct, dmp, dmn, atr_value


def _detect_liquidity_sweep(df, window: int = 20) -> tuple:
    """SMC: 直近安値をヒゲで割り込み終値で戻すパターン → (bool, sweep_level)"""
    if len(df) < window + 1:
        return False, 0.0
    recent_low = float(df['low'].iloc[-(window+1):-1].min())
    latest = df.iloc[-1]
    is_sweep = (float(latest['low']) < recent_low) and (float(latest['close']) > recent_low)
    return is_sweep, recent_low


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


# ---------------------------------------------------------
# ニュースリスク分類
# ---------------------------------------------------------
# SYSTEMIC_RISK は _decide_signal の最優先分岐でテクニカルを完全無効化する
# (signal="STAY AWAY", confidence=0)。しかも呼び出し側はニュース検索を
# 「24h変動が3%以上のとき」だけ走らせる = BUYが出る局面に限って起動する。
# つまりこの分類の誤爆は「BUYが二度と出ない」に直結する。
#
# 旧実装は `any(w in content for w in ["hack","sec","ban","war",...])` の
# 部分文字列一致だった:
#   "sec"  -> seconds / security / sector / securities   (ほぼ全ての英文記事)
#   "war"  -> warning / toward / warrant
#   "ban"  -> banks / abandon / urban
# 実測 (2026-03〜07 のフル分析34件) では、ニュース検索が走った9件のうち
# 7件(78%)が誤爆で STAY AWAY になり、trade_log が5ヶ月間ゼロ件だった。
# -> 単語境界マッチ + 「その記事が暗号資産の話か」の関連性ゲートで是正する。

# 大文字小文字を無視して判定する深刻語
_RISK_PATTERNS_CI = [
    r"\bhack(s|ed|ing|er|ers)?\b",
    r"\bexploit(s|ed|ing)?\b",
    r"\bbankrupt(cy)?\b",
    r"\bfraud(ulent)?\b",
    r"\bban(s|ned|ning)?\b",
    r"\bwar\b",
    r"\bcollapse[sd]?\b",
    r"\brug[- ]?pull\b",
    r"\bdelist(s|ed|ing)?\b",
    r"\bshut[- ]?down[s]?\b",
    r"\bsec (lawsuit|charges|sues|suing|probe|investigation)\b",
]
# 原文の大文字表記でのみ判定する語 (小文字の "sec" は seconds/section と
# 区別できないため、規制当局を指す "SEC" は大文字でしか拾わない)
_RISK_PATTERNS_CS = [
    r"\bSEC\b",
]
# 一時的な弱材料 (単独では STAY AWAY にならない軽い減点)
_FUD_PATTERNS_CI = [
    r"\brumou?rs?\b",
    r"\bcorrection\b",
    r"\bprofit[- ]taking\b",
    r"\bliquidation(s)?\b",
    r"\bsell[- ]?off\b",
    r"\bpullback\b",
    r"\bfear\b",
]
# 記事が暗号資産市場の話であることを示す語。銘柄名 (BTC/INJ 等) と併せて
# 判定し、どちらも無い記事は採点対象外にする (実害例: 金・銀相場の記事と
# ミームコインのプレセール記事で INJ が STAY AWAY になった 2026-07-26)。
# ここも単語境界で判定する — 部分文字列にすると "eth" が whether/together/method に、
# "btc" 以外でも "coin" が coincidence に当たり、関連性ゲートが素通しになる
# (深刻語側で潰したのと同じ罠を、ゲート側で作り直すことになる)
_CRYPTO_TERM_RE = re.compile(
    r"\b(crypto\w*|bitcoin|btc|ethereum|eth|altcoins?|tokens?|blockchain|"
    r"exchanges?|defi|stablecoins?|web3|coins?)\b", re.IGNORECASE)

_RISK_RE_CI = [re.compile(p, re.IGNORECASE) for p in _RISK_PATTERNS_CI]
_RISK_RE_CS = [re.compile(p) for p in _RISK_PATTERNS_CS]
_FUD_RE_CI = [re.compile(p, re.IGNORECASE) for p in _FUD_PATTERNS_CI]

SYSTEMIC_RISK_THRESHOLD = -2.0


def _is_crypto_related(text_lower: str, coin_name: str) -> bool:
    """記事が対象銘柄または暗号資産市場の話かどうか。"""
    if coin_name and re.search(rf"\b{re.escape(coin_name.lower())}\b", text_lower):
        return True
    return bool(_CRYPTO_TERM_RE.search(text_lower))


def classify_news_risk(articles: list, coin_name: str = "") -> tuple:
    """ニュース検索結果から市場リスクを分類する (純関数)。

    articles: [{"title","url","content"}] (Tavily の results 形式)
    Returns: (summary_lines, risk_level)
      summary_lines: Obsidianレポート用の markdown 行リスト。
                     採点に効いた記事には理由を併記する (誤爆を可視化するため。
                     旧実装は判定根拠を一切残さず、5ヶ月間気付けなかった)
      risk_level: "SYSTEMIC_RISK" | "NORMAL_CORRECTION" | "NEUTRAL"
    """
    summary_lines = []
    score = 0.0

    for art in articles:
        title = art.get("title") or "No title"
        url = art.get("url") or ""
        raw = f"{title}\n{art.get('content') or ''}"
        lowered = raw.lower()

        if not _is_crypto_related(lowered, coin_name):
            summary_lines.append(f"- [{title}]({url}) ⏭️ 対象外(暗号資産の記事ではない)")
            continue

        hit = next((r.pattern for r in _RISK_RE_CI if r.search(raw)), None)
        if hit is None:
            hit = next((r.pattern for r in _RISK_RE_CS if r.search(raw)), None)
        if hit:
            score -= 2
            summary_lines.append(f"- [{title}]({url}) ⚠️ 深刻語ヒット: `{hit}`")
            continue

        fud = next((r.pattern for r in _FUD_RE_CI if r.search(raw)), None)
        if fud:
            score -= 0.5
            summary_lines.append(f"- [{title}]({url}) 🟡 弱材料ヒット: `{fud}`")
        else:
            summary_lines.append(f"- [{title}]({url})")

    if not summary_lines:
        return [], "NEUTRAL"

    if score <= SYSTEMIC_RISK_THRESHOLD:
        risk_level = "SYSTEMIC_RISK"
    elif score < 0:
        risk_level = "NORMAL_CORRECTION"
    else:
        risk_level = "NEUTRAL"
    return summary_lines, risk_level


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
# Phase 3: 指標一括計算 / ATR水準 / 日足ゲート / トレード解決
# ---------------------------------------------------------

def _compute_indicators(df) -> dict:
    """4h足OHLCV DataFrame から判定に必要な全指標を一括計算する。

    ライブ (tool_analyze_crypto) とバックテスターの両方がこれを呼ぶことで
    「同一ロジック」を構造的に保証する。df は変更しない (copyして計算)。
    change_24h は 4h足前提 (6本=24時間前との比較) — live実装と同一。
    EMA200 はデータ不足時 NaN → 現在価格フォールバック → trend="DOWN" に
    倒れる (liveの従来挙動と同一。300本以上渡せば正しく計算される)。
    """
    df = df.copy()
    df['rsi'] = ta_lib.rsi(df['close'], length=14)
    df['ema50'] = ta_lib.ema(df['close'], length=50)
    df['ema200'] = ta_lib.ema(df['close'], length=200)

    latest = df.iloc[-1]
    price = float(latest['close'])
    current_rsi = float(latest['rsi']) if pd.notna(latest['rsi']) else 50.0
    ema50 = float(latest['ema50']) if pd.notna(latest['ema50']) else price
    ema200_valid = pd.notna(latest['ema200'])
    ema200 = float(latest['ema200']) if ema200_valid else price

    regime, adx, atr_pct, dmp, dmn, atr_value = _detect_regime(df)
    is_sweep, sweep_level = _detect_liquidity_sweep(df)
    has_divergence = _check_divergence(df)
    is_falling_knife = (adx > 30) and (dmn > dmp)
    trend = "UP" if price > ema200 else "DOWN"

    price_24h_ago = float(df['close'].iloc[-7]) if len(df) >= 7 else price
    change_24h = ((price - price_24h_ago) / price_24h_ago) * 100 if price_24h_ago else 0.0

    return {
        "price": price,
        "rsi": current_rsi,
        "ema50": ema50,
        "ema200": ema200,
        "ema200_valid": ema200_valid,
        "regime": regime,
        "adx": adx,
        "atr_pct": atr_pct,
        "atr_value": atr_value,
        "dmp": dmp,
        "dmn": dmn,
        "is_sweep": is_sweep,
        "sweep_level": sweep_level,
        "has_divergence": has_divergence,
        "is_falling_knife": is_falling_knife,
        "trend": trend,
        "change_24h": change_24h,
    }


def _compute_levels(price: float, atr_value: float,
                    sl_mult: float = 2.0,
                    dca_mults: tuple = (1.5, 3.0, 4.5),
                    atr_floor_pct: float = 1.0,
                    atr_cap_pct: float = 4.0) -> dict:
    """ATR倍数ベースの動的DCA/SL水準を算出する (固定-5/-10/-15%の置き換え)。

    atr_eff = clamp(atr_value, price×floor%, price×cap%)。
    floor はレンジ相場で Stop が浅くなりすぎる事故 (例: 1×ATR=-0.5%) を、
    cap は高ボラ相場で旧固定%より深くなる事故を防ぐ保険。
    """
    if price <= 0:
        return {"dca_entry1": 0.0, "dca_entry2": 0.0, "dca_entry3": 0.0,
                "dca_stop": 0.0, "atr_eff": 0.0, "atr_used_pct": 0.0,
                "sl_mult": sl_mult, "dca_mults": tuple(dca_mults)}
    floor = price * atr_floor_pct / 100.0
    cap = price * atr_cap_pct / 100.0
    atr_eff = min(max(atr_value, floor), cap)
    return {
        "dca_entry1": price - dca_mults[0] * atr_eff,
        "dca_entry2": price - dca_mults[1] * atr_eff,
        "dca_entry3": price - dca_mults[2] * atr_eff,
        "dca_stop": price - sl_mult * atr_eff,
        "atr_eff": atr_eff,
        "atr_used_pct": (atr_eff / price) * 100.0,
        "sl_mult": sl_mult,
        "dca_mults": tuple(dca_mults),
    }


def _daily_trend(df_1d) -> str:
    """日足終値 vs 日足EMA200 でトレンド判定 → "UP" | "DOWN" | "UNKNOWN"。

    UNKNOWN はデータ不足/取得失敗 (フェイルオープン: ゲートは何もしない)。
    """
    if df_1d is None or len(df_1d) < 200:
        return "UNKNOWN"
    ema200_s = ta_lib.ema(df_1d['close'], length=200)
    if ema200_s is None or pd.isna(ema200_s.iloc[-1]):
        return "UNKNOWN"
    return "UP" if float(df_1d['close'].iloc[-1]) > float(ema200_s.iloc[-1]) else "DOWN"


def _apply_daily_gate(signal: str, daily_trend: str, enabled: bool = True) -> tuple:
    """日足ハードゲート: 日足下降トレンド中は BUY/STRONG BUY を WATCH LIST に降格。

    Returns: (signal, gate_note or None)
    WATCH LIST は n8n通知にも trade_log 記録にも乗らないため、降格 = 完全遮断。
    signal_type は呼び出し側で保持される (WATCH LISTは記録されないので
    勝率統計は汚れない)。UNKNOWN は素通し (フェイルオープン)。
    """
    if not enabled or daily_trend != "DOWN":
        return signal, None
    if signal in ("BUY", "STRONG BUY"):
        note = f"⛔ 日足フィルタ: 1d EMA200 下抜けのため {signal} → WATCH LIST に降格"
        return "WATCH LIST", note
    return signal, None


def _resolve_trade(entry: float, stop: float, target: float,
                   candles, entry_time: datetime.datetime,
                   expiry_days: int = 21) -> tuple:
    """1h足を時系列に走査してトレードの決着を判定する。

    candles: 'timestamp'(datetime64) / 'high' / 'low' 列を持つ DataFrame
    (時系列昇順)。entry_time 以降に開始するバーを評価する (エントリー時刻
    ちょうどに始まるバー = エントリー直後1時間を含む。ライブ監査の
    fetch_ohlcv since=エントリー時刻 と同じ窓)。

    Returns: (status, exit_time, note)
      status: "WIN" | "LOSE" | "EXPIRED" | "OPEN"(データ端まで未決着)

    ライブ監査 (tool_audit_predictions) との判定差異:
    - 監査は観測窓全体の min(low)/max(high) で SL優先判定 (時系列無視の悲観近似)。
      本関数は時系列順に評価するため「TP先着→後日SL」を正しく WIN にできる。
    - 同一バー内で SL/TP 両到達の場合は SL 優先 (悲観バイアス。バー内の
      順序は1hデータでは不明のため、監査と同じ safe 側に倒す)。
    """
    expiry = entry_time + datetime.timedelta(days=expiry_days)
    for row in candles.itertuples():
        ts = row.timestamp
        if ts < entry_time:
            continue
        if ts >= expiry:
            return "EXPIRED", ts, f"{expiry_days}日経過してもSL/TP未到達"
        if float(row.low) <= stop:
            return "LOSE", ts, f"SL ${stop:,.2f} 到達"
        if float(row.high) >= target:
            return "WIN", ts, f"TP ${target:,.2f} 到達"
    return "OPEN", None, "データ端まで未決着"
