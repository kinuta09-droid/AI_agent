# 実機で走らせる実行プラン — 仮想通貨エンジン

> 背景文書 (先に読む必要はない。各 WP から必要箇所を参照する):
> - `plans/crypto-prediction-enhancements.md` — 何を足すか (理論の検討)
> - `plans/crypto-engine-split.md` — どこに置くか (構成の検討)
>
> 本文書は **実機 (`C:\AI_agent`) で実行するための手順書**。
> 作成: 2026-07-30

---

## このセッションの始め方

```
cd C:\AI_agent
git pull origin master
```

Claude Code を起動し、Plan Mode に入って最初にこう指示する:

> `plans/crypto-engine-execution.md` を読んで、Phase -1 から始めて

**CLAUDE.md「1 session, 1 feature」に従い、WP ごとに別セッション・別ブランチにする。**
1つのセッションで WP-A と WP-B を混ぜない。

| WP | ブランチ名 | 依存 |
|---|---|---|
| Phase -1 (計測) | `chore/crypto-baseline-measurement` | なし。**最初に必ず** |
| WP-A (記録の欠測を直す) | `fix/crypto-record-budget-decoupling` | Phase -1 |
| WP-B (バックテストにコスト) | `feat/backtest-costs-benchmark` | Phase -1 |
| WP-C (エンジン分離 Phase 0) | `refactor/crypto-engine-package` | WP-A, WP-B の後を推奨 |

WP-A と WP-B は互いに独立なので順序は自由。**WP-C は最後**
(移動の前後で出力一致を確認したいので、動く部分の変更を先に済ませる)。

---

## ⚠️ この文書がどこまで検証済みか

**背景文書の分析は、`.env` も `logs/` も Obsidian Vault も ccxt も無い
クラウド環境で、コードを読んで書いたもの。** 以下は実機で確認が要る。

| 項目 | 状態 |
|---|---|
| コードの読解に基づく指摘 (欠測の経路・結合関係・行番号) | **検証済み** (静的に確認) |
| Kelly の数値 (0.65→11.9% 等) | **検証済み** (式を実行して確認) |
| バックテスト実測値 -0.01R | **未検証** — `tool_crypto.py:352-353` の記述を引用しただけ |
| `trade_log.json` の実際の中身 | **未確認** — クラウド側に存在しない |
| 欠測が実際に起きているか | **未確認** — Phase -1 で証拠を取る |
| ccxt のファンディング/建玉の取得可否と遡れる期間 | **未検証** — ccxt 未インストール |

→ **Phase -1 は飛ばさない。** 指摘が正しくても、規模が分からないと
どの WP に工数を割くべきか決められない。

---

## Phase -1: 実機でしか取れない計測

### Goal

背景文書の主張を実データで裏付ける (または否定する)。
**この時点ではコードを1行も変更しない。**

### 前提の確認 (先に緑にする)

```
py verify_project.py
```

緑でないなら、**先にそれを直す**。赤い状態から始めると、
後の変更が原因かどうか切り分けられなくなる。

### 計測 1. trade_log の実態

`TRADE_LOG_PATH` = `%OBSIDIAN_VAULT_ROOT%\Dev_Logs\trade_log.json`
(既定 `D:\マイドライブ\Obsidian\Dev_Logs\trade_log.json`)

取るもの:

- 総レコード数
- `record_kind` 別 (signal / observation / 未設定=旧レコード)
- `status` 別 (OPEN / WIN / LOSE / EXPIRED)
- `signal_type` 別の勝敗と勝率
- **日付ごとの signal レコード数** (← 計測2 で使う)

`_get_signal_stats` と同じ集計は `/cryptostats` (`tool_crypto_stats`) で
既に出せるので、まずそれを叩く。日付分布だけは別途集計が要る。

### 計測 2. ★ 欠測の直接証拠

**背景文書の中心的な主張を検証する、最も重要な計測。**

比べるのは2つ:

| ファイル | 見るもの |
|---|---|
| `logs/crypto_sweep_daily.json` | 日次の `count`。上限に張り付いた日を特定 |
| `Dev_Logs\trade_log.json` | 同じ日の `record_kind=signal` レコード数 |

**上限は `DAILY_SWEEP_ANALYSIS_LIMIT` の既定 `6`** (`lib/cost_guard.py:193`)。

