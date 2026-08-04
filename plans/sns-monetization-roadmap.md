# SNS 運用マネタイズ ロードマップ — 累計10万円まで

> 作成: 2026-07-31 / 更新: 2026-08-04 / 以後は master 直接管理
> **目標: 累計10万円（手取り）**。月次の継続収入ではなく、まず一度到達すること。
> 経路: X アカウント開設 → note 有料記事 → SNS運用代行 → BOOTH でツール販売

---

## 進捗表 — **中断・再開はここを見る**

各 Phase を終えたら**この表を更新してコミットする**。次のセッションはこの表だけ読めば
どこから再開すればよいか分かる（会話履歴に依存しない）。
状態は `未着手` / `進行中` / `完了` / `見送り(理由)` のいずれかで書く。

| # | 内容 | 状態 | 実施日 | 備考 / 実測値 |
|---|---|---|---|---|
| 0-1 | ニッチ・ペルソナ確定 + X アカウント開設 | 未着手 | | **AI に丸投げ不可。全 Phase の前提** |
| 0-2 | Agent Reach 実機導入 | **完了** | 2026-08-04 | STEP 1〜5すべて完了。X(twitter-cli, cookie認証)・Reddit・Instagram・Facebook(profileのみ精度粗い既知の制約)の4プラットフォームで実機検証済み。TikTokは無料CLIが実質存在しない(唯一の候補が2022年から更新停止)ため保留 |
| 0-3 | docling Phase 0 の Go/No-Go 判定 | **完了 (Go)** | 2026-08-01 | 想定より前進: Phase 0〜3全体が完了し[PR #42](https://github.com/kinuta09-droid/AI_agent/pull/42)でmasterへ統合済み。`tool_paper_radar.py`/`tool_paper_digest.py`もmaster上に存在。**ただしSNS投稿導線(Phase1)への接続はまだ無い** |
| 0-4 | X Developer 登録 + 従量課金の支払い設定 | **進行中** | 2026-08-04 | 申請承認・プロジェクト作成(`hiyumetaphor`)・支払い方法登録・OAuth1.0aキー発行(Read and Write)・`.env`設定まで完了。**$500無料クレジットが反映されない既知の不具合**でバウチャー反映待ち中断 — 完了条件(実投稿+課金確認)は未達。Freeプランは廃止済みで想定コスト月$2〜4 |
| 1-1 | `tools/tool_x_post.py`（X API v2 投稿） | **進行中** | 2026-08-04 | `tools/tool_twitter_post.py`として実装済み(`tool_post_tweet(text)`、通常実行はドライラン=課金なし、`--live`で実投稿)。**git未追跡・tool_registry未登録**。実投稿テストはバウチャー反映待ち |
| 1-2 | 投稿コストガード（`lib/cost_guard.py` に日次カウンタ追加） | 未着手 | | 既存の Gemini カウンタ機構を流用 |
| 1-3 | Discord 承認キュー（下書き → ボタンで投稿/却下/修正） | 未着手 | | skill `discord-bot-patterns`（View 永続化） |
| 1-4 | べき等性（再送で二重投稿・二重課金しない） | 未着手 | | |
| 1-5 | 実績回収（impression/like を engagement_log へ） | 未着手 | | 既存 `TWITTER_API_IO_KEY`（読み取り専用）を使う |
| 2-0 | 毎日投稿の開始 | 未着手 | | ここから最低7週間 |
| 2-1 | 週次「伸びた型 / 死んだ型」レポート | 未着手 | | 後の**営業資料そのもの**になる |
| 2-G | **判断ゲート: フォロワー500 + 週次インプ1万** | 未着手 | | 未達ならニッチを変える |
| 3-1 | note アカウント開設 + 銀行口座紐付け | 未着手 | | |
| 3-2 | 無料記事5本 → 有料記事（¥500〜1,000）月2本 | 未着手 | | |
| 3-G | **判断ゲート: 初月3本** | 未着手 | | 未達なら価格ではなくテーマを疑う |
| 4-1 | 運用代行の営業（自アカ実数 + note + AI納品の差別化） | 未着手 | | |
| 4-2 | ¥30,000/月 × 1〜2件 × 3ヶ月 → **累計10万円到達** | 未着手 | | |
| 5-1 | BOOTH でツール販売（代行の実需を見てから） | 未着手 | | **先に作らない** |

---

## Goal

**累計10万円（手取り）に到達する。** 副次的に、以下が常時動いている状態を作る:

1. Discord に下書きが降ってきて、ボタン1つで X に載る
2. 翌日その投稿の impression が Bot から返ってくる
3. 週次で「どの型が伸びたか」が数字で出る

**やらないこと**: note への自動投稿（下記「外部条件」参照）、非公式 API の利用、
Agent Reach を収益経路の必須依存にすること。

---

## 外部条件（2026-07-31 に確認・**設計を決めている事実**）

| 事実 | 影響 |
|---|---|
| **X API の無料枠は 2026年2月に廃止**。新規開発者は従量課金のみ（Basic/Pro も新規申込不可） | 投稿は有料。**投稿単価 $0.015**（≒¥2.3）・月額最低額なし |
| **リンクを含む投稿だけ $0.20**（≒¥30）と 13倍 | → **本文に URL を入れない。導線は固定ポストとプロフィールに逃がす** |
| **note に公式 API は無い**（公開予定も未定）。自動投稿記事はすべて非公式の Cookie ベース | **非公式 API は使わない。** BAN されたら収益源そのものが消える |
| note 有料記事の手取りは**約85.5%**（決済5% → 残額にプラットフォーム10%）+ 振込ごと¥270 | **手取り10万円 = 売上約11.7万円** |

出典:
- <https://postproxy.dev/blog/x-api-pricing-2026/>
- <https://www.getxapi.com/twitter-api-pricing>
- <https://note.com/jibun_updating/n/nd2eb96ca6255>
- <https://hanapapa-side-business.com/note-monetization/>

### 運転コスト見込み

| 項目 | 月額 |
|---|---|
| X API（本文にリンクを入れない設計・90投稿/月） | ¥200〜1,000 |
| Gemini | 無料枠 + 既存コストガード |
| Tavily / agent-reach / docling | ¥0（既存 or ローカル） |

**初期投資はほぼゼロ。** `.env` の `DAILY_GEMINI_CALL_LIMIT` は 2026-07-28 に一時解除
（200→0）していたが **2026-07-31 に 200 へ復帰済み**（スタック再起動も実施済み）。
Phase 2 で投稿量が増えたら、この上限とのバランスを再度確認すること。

---

## Context — 既にあるもの（作らなくていい）

| 資産 | 場所 | 役割 |
|---|---|---|
| 論文レーダー + 要約 + **X スレッド下書き + note 記事下書き** | **master**（`tool_paper_radar.py`/`tool_paper_digest.py`。2026-08-01 PR #42 マージ済。**SNS投稿導線への接続はまだ無い**） | コンテンツ生産ライン本体 |
| docling 変換層（PDF→Markdown・サイドカー変換・意味検索接続まで完了） | **master**（`lib/docling_client.py` 他。Phase 0〜3完了、hit@5 +0.059で実証済み） | 一次情報の質での差別化 |
| Agent Reach 導入手順 + STEP1〜5全完了 | `claude/agent-reach-mcp-k91vmv`（未push commit `c650c22` 含む） | X/Reddit/Instagram/Facebook/YouTube の生ネタ取得、全稼働 |
| X投稿ツール（ドライラン確認済・実投稿未検証） | ローカル未追跡 `tools/tool_twitter_post.py` | `.env`にOAuth1.0aキー設定済。tool_registry未登録 |
| XTrend / Curator / FutureSearch / Vault RAG | master | ネタの多様化 |
| Engagement Log Phase 1（354件収集済） | master + `claude/multi-agent-rag-monetization-m1a7mx` | 反応の計測基盤 |
| スケジューラ / Discord View 基盤 / コストガード / Verifier | master | 運用の自動化と品質ゲート |

**つまり技術側は約8割できている。決定的に欠けているのは「投稿する手段」と「伸びたかを測る手段」。**

---

## 数字の現実 — なぜこの順序なのか

| 経路 | 単価（手取り） | 累計10万に必要な数 | 判定 |
|---|---|---|---|
| note ¥500 記事 | ¥427 | 234本 | 非現実的 |
| note ¥1,000 記事 | ¥855 | **117本** | ゼロスタートでは非現実的 |
| **運用代行 ¥30,000/月** | ¥30,000 | **1件 × 3〜4ヶ月** | **最短経路** |
| BOOTH ¥5,000 ツール | 約¥4,500 | 22本 | 代行の後 |

→ **note 有料記事は収益源ではなく「代行を受注するための実績証明」として位置づける。**
Phase 3 を飛ばして Phase 4 に行けないのは、営業材料が無くなるから。

---

## Phase 0 — 土台（Week 1・収益ゼロだが、飛ばすと後が全部崩れる）

### 0-1. ニッチ・ペルソナ確定 + X アカウント開設 ← **AI に丸投げできない**

推奨テーマ: **「個人が AI エージェントを自作して SNS 運用する実況」**。
理由は差別化可能な一次情報を既に持っているため — ARMS の開発ログ（`Dev_Logs`）、
`learning_db`、CLAUDE.md の Known Bug Patterns は他人が真似できない。

**完了条件**: アカウント名・プロフィール文・固定ポストが確定して公開されている。

### 0-2. Agent Reach 実機導入 — ✅ 完了 (2026-08-04)
`plans/agent-reach-setup.md`（ブランチ `claude/agent-reach-mcp-k91vmv`、実機作業はcommit
`c650c22`・**まだ未push**）の STEP 1〜5すべて完了。

**実機で判明した計画からの差分** (`docs/agent-reach.md` に反映済み):
- `skill --install` の書き込み先は `~/.claude/skills/` ではなく `~/.agents/skills/`
- GitHub は `gh` CLI 導入・認証済みのため doctor の `[!]` 表示は実害なし
- YouTube は STEP 4-a (js-runtimes 設定) 後、`yt-dlp --dump-json` で実データ取得を確認
- Twitter/X は `--from-browser` 自動抽出が未対応のため Cookie-Editor での手動エクスポートで対応
- **STEP5完了時に、当初想定のXだけでなくReddit/Instagram/Facebookも合わせてセットアップ**
  (OpenCLI+Chrome拡張連携)。Facebookの`profile`は解析精度が粗い既知の制約あり
- TikTokは無料の専用CLIが実質存在しない(唯一の候補`tiktok-scraper`が2022年から更新停止)ため保留。
  代替としてReddit経由(r/TikTokCringe等)の間接把握は可能だが「数日遅れの厳選済みトレンド」であり
  リアルタイム性は無い

> 🔐 **cookie は情報収集専用サブアカウントのものを使う。** 収益アカウントの cookie を
> ローカルツールに渡さない（BAN = 収益源の消滅）。

完了条件どおり `agent-reach doctor` は自動検証しない仕様のため、各プラットフォームで
実コマンドを叩いて動作確認済み(詳細はセッション「ソーシャルメディアアカウント準備」)。

### 0-3. docling Phase 0 の Go/No-Go 判定 — ✅ 完了 (2026-08-01)
`plans/docling-integration.md`（[PR #42](https://github.com/kinuta09-droid/AI_agent/pull/42) → master。以後は master 上のファイルを参照すればよい）。

判定は Go。Phase 0 だけでなく Phase 1〜3（変換層の一般化・`tools/tool_document.py` +
Discord `/doc`・サイドカー変換 + 意味検索接続）まで完了し、Vault 全件再構築後の
検索評価は hit@5 +0.059。詳細は memory `docling-integration-progress.md`。

**残っている差分**: 論文レーダー→docling要約→SNS下書き (`tool_paper_digest.py`) 自体は
master にあるが、**Discord 承認キュー（Phase 1-3）や X 投稿（Phase 1-1）にはまだ
繋がっていない**。ここを繋ぐのは本ロードマップの Phase 1 の仕事のまま変わらない。

### 0-4. X Developer 登録 + 従量課金の支払い設定 — 進行中 (2026-08-04)
Free プランは廃止済みで Pay Per Use 一択(想定コスト月$2〜4程度)なのは外部条件のとおり実測確認済み。
アプリ`hiyumetaphor`を作成し、申請承認・支払い方法登録・OAuth1.0aキー(Consumer Key/Secret,
Access Token/Secret、Read and Write権限)発行・`.env`設定まで完了。

**中断理由**: 新規プロジェクトに付与されるはずの$500無料クレジットが、カード登録後もダッシュボードに
反映されない(X公式コミュニティに同様の報告多数の既知不具合)。反映を待って実投稿テストする方針で
一時中断。クレジット無しでも月数百円程度の想定なので、待たずに課金前提で進める選択肢もある。

**完了条件**: テスト投稿1本が API 経由で通り、課金が想定どおり（$0.015）計上される（未達）。

### 1-1. `tools/tool_x_post.py` — 進行中 (2026-08-04)
`tools/tool_twitter_post.py` として実装済み（ロードマップ想定のファイル名とは異なる）。
`tool_post_tweet(text)` を実装し、通常実行は認証確認のみのドライラン(課金なし)、
`--live` フラグで実投稿。ドライランでの認証情報読み込みは確認済み。

**残作業**: (a) 実投稿(`--live`)のテスト — 0-4のバウチャー反映待ちで未実施、
(b) **git未追跡・`lib/tool_registry.py`未登録** — commit・レジストリ登録をしないと
Discord/エージェント経路からは呼べない、(c) 本文にURLを含めない設計（$0.20回避）に
なっているかの確認

---

## Phase 1 — 投稿の実行系（Week 2-3・**唯一のまとまった新規実装**）

> **1 session, 1 feature**: Phase 0 のマージ後に着手する。1-1〜1-5 も別セッションに分ける。

### 1-1. `tools/tool_x_post.py`
- X API v2 / OAuth。**戻り値契約はこのリポジトリの規約に従う** — 例外を投げず
  `return f"Error: {e}"`（`lib.bot_errors._TOOL_FAILURE_RE` が拾える形にする）
- 本文に URL を含めない（$0.20 回避）。導線は固定ポストとプロフィール

### 1-2. 投稿コストガード
`lib/cost_guard.py` の既存 Gemini 日次カウンタと同じ形で投稿カウンタを足す。
**カウンタのパスは `lib/paths.py` 経由**（絶対パス直書き禁止・`check_hardcoded_abs_paths()` が落とす）。

### 1-3. Discord 承認キュー ← **AI エージェント運用の中核**
下書きを Bot が出す → ボタンで「投稿 / 却下 / 修正指示」。
`commands/bot_views.py` + skill `discord-bot-patterns`（Bot 再起動をまたぐ View 永続化）。

- ❌ `except Exception as e: return f"Error: {e}"` と書かない → `lib.bot_errors.log_command_error()`
- ❌ 見出しに `✅` をハードコードしない → `is_failure_result()` / `is_partial_result()` で判定

### 1-4. べき等性
再送・再起動で二重投稿・二重課金しないよう、投稿済み ID をローカルに記録して照合する。
**X 側の重複検知に頼らない**（ネットワーク再送では通ってしまう）。

### 1-5. 実績回収
既存の `TWITTER_API_IO_KEY`（読み取り専用）で自分の投稿の impression / like / RT を回収し
`lib/engagement_log.py` へ流す。`claude/multi-agent-rag-monetization-m1a7mx` の
`plans/engagement-phase2-prep.md` と重なるので、着手前に読む。

### Verification（Phase 1 の完了条件）
- [ ] Discord に下書きが降ってきて、ボタン1つで X に載る
- [ ] 翌日その投稿の impression が Bot から返ってくる
- [ ] 同じ下書きのボタンを2回押しても投稿は1本（べき等性）
- [ ] 想定外の連投が起きても日次カウンタで止まる
- [ ] `py verify_project.py` が ALL SYSTEMS GO

---

## Phase 2 — 無料運用で「型」を探す（Week 3-10・**最も長く、最も飛ばしたくなる**）

- 毎日 1〜3 投稿。ネタ = 論文 digest / XTrend / **ARMS 開発ログ**
- 毎週「伸びた型 / 死んだ型」を engagement_log で振り返る
- この週次レポートが、そのまま Phase 4 の**営業資料**になる

### 判断ゲート 2-G（Week 10）
**フォロワー500 + 週次インプレッション1万** に届かなければ、**続けずにニッチを変える。**
到達していれば Phase 3 へ。

---

## Phase 3 — note で初売上（Week 8-14）

- 無料記事5本 → 有料 ¥500〜1,000 を月2本
- 売り物は「AI エージェントで SNS 運用を自動化した実装と実測値」。
  **Phase 2 の計測データそのものが商品になる**
- **投稿は手動**（外部条件のとおり非公式 API は使わない）。下書き生成 → Obsidian 保存まで自動

### 判断ゲート 3-G
初月で3本売れなければ、**価格ではなくテーマを疑う。**

---

## Phase 4 — 運用代行で10万到達（Month 4-6）

- 営業材料 = 自アカウントの実数 + note 記事 + 「AI エージェントで納品する」差別化
- ¥30,000/月 × 1〜2件を3ヶ月 → **累計10万円到達**
- ここで `tool_paper_digest` の「投稿は人間確認を挟む」設計が効く
  （誤情報を客の看板で出す事故を防ぐ）

---

## Phase 5 — BOOTH（Month 6+）

代行で見えた「他人も欲しがる部分」だけを切り出して ¥3,000〜10,000 で販売。
**先に作らない** — 代行の実需を見てから作るほうが売れる。

---

## 法務・規約チェックリスト（見落とすと後で全部止まる）

- [ ] **特定商取引法の表示** — note / BOOTH で有料販売する時点で義務。
      BOOTH は「請求があれば遅滞なく開示」運用が可能だが、事前に方針を決める
- [ ] **ステマ規制（景表法・2023年10月〜）** — 運用代行案件では「PR」表記が必須
- [ ] **著作権** — 論文の**要約は可、図表の転載は不可**。引用ルールをテンプレートに埋め込む
- [ ] **X の自動化ポリシー** — API 経由の投稿は正規。ただし重複コンテンツの大量投稿はスパム判定
- [ ] **税** — 累計10万円なら雑所得の範囲。**詳細は税務署 / 税理士に確認すること**
      （このドキュメントは税務助言ではない）

---

## リスクと、それに対する設計

| リスク | 対策 |
|---|---|
| **Phase 2 が本番** — 実装（Phase 0/1）は自分にとって一番簡単な部分。難所は「毎日投稿して反応を見る7週間」で、ここは自動化できない | 判断ゲート 2-G を数値で置いた。惰性で続けない/やめないための線 |
| **収集パイプライン構築が目的化し、ナレッジ化・投稿が後回しになる** （docling 計画の指摘がそのまま当てはまる） | Phase 1 の完了条件を「実装できた」ではなく「投稿が載って翌日数字が返る」に置いた |
| **Agent Reach は壊れやすい** — 上流 CLI の一斉停止が 2026年3月に実績あり | 収益経路の**必須依存にしない**。落ちても投稿は止まらない設計にする |
| **論文の解釈を誤ったまま公開すると取り返しがつかない** | 投稿は必ず人間承認を挟む（Phase 1-3）。Verifier で `[根拠なし]` を付ける |
| **X / note の BAN = 収益源の消滅** | 非公式 API を使わない。cookie は情報収集専用サブアカウント |

---

## Rollback

- Phase 0-2 / 0-3: 各計画書の Rollback 節に従う（`pipx uninstall` / インデックス DB の `.bak-` を戻す）
- Phase 1: `tool_x_post.py` を registry から外せば投稿経路が消える。下書き生成は残る
- 課金の停止: X Developer ポータルで従量課金を止める + 投稿カウンタの上限を 0 にする

---

## 関連ドキュメント

| ドキュメント | ブランチ | 関係 |
|---|---|---|
| `plans/agent-reach-setup.md` | `claude/agent-reach-mcp-k91vmv` | Phase 0-2 の手順書 |
| `plans/docling-integration.md` | **master**（完了・PR #42 でマージ済み） | Phase 0-3 の実施記録（完了済み） |
| `plans/engagement-phase2-prep.md` | `claude/multi-agent-rag-monetization-m1a7mx` | Phase 1-5 と重複。着手前に読む |
