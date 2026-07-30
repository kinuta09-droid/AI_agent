# 仮想通貨エンジンを独立アプリとして切り出す — 構成の検討

> 対象: `lib/quant_core.py` / `lib/bt_data.py` / `tools/tool_crypto*.py` /
> `scripts/backtest_crypto.py` / `agent_scheduler.py`
> 前提文書: `plans/crypto-prediction-enhancements.md` (何を足すかの検討)
> 本文書: **どこに置くか**の検討。実装計画ではない。
> 作成: 2026-07-30

---

## 0. 結論

**分割に賛成。** ただし価値の大半は「PCアプリになること」ではなく、
**エンジンの動作が LLM 予算とチャットボットの死活から切り離されること** から来る。
そこを外すと、プロセスが2つになるコストだけ払う形になる。

分割線は **「数字」と「物語」** の間に引く。

| | 独立アプリ (エンジン) | AIエージェント (現行) |
|---|---|---|
| 責務 | 取得・指標・判定・水準・サイズ・記録・監査・バックテスト・学習 | 自然言語の要約・Obsidian保存・Discord配信・対話 |
| 性質 | 決定的・再現可能 | 生成的・提示的 |
| 依存 | ccxt / pandas / (将来) sklearn 等 | Gemini / Tavily / discord.py / Vault |
| 判定基準 | **バックテストとライブで同一でなければならないものは、全部こちら** | それ以外 |

最後の行が実質的なルール。既存の「ロジックのフォーク禁止」を、
モジュール境界からプロセス境界へ引き上げたものになる。

---

## 1. 分割を支持する理由

### 1-1. ★ trade_log の記録が Gemini 予算に縛られている (構造的な不具合)

これが最も強い理由で、かつ**単なる設計の好みではなく、統計を壊している**。

`tools/tool_crypto.py:_record_scan_results` の BUY 系の経路:

```python
if signal in ("BUY", "STRONG BUY"):
    if _has_open_record(logs, symbol, RECORD_KIND_SIGNAL):
        skipped.append(symbol); continue
    try:
        SWEEP_COUNTER.check_and_increment()
    except GeminiBudgetExceeded:
        budget_hit = True
        continue          # ← BUY シグナルが trade_log に一切残らない
    ...
```

日次の Gemini 予算を使い切ると、**BUY シグナルは記録されずに捨てられる**。
一方 `WATCH LIST` (observation) は課金がないので、同じループの下の分岐で普通に記録される。

つまり:

- 記録が落ちるのは **`record_kind=signal` のレコードだけ**
- それは `_get_signal_stats` → `_resolve_win_rate` → Kelly に渡る**唯一の母集団**
- そして予算を先に使い切るのは **BUY 候補が多い日** = 市場全体が売られた日
  = `panic_reversal` が最も多く成立する日

結果、**勝率統計から、最も情報量の多い局面のサンプルが系統的に抜ける**。
欠測はランダムではなく、シグナル種別と市場局面に相関している。
`plans/crypto-prediction-enhancements.md` で「L4 の確信度に根拠がない」と書いたが、
根拠にしようとしている実測値そのものが、この経路で偏っている。

しかもログには `budget_hit` として集計行に出るだけで、
**どの銘柄のどのシグナルが捨てられたかは残らない**。
このリポジトリが繰り返し潰してきた「無言の死」と同じ形。

> **なぜ分割で直るか**: 評価も記録も Binance 公開API のみで課金ゼロ。
> 有料なのは「人間向けの文章を書く」部分だけ。
> エンジンとエージェントを分ければ、**記録は無条件・無制限**になり、
> Gemini は人が `/crypto` と打ったときだけ動く。
> 予算上限が観測データに影響する経路そのものが消える。

補足: これは分割せずに `_record_scan_results` を直すことでも修正できる
(記録を先に済ませ、フル分析は後追いにする)。ただし**同じ形の結合が
`tool_crypto_watcher` の `WATCHER_COUNTER` にもある**ので、
個別に潰すより境界を引いたほうが再発しない。

### 1-2. クオンツの稼働要件と、チャットボットのそれが違う

`agent_scheduler.py` には現在 **18個のジョブ**が同居している:

```
Daily Note / Daily Sync / X Trend / Morning Briefing / Auto Deep-Dive /
Daily News / BTC Analysis / Crypto Market Scan / Crypto Sweep ×6 /
Crypto Audit / Weekly Crypto Report / Crypto Event Watcher /
Engagement Snapshot / Performance Dashboard / User Memory Distillation /
Vault Semantic Index / Activity Index / Weekly Archive
```