`DailyCounter` は当日分しか保持しない (`{"date","count","warned","limit_notified"}`)
実装なので、**過去日の履歴は残っていない可能性が高い**。その場合:

- `limit_notified` が立った日には `notify_discord` で
  「⛔ Crypto Sweep分析が日次上限(6)に到達」が飛んでいるはず
  → **Discord の通知履歴を検索する**のが最も確実な過去の証拠
- 併せて `logs/` に残っているログ (`_record_scan_results` の
  `budget_hit` を含む sweep のサマリ行) を grep する

期待される所見 (背景文書の仮説):

> 相場が大きく下げた日に、sweep の count が 6 で頭打ちになり、
> かつ同じ日の trade_log の signal レコードが 6件以下で止まっている。
> 一方 observation レコードは同じ日に多数記録されている。

**この非対称 (signal は頭打ち / observation は頭打ちでない) が出れば、仮説は確定。**
出なければ WP-A の優先度を下げてよい。

> なお `lib/cost_guard.py:186-189` のコメント自体が
> 「相場全体が同時に売られた日は候補が20銘柄同時に立ちうる」と書いている。
> 上限6は**意図された設計**で、問題は上限そのものではなく
> **記録が上限に巻き込まれていること**。

### 計測 3. trade_log の置き場所のリスク確認

`trade_log.json` は **Google ドライブ同期フォルダ** (`D:\マイドライブ\...`) にあり、
bot / scheduler / watcher の3プロセスが `FileLock` 下で read-modify-write している。

`FileLock` はローカルのプロセス間排他であって、**同期クライアントの書き込みは
制御できない**。確認するもの:

- `Dev_Logs\` に競合コピー (`trade_log (1).json`、
  `trade_log-競合コピー*.json`、`*.gdoc` 等) が無いか
- `trade_log.json` の更新時刻と、直近の sweep 時刻が整合しているか

競合コピーが1つでもあれば、**データセットが静かに壊れている**証拠になる。
その場合は WP-C (エンジンがリポジトリ配下に自前のデータストアを持つ) の
優先度が上がる。

### 計測 4. ccxt の能力確認 (WP 外・将来の判断材料)

```
py -c "import ccxt; e=ccxt.binanceusdm(); print({k:v for k,v in e.has.items() if 'unding' in k or 'penInterest' in k})"
```

取得できたら、実際に何日遡れるかを1銘柄で確認する。
**建玉履歴は Binance が概ね30日しか返さない**はずで、730日のバックテストには
足りない見込み。ここが確定すると背景文書 Tier 2-2 の可否が決まる。

### 計測 5. バックテストのベースライン保存

```
py scripts\backtest_crypto.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --days 730 --label baseline-2026-07-30
```

`logs/backtest/` の出力を**そのままコミットするか、別途保管する**。
以降すべての WP の before/after 比較の基準になる。

- ここで得た期待値が `tool_crypto.py:352-353` の -0.01R と一致するかを確認する
  (乖離していたら、その記述自体が古い = 背景文書の前提が1つ崩れる)

### Verification

- [ ] `py verify_project.py` が緑
- [ ] trade_log の集計結果を `plans/` か `logs/` に**書き出して残す**
      (次のセッションが再計測せずに済むように)
- [ ] 計測2 の結論を明記する: **仮説は確定したか、否定されたか**
- [ ] ベースラインのバックテスト出力を保管した
- [ ] コードは1行も変更していない (`git diff` が空)

### Rollback

計測のみなので不要。

---

## WP-A: BUY 記録を Gemini 予算から切り離す

> **Phase -1 の計測2 で仮説が否定されたら、この WP は不要。** 先に確認すること。

### Goal

`tools/tool_crypto.py:_record_scan_results` で、BUY/STRONG BUY の
**trade_log への記録が `SWEEP_COUNTER` の上限に巻き込まれて捨てられる**のを止める。

記録 (課金ゼロ) と フル分析 (Gemini + Tavily = 有料) を分離する。

### 現状のコード

```python
if signal in ("BUY", "STRONG BUY"):
    if _has_open_record(logs, symbol, RECORD_KIND_SIGNAL):
        skipped.append(symbol); continue
    try:
        SWEEP_COUNTER.check_and_increment()
    except GeminiBudgetExceeded:
        budget_hit = True
        continue          # ← 記録ごと捨てられる
    result = tool_analyze_crypto(symbol)   # 記録はこの中で行われる
