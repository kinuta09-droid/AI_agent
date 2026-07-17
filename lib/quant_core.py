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
import pandas as pd
import pandas_ta as ta_lib


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