このうち crypto は6系統。残りは Vault・ニュース・SNS・Discord 系で、
**crypto とは何の関係もないのに同じプロセスの生死を共有している**。

Crypto Sweep は UTC の4h足確定直後 (JST 01:05/05:05/09:05/13:05/17:05/21:05) に
打たれている。これは正しい設計だが、**「その時刻にスケジューラが生きていること」に
賭けている**。取りこぼしはそのまま母集団の欠測になる (1-1 と同じ問題の別経路)。

このリポジトリに `lib/pid_guard.py` / `lib/watchdog.py` / skill `process-lifecycle` が
あるのは、この1プロセスを生かし続けるのが実際に難しいから。
**要件が違うものを同じ籠に入れている**のが問題で、
エンジン側は Discord トークンも API キーも対話状態も持たないぶん、
単体では現行スケジューラよりずっと落ちにくい。

### 1-3. `quant_core` の依存制約が消える

`lib/quant_core.py` の冒頭にある制約:

> 依存は pandas / pandas_ta のみ。Gemini/Tavily クライアントや .env 必須の
> lib_shared を import しない (lib_shared は APIキー無しで sys.exit(1) するため、
> オフラインのバックテスターから使えない)

**この制約は「エージェントのリポジトリに同居しているから」だけの理由で存在している。**
前文書で挙げた候補 — メタラベリング (sklearn)、GARCH (arch)、HMM (statsmodels) — は
すべてこの制約に当たり、「係数を定数で埋め込む」といった回避策を検討する必要があった。

独立アプリには避けるべき `lib_shared` が存在しないので、この問題は消滅する。
**前文書の Tier 1〜3 の実装難易度が、分割によって一段下がる。**

### 1-4. Tier 4 (検証基盤) の出力は、そもそも Discord に載らない

信頼度図、エクイティカーブ、ウォークフォワードの期間別成績、PBO、
特徴量重要度 — 前文書で「これが無いと全部幻になる」と書いた層の成果物は、
**本質的に画面で見るもの**。Discord のテキストと Obsidian の markdown 表では扱えない。

ローカルの読み取り専用ダッシュボードが自然な置き場になる。
localhost に閉じるので認証も要らない。

### 1-5. テストの密閉性

このリポジトリのテスト関連の苦労 (実 Gemini 課金、実 Vault 書き込み、
本番カウンタの共有、`ARMS_LIVE_TESTS` ゲート) は、ほぼ全部
「テスト対象が外部サービスと本番状態に触れている」ことに由来する。

エンジンは LLM を呼ばず Vault にも書かないので、**密閉テストが自然に書ける**。
唯一の外部依存である Binance も `lib/bt_data.py` のキャッシュ層が既にあるので、
CSV を置けばネットワークなしで全経路を回せる。

### 1-6. 分割線が既に引かれている

これは移行コストの話。以下は **既に `lib_shared` 非依存**:

| ファイル | 現在の依存 | 移動の性質 |
|---|---|---|
| `lib/quant_core.py` | pandas, pandas_ta のみ | **そのまま移動** |
| `lib/bt_data.py` | ccxt, pandas, requests, lib.paths | **そのまま移動** |
| `scripts/backtest_crypto.py` | quant_core, bt_data, lib.paths | **そのまま移動** |

結合しているのは `tools/tool_crypto.py` 1本だけで、そこでは
「ccxt取得 + 判定 + Gemini要約 + Obsidian保存 + n8n通知 + trade_log」が
1つの関数 (`tool_analyze_crypto`, 300行超) に混ざっている。
**実質の作業はこの関数を2つに割ること**であって、書き直しではない。

---

## 2. 反対材料と、その手当て

正直に書く。以下を潰さないなら分割しないほうがマシ。

### 2-1. 生かし続けるプロセスが2つになる

最大の実コスト。skill `process-lifecycle` が存在するのは、
Windows で1プロセスを確実に生かすのが既に難しいから。

**手当て**: `scripts/arms.bat` には既に `start|stop|restart|status|logs` がある。
2つ目のユニットを足すのは新規機構ではなく既存機構の拡張。
エンジン側は状態を持たない HTTP サービスなので、
**落ちたら再起動すれば済む** (Discord のセッションや interaction token のような
復帰の難しい状態を持たない)。

### 2-2. trade_log の書き手が曖昧になると最悪

現在 `trade_log.json` は bot / scheduler / watcher の3プロセスが
`FileLock` 下で read-modify-write している。ここに4つ目が加わると悪化する。

**手当て**: 分割後は **エンジンが唯一の書き手**。エージェントは HTTP 経由の
読み取りのみ。「両方が書ける」状態を一瞬でも作らない。
これは移行順序の制約になる (§3 Phase 2 で書き手を切り替えるまで、
エンジンに書かせない)。

