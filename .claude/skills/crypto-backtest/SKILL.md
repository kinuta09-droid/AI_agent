---
name: crypto-backtest
description: 暗号資産シグナル/ゲートロジック (lib/quant_core.py, tools/tool_crypto.py) の変更ルールと既知バグパターン。バックテストゲート、キーワード判定、naive datetime の UTC/JST 混在に関する規約。このファイル群を編集するときは必ず読む。
---

# Crypto Signal Logic: Backtest Gate & Known Bugs

## Backtest Gate (MANDATORY)
- `lib/quant_core.py` / `tools/tool_crypto.py` のシグナル・水準・ゲートロジック変更は
  **before/after バックテスト比較が必須**:
  `py scripts/backtest_crypto.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --days 730 --compare ...`
- 比較表（signal_type別 勝率・期待値R・トレード数）をコミットメッセージに貼る。比較なしはマージ禁止
- バックテストとライブは `lib/quant_core.py` の純関数を共有する。**ロジックのフォーク禁止**
  （ライブ側だけ/バックテスト側だけの判定分岐を作らない）
- 既知の制約: news_risk はバックテストで再現不能（NEUTRAL固定）、評価密度はライブと異なる

## Known Bug Patterns

- **キーワード判定を部分文字列一致で書かない**。`any(w in content for w in ["sec","ban","war",...])` は "sec" が seconds/security/sector に、"war" が warning/toward に、"ban" が banks/abandon に当たる。`tool_crypto` のニュースゲートがこれで、SYSTEMIC_RISK は `_decide_signal` の最優先分岐でテクニカルを無効化する (STAY AWAY / confidence=0) 上に、検索が走るのは `|24h変動| >= 3%` のとき **だけ** = BUY が出る局面に限って起動するという最悪の組み合わせだった。実測でニュース検索が走った9件中7件(78%)が誤爆し、`trade_log.json` が5ヶ月ゼロ件になった。必ず単語境界の正規表現にし、**判定根拠をレポート/ログに併記**する (旧実装は根拠を一切残さず、だから5ヶ月気付けなかった)。おまけ: 同じ罠を「関連性ゲート」側で作り直しかけた ("eth" が whether/together/method に一致)。**フィルタを直すときは、そのフィルタを守るために足したゲートも同じ基準で書く**
- **naive datetime を跨いで比較しない**。`trade_log` の timestamp は `datetime.now().isoformat()` = ローカル(JST) naive、ccxt の足は epoch ms → `pd.to_datetime(unit='ms')` = **UTC naive**。見た目はどちらも tz なしなので比較が通ってしまい、`_resolve_trade` がエントリー直後9時間の足を「entry より前」として捨てていた (その間に SL/TP へ到達したトレードを取りこぼす)。旧監査は窓全体の `min/max` で時刻を見ていなかったため、判定を時系列走査に統一した瞬間に顕在化した。`lib/quant_core` 側は全て UTC naive で閉じているので **ライブを UTC に寄せる** (`tool_crypto._to_utc_naive`)。なお `.timestamp()` はローカル naive を正しくエポックに直すので、**fetch に渡す時刻はローカルのまま**にすること (両方 UTC にすると取得開始が9時間ずれる)