```

記録が `tool_analyze_crypto` の内部でしか行われないため、
予算チェックが記録より手前にあると記録も落ちる。

### Steps

1. **記録を先に、無条件で行う**。observation 経路 (同じ関数の下の分岐) と
   同じ作法で `_resolve_levels` → `_resolve_win_rate` → `_save_prediction`
   を `RECORD_KIND_SIGNAL` で実行する
   → 期待結果: BUY シグナルは予算に関係なく必ず trade_log に載る
2. **その後で** `SWEEP_COUNTER` を試し、通ったときだけ `tool_analyze_crypto`
   を起動する (Obsidian レポート・初心者向け要約・n8n 通知のため)
   → 期待結果: Gemini/Tavily の呼び出し回数は変わらない (上限6を維持)
3. `budget_hit` の意味を「記録が落ちた」から「**レポート生成を見送った**」に変える。
   サマリ文言も直す

### ★ 実装上の注意 (ここを外すと壊れる)

- **`tool_analyze_crypto` 末尾の `_save_prediction` を消さない。**
  `/crypto` コマンドと watcher からの単独呼び出し経路があり、そちらでは
  この関数が唯一の記録経路。sweep から呼ばれたときは `_has_open_record` の
  ガードで自動的にスキップされる (`_save_prediction` の重複チェックは
  ロック内で行われるので競合しない)
- **記録される `entry_price` の意味が変わる。** 現状は「フル分析が再取得した
  時点の価格」、変更後は「sweep が評価した4h足クローズの価格」。
  → これは**バックテストとの整合性が改善する方向**。
  バックテスターは4h足クローズ価格でエントリーする (`backtest_crypto.py:
  entry = ind["price"]`) ので、変更後のほうが母集団の定義が揃う。
  **改善だが挙動変更なので、コミットメッセージに明記する**
- `extra` フィールド (`levels_mode` / `atr_value` / `atr_used_pct` / `sl_atr_mult`)
  は observation 経路が既に同じものを組み立てている。**そちらをコピーする**
  (新しく書き起こすと2箇所に分裂する)

### Verification

**宣言ではなくカウンタの差分で確かめる** (テストのゲーティングで学んだ作法)。

- [ ] `.env` に `DAILY_SWEEP_ANALYSIS_LIMIT=1` を一時設定
      (**`0` は使わない — `lib/cost_guard.py:129` により 0 は「無制限」のキルスイッチ**)
- [ ] BUY 候補が2件以上出る状況で `py -c "from tools.tool_crypto import tool_crypto_sweep; print(tool_crypto_sweep())"` を実行
- [ ] **記録**: trade_log の `record_kind=signal` レコードが **候補数ぶん全件**増える
- [ ] **課金**: `logs/gemini_daily_calls.json` の count 差分が
      **1銘柄ぶんのフル分析に相当する回数だけ**増える (全候補ぶんではない)
- [ ] `logs/crypto_sweep_daily.json` の count が 1 で止まっている
- [ ] 逆方向: `DAILY_SWEEP_ANALYSIS_LIMIT` を既定に戻すと、
      レポートが候補数ぶん生成される
- [ ] **BUY 候補が出ない日は上の実測ができない**ので、単体テストを主にする。
      `tests/test_crypto_sweep.py` に必要な部品が既に揃っている:
      - `FakeCounter(limit=N)` — `SWEEP_COUNTER` の差替え。N 回まで通し、
        以降 `GeminiBudgetExceeded` を送出する
      - `test_record_branches()` — `_record_scan_results` に捏造 `results` を
        渡す既存テスト。実API・実Vaultは使わない
      → **新規ケースを1本足す**: BUY 候補3件 + `FakeCounter(limit=1)` で
      `_record_scan_results` を呼び、**記録3件 / フル分析呼び出し1件**に
      なること。修正前のコードではこれが **記録1件** になるはずなので、
      **先にテストを書いて赤くしてから直す**
- [ ] `py verify_project.py` が緑
- [ ] **Backtest Gate**: `tools/tool_crypto.py` の変更なので
      before/after 比較を実施しコミットメッセージに貼る。
      **判定ロジックは触っていないので差分ゼロが期待値** — 差が出たら
      意図しない変更が混ざっている

### Rollback

`_record_scan_results` 1関数の変更に閉じる。単一コミットを revert すれば戻る。
`.env` の `DAILY_SWEEP_ANALYSIS_LIMIT` を**元に戻し忘れないこと**。

---

## WP-B: バックテストに取引コストとベンチマークを入れる

### Goal

`scripts/backtest_crypto.py` に (a) 手数料・スリッページ、(b) ベンチマーク対比を追加し、
**「そもそも優位性があるのか」を確定させる**。

背景: 背景文書 §Tier 4-1 / 4-4。現状コストが一切入っておらず (grep で不在を確認)、
期待値 -0.01R はその分だけ良く出ている。

### Steps

1. `BTConfig` に `fee_pct` (往復) と `slippage_pct` を追加。既定は
   **Binance 現物 taker 0.1% × 2 = 0.2%** + スリッページ (要検討、0.05%程度から)
2. R の計算にコストを反映する。現状の `R_BY_STATUS = {WIN: 2.0, LOSE: -1.0,
   EXPIRED: 0.0}` は固定値だが、コストは**リスク幅 (entry - stop) に対する比率**
   なので、玉ごとに R への影響が変わる (SL が狭い玉ほどコストの相対負担が重い)
   → 期待結果: SL 幅の小さい玉の期待値が下がり、`atr_floor_pct` の意味が変わる
3. **`EXPIRED = 0R` を実勢に直す**。現状は21日後に建値決済できた仮定。
   期限切れバーの終値で手仕舞う計算に変える
4. ベンチマークを2つ追加:
   - バイ&ホールド (同期間・同銘柄)
   - **ランダムエントリー** (同じ本数・同じ SL/TP/期限ルールで N 回)
   → 期待結果: 「出口ルールが生む R 分布」と「エントリーの寄与」が分離できる
5. 比較表 (`_summary_table`) に上記を出す

### 実装上の注意

- `scripts/backtest_crypto.py` は `lib/quant_core.py` を**変更しない**。
  Backtest Gate の対象は quant_core / tool_crypto なので、この WP は
  厳密には Gate の対象外。**ただし出力の数字が変わるので、
  Phase -1 のベースラインと必ず並べる**
- ランダムエントリーは乱数シードを固定し、**試行回数をレポートに記録する**
  (背景文書 §Tier 4-3 の多重検定の話。ここから記録を始める)
- コスト込みで期待値が明確な負になった場合、**それは失敗ではなく成果**。
  背景文書 §4 Step 0 に「そこで分岐が立つ」と書いたとおりで、
  Tier 1〜3 に工数を積む前に判断できたことになる

### Verification

- [ ] コスト 0 を指定すると、Phase -1 のベースラインと**完全に一致**する
      (= コスト機構の追加が既存経路を壊していない)
- [ ] 既定コストで再実行し、ベースラインとの差分を表にする
- [ ] ランダムエントリー N=100 程度の分布に対し、本戦略の期待値が
      **何パーセンタイルに位置するか**を出す
- [ ] `py verify_project.py` が緑

### Rollback

新規オプションの追加なので、既定値をコスト0にすれば従来と同一。
revert しなくても無効化できる形にしておく。

---

## WP-C: エンジン分離 Phase 0 (移動のみ)

> 背景文書 `plans/crypto-engine-split.md` §5 Phase 0。
> **移動だけ。中身は1行も変えない。**

### Goal

`crypto_engine/` パッケージを新設し、既に `lib_shared` 非依存の3ファイルを移す。

| 移すもの | 現在の依存 |
|---|---|
| `lib/quant_core.py` | pandas, pandas_ta |
| `lib/bt_data.py` | ccxt, pandas, requests, lib.paths |
| `scripts/backtest_crypto.py` | quant_core, bt_data, lib.paths |

### Steps

1. `crypto_engine/` を作り、3ファイルを移動。import パスを更新
2. 旧パスからの再輸出は**置かない** (`from lib.quant_core import ...` を
   残すと、どちらが正なのか分からなくなる)。呼び出し側 6箇所を直接直す:
   `tools/tool_crypto.py` / `tools/tool_crypto_stats.py` /
   `tools/tool_crypto_watcher.py` / `tests/` 配下
3. `verify_project.py` に `check_engine_isolation()` を追加。
   `crypto_engine/` 配下が `lib.lib_shared` / `google.genai` / `tavily` /
   `discord` を import していたら落とす。**純 ast で書く**
   (文字列走査だと、この規約を説明する docstring 自体が違反になる —
   `check_machine_locked_syspath` で踏んだのと同じ罠)

### Verification

- [ ] **バックテストの出力が移動前とバイト単位で一致する。**
      Phase -1 のベースライン (または WP-B 後の出力) と `fc /b` で比較。
      **差分が出たら、移動以外のことをしている**
- [ ] `py verify_project.py` が緑 (import 整合性検査が新パスを通ること)
- [ ] `tests/test_*.py` 全件スイープが Phase -1 と同じ結果
- [ ] `check_engine_isolation()` の**逆方向テスト**:
      `crypto_engine/` に `from lib.lib_shared import client` を書いた
      一時ファイルを置くと**赤くなる**こと (検査が実際に配線されている確認)。
      `tests/test_verify_fast_mode.py` の [3][5] に既存の作法がある
- [ ] CI (`.github/workflows/tests.yml`) の guardian / tests が両方緑

### Rollback

ファイル移動なので `git revert` で戻る。ただし **WP-C は他の WP と
同じ PR に混ぜない** — 混ぜると「移動で壊れたのか、変更で壊れたのか」が
切り分けられなくなる。

---

## 踏んではいけない罠

- **Phase -1 を飛ばさない。** 背景文書の主張はコードの静的読解に基づくもので、
  規模 (何件欠測しているか) は未確認。規模が小さければ WP-A の優先度は下がる
- **`DAILY_SWEEP_ANALYSIS_LIMIT=0` で試さない。** `lib/cost_guard.py:129` により
  **0 は無制限 (キルスイッチ)**。上限の挙動を試したいなら `1` を使う
- **`.env` を書き換えたら必ず戻す。** WP-A の検証で一時的に上限を下げるが、
  戻し忘れると本番の分析が1回/日に制限されたまま気付けない
- **`tool_analyze_crypto` 内の `_save_prediction` を消さない** (WP-A の注意参照)
- **Backtest Gate**: `lib/quant_core.py` (WP-C 後は `crypto_engine/quant_core.py`) と
  `tools/tool_crypto.py` を触ったら before/after 比較が必須。
  比較表をコミットメッセージに貼る。skill `crypto-backtest` を参照
- **naive datetime**: 新しい時系列 (ファンディング・建玉) を足すたびに
  UTC naive / ローカル naive の混在が起きる。`_to_utc_naive` の既存の作法に従う
- **エンコーディング**: 新規 .py は `# -*- coding: utf-8 -*-`、
  ファイル I/O は `encoding='utf-8'` 明示。LEVEL 0 / LEVEL 1 を超えない
  (`os.environ["PYTHONIOENCODING"]` は禁止)