### 2-3. ロジックのフォークが、CI から見えない場所で起きる

`quant_core` をコピーして両側に置いたら、CLAUDE.md が明示的に禁じている
フォークを、**検査が届かないプロセス境界の向こうで**再現することになる。

**手当て**: **同一リポジトリ・別プロセス**にする (§4)。
加えて `verify_project.py` に検査を1つ足す:

```
check_engine_isolation():
    crypto_engine/ 配下が lib.lib_shared / google.genai / tavily /
    discord を import していたら落とす (純 ast、--fast に乗る)
```

このリポジトリの流儀 — 規約を文章ではなく機械で強制する
(`check_swallowed_exceptions` / `check_machine_locked_syspath` /
`check_hardcoded_abs_paths`) — にそのまま合う。

### 2-4. エンジン停止時にエージェントが黙る

`/crypto` が無言で失敗したり、古いキャッシュを現在値のように見せたら、
このリポジトリの最大の持病の再発。

**手当て**: 接続失敗は `Error: crypto engine に接続できません (127.0.0.1:PORT)` として
`is_failure_result()` に確実に引っかかる形で返す。スケジューラ側は
`notify_discord` で明示的に鳴らす。**フォールバックで計算し直さない**
(それをやると 2-3 のフォークになる)。

### 2-5. Windows のランチャー/エンコーディング周りが増える

新サービス = 新しい .bat / タスクスケジューラ登録 = cp932 と BOM の罠。
skill `windows-encoding` の管轄。既知の地雷原なので、
**新規の .bat を書くのではなく `arms_ctl.py` の既存経路に相乗りさせる**。

---

## 3. 具体的な構成

```
┌─────────────────────── Crypto Engine (独立プロセス) ────────────────────┐
│  常駐スケジューラ                                                        │
│    4h足クローズ毎 : 全20銘柄評価 → シグナル → trade_log 記録 (課金ゼロ)  │
│    15分毎         : ティッカー監視・SL/TP接近検知 (課金ゼロ)             │
│    日次           : 予測監査 (WIN/LOSE/EXPIRED 判定)                     │
│    任意           : バックテスト / ウォークフォワード / モデル再学習     │
│                                                                          │
│  所有する状態: trade_log / OHLCVキャッシュ / 特徴量 / 学習済みモデル     │
│                                                                          │
│  HTTP API (127.0.0.1 のみ・JSON)                                         │
│    GET /analyze?symbol=BTC/USDT   → 指標・シグナル・水準・Kelly          │
│    GET /scan                      → 全銘柄の評価結果                     │
│    GET /watchlist                 → 監視状況・OPEN建玉                   │
│    GET /stats                     → 種別別勝率・キャリブレーション       │
│    GET /positions                 → OPEN一覧                             │
│    POST /backtest                 → 設定を渡して比較実行                 │
│                                                                          │
│  ダッシュボード (localhost・読み取り専用)                                │
│    エクイティカーブ / 信頼度図 / 種別別成績 / ウォークフォワード結果     │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │ JSON over 127.0.0.1
┌──────────────────────────────────┴───────────────────────────────────────┐
│  AI Agent (現行)                                                         │
│    /crypto BTC   → GET /analyze → Gemini で初心者向け要約 → Obsidian +   │
│                    Discord                                               │
│    /scan         → GET /scan    → 整形して Discord                       │
│    /watchlist    → GET /watchlist → 整形して Discord                     │
│    週次レポート  → GET /stats   → Gemini で講評 → Obsidian               │
│    n8n Webhook   → エンジンからの BUY 通知を受けて配信                   │
└──────────────────────────────────────────────────────────────────────────┘
```

### 境界の判定基準 (迷ったときのルール)

> **バックテストとライブで同一の結果でなければならないものは、エンジン側。**

- 指標計算・シグナル判定・SL/TP 水準・ポジションサイズ → エンジン
  (バックテストで再現できなければ Backtest Gate が意味を失う)
- Gemini の要約・Obsidian のファイル名・Discord の絵文字 → エージェント
  (バックテストで再現する必要がそもそもない)

現行の `news_risk` は判定に効くのにバックテスト再現不能という中間物になっている。
**分割を機に「エンジン側に置くが、バックテストでは NEUTRAL 固定と明示する」
という現状の扱いをそのまま持ち込む**か、遮断をやめて特徴量に降格するかを決める。

### API の副次的な利点

現行はツール層が `return f"Error: {e}"` を返し、それを
`lib/bot_errors._TOOL_FAILURE_RE` の正規表現で**推測**している
(`Error:` / `Error (` / `[ERROR]` / `[xtrend error]` … と揺れて、
`\b` が日本語に当たる罠まで踏んだ経緯がある)。