- **ランチャー**: 新しい .bat を書かず `scripts/arms_ctl.py` の既存経路に乗せる
  (skill `windows-encoding`)
- **1 session, 1 feature**: WP をまたがない。混ぜると Backtest Gate の
  before/after が何の差分なのか読めなくなる

---

## 全体の完了条件

- [ ] Phase -1 の計測結果が文書として残っている (次のセッションが再計測しない)
- [ ] 計測2 の結論 (欠測仮説の確定 / 否定) が明記されている
- [ ] WP-A: 予算上限を下げても signal レコードが落ちないことを**カウンタ差分で**確認
- [ ] WP-B: コスト込みの期待値が出ており、ランダムエントリー対比の位置が分かる
- [ ] WP-C: バックテスト出力がバイト単位で不変、`check_engine_isolation()` が
      逆方向テスト付きで配線されている
- [ ] 各 WP で `py verify_project.py` が緑、CI が緑

## この先 (本文書の範囲外)

WP-C まで終わったら、背景文書 `plans/crypto-engine-split.md` §5 の
Phase 1 以降 (`tool_analyze_crypto` の分割 → 独立プロセス化 → ダッシュボード) と、
`plans/crypto-prediction-enhancements.md` の Tier 1〜3 に進む。

**その前に、同文書 §6 の「決めるべきこと」4点を確定させること**
(エンジンの UI 範囲 / `news_risk` の扱い / 分割と Tier の順序 /
WP-A を分割前に単独でやるか — 本文書は最後の1つを「やる」と決めた形になっている)。