HTTP 境界なら **ステータスコードで確定する**。この境界に関しては
文字列からの推測が要らなくなる。

---

## 4. リポジトリ構成: 同一リポジトリを推奨

| | 同一リポジトリ・別プロセス ★推奨 | 別リポジトリ |
|---|---|---|
| quant_core の単一ソース性 | 保たれる。CI が両側を同時に見る | **跨いだ瞬間に検査不能** |
| バージョン管理 | 不要 | エンジンとクライアントの互換性管理が発生 |
| CI | 現行の `tests.yml` がそのまま使える | 2本立て |
| 得られるもの | プロセス分離・LLM非依存・独自の依存関係 | 上記 + 配布可能性 |

**得たい価値 (プロセス分離・課金の切り離し・依存の自由) は、
同一リポジトリのままで全部得られる。** 別リポジトリで追加で得られるのは
「他人に配れる」ことだけで、現時点でそれは要件ではない。

`crypto_engine/` を新設し、`verify_project.check_engine_isolation()` で
`lib_shared` への依存を機械的に禁じる形にすれば、**同居していながら
分離が保証される**。将来リポジトリを割る判断をしても、
その時点で切り出すのは容易 (依存が既に切れているため)。

---

## 5. 移行順序

**Phase 0 と 1 はリスク低減、価値が出るのは Phase 2 から。**

| Phase | 内容 | 検証 |
|---|---|---|
| **0** | `crypto_engine/` 新設。`quant_core` / `bt_data` / `backtest_crypto` を**移動のみ** (中身に触れない)。`check_engine_isolation()` 追加 | **Backtest Gate: 出力が移動前とバイト単位で一致すること**。差分が出たら移動以外のことをしている |
| **1** | `tool_analyze_crypto` を分割。決定的な部分 (ccxt取得・指標・判定・水準・Kelly・trade_log) をエンジンへ。Gemini要約 / Obsidian / n8n はエージェントに残す。**この時点ではまだ同一プロセス内の import** | 同一入力で分割前後のシグナル・水準・Kelly% が完全一致 |
| **2** | エンジンを**独立プロセス化**。HTTP API + 自前スケジューラ。エージェントをクライアントに切替。`agent_scheduler.py` から crypto ジョブ6系統を削除 | **ここで 1-1 の欠測が消える**。記録件数が Gemini 予算と無相関になったことを、予算上限を意図的に下げて確認する |
| **3** | ダッシュボード (エクイティカーブ・信頼度図・種別別成績) | 目視 |
| **4** | 前文書 Tier 1〜4 の実装。依存制約が外れているので sklearn 等が自由に使える | 各項目の Gate に従う |

Phase 2 の検証方法を具体的に書いたのは意図的。
「予算上限を1に下げても記録件数が変わらない」ことが確認できて初めて、
1-1 の問題が本当に消えたと言える。**宣言ではなくカウンタの差分で確かめる**
— テストのゲーティングで学んだのと同じやり方。

---

## 6. 決めるべきこと

実装に入る前に確定が要るもの。

1. **エンジンの UI をどこまでやるか** — API のみ (Phase 3 を後回し) か、
   最初からダッシュボードを含めるか。スコープが倍近く変わる
2. **`news_risk` の扱い** — エンジンに持ち込む (バックテスト再現不能のまま) か、
   遮断をやめて特徴量に降格するか
3. **Phase 4 との順序** — 分割を先に済ませるか、前文書の Step 0
   (コスト・ベンチマーク導入) だけ現行構成で先にやるか。
   Step 0 は `scripts/backtest_crypto.py` の中だけで完結し、
   Phase 0 で丸ごと移動する対象なので、**どちらの順でも手戻りはない**
4. **1-1 を分割前に単独で直すか** — 統計を汚し続けている問題なので、
   Phase 2 まで待たずに `_record_scan_results` の記録順序を入れ替える
   応急処置をしてもよい (記録を先、フル分析を後追いに)

---

## 7. この分割で「解決しないこと」

期待値の調整のため明記する。

- **予測精度は1ミリも上がらない**。分割は、前文書 Tier 1〜4 を
  やりやすくするための地ならしであって、それ自体は優位性を生まない
- **W4 (ポートフォリオ層の不在) は自動では直らない**。
  エンジン側に置く場所ができるだけで、相関クラスタも合計リスク上限も
  別途実装が要る
- **バックテストの楽観性 (手数料・スリッページ・多重検定) も直らない**。
  これは Tier 4 の作業で、置き場所とは独立
