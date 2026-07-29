# 外部CLI統合 — 4セッション実装計画

ブランチ: `claude/claude-skills-adoption-cost-mu6tcd`（本計画書のみ。実装は各セッションで別ブランチ）

## 背景 — なぜ Skills ではなく CLI ラップなのか

Claude の Agent Skills 導入を検討した結果、**能力の実体は SKILL.md ではなく同梱スクリプト側**にあると判明した
（`last30days` は Python エンジン + yt-dlp + arXiv CLI、`agent-browser` はそもそも CLI）。
SKILL.md を読めるようにしても能力は増えず、スクリプトを実行できるなら SKILL.md は要らない。

一方 ARMS には既に「CLI を subprocess で叩いてツール化する」型が2本走っている:

| 既存ツール | 実体 |
|---|---|
| `tool_get_calendar` / `tool_add_event` | `lib.lib_shared._run_gws_command()` → gws CLI |
| `tool_bitnet` | `subprocess.run()` → llama-cli.exe |

**損益分岐点は約12件**（Skills loader + Bash tool の固定コスト 5〜7人日 vs CLI ラップ 0.5人日/本）。
4件なら CLI ラップが圧倒的に安い。よって本計画は Skills 機構を導入しない。

---

## 共通規約（全セッション）

### 1セッション1機能

CLAUDE.md の AI Instruction Principles に従い、**1セッションで1ツールだけ**入れる。
セッションをまたぐときは新しいチャットを開く。

### 各セッションの成果物（固定）

1. `tools/tool_<name>.py` 新規
2. `lib/tool_registry.py` に1エントリ追加
3. `tests/test_tool_registry.py` の**どちらかの集合に追記**（後述・必須）
4. `tests/test_<name>.py` 新規
5. `py verify_project.py` = ALL SYSTEMS GO
6. `/grill` でアドバーサリアルレビュー
7. コミット

### subprocess の型（4罠を踏まないため）

```python
r = subprocess.run(
    cmd,                       # ← 文字列連結禁止。必ず list
    capture_output=True,
    encoding='utf-8',          # ① LEVEL 1。cp932 コンソールで化ける
    errors='replace',          # ①
    timeout=N,                 # ② 無いとワーカースレッドが無言で止まる
)
return r.stdout if r.returncode == 0 else f"Error: {r.stderr[:200]}"
                               # ③ ツール層の戻り値契約（例外を投げない）
```

**④ `lib/bot_errors._TOOL_FAILURE_RE` の変更は不要。**
`Error:` 形式は pattern 1 (`(?:\[[^\[\]\n]{1,40}\]\s*)?Error(?![^\W\d_])`) が既にカバーしている。
`[docling error]` 形式にする場合も pattern 4 (`\[[^\[\]\n]{1,40}[eE]rror\]`) が拾う。
**新しい形（例: `失敗: ...`）を発明しないこと** — 発明すると engine が失敗を success として
learning_db に記録する（2026-07-27 の実測事故と同型）。

### parallel_safe の分類は必須

`tests/test_tool_registry.py::test_parallel_safe_covers_every_entry` が
`EXPECTED_PARALLEL_SAFE` にも `MUST_NOT_BE_PARALLEL_SAFE` にも載っていないツールを落とす。
**「判断されていない」状態を作れない設計**なので、どちらかに必ず書く。

### テスト冒頭の必須1行

```python
sys.stdout.reconfigure(encoding='utf-8')
```

これが無いと cp932 コンソールで全件通っていても赤くなり、
本物の不合格と文字化けが区別できない（`tests/test_verifier.py` の前例）。

---

# Session 1: Docling（PDF/docx/pptx → Markdown）

## Goal

Vault 内の PDF・Word・PowerPoint を Markdown 化して Vault に保存する。
**現状ゼロの領域**（pypdf すら未導入）なので、入れた分がそのまま純増する。

## なぜ Marker ではなく Docling か

CLAUDE.md に記録の通り VRAM 6GB を bge-m3 (1.16GB)・qwen2.5-coder:7b (4.68GB)・
ComfyUI (`tool_lora_factory`) が既に奪い合っている。Marker は GPU 前提で速度を稼ぐ設計なので、
**入れると競合が三つ巴から四つ巴になる**。Docling は CPU 既定（CUDA は任意）で戦争に参加しない。
表抽出精度も上（F1 88% vs 82%、TableFormer 搭載）、ライセンスも MIT（Marker は GPL-3.0 + 独自 RAIL-M）。

## Steps → Expected Result

1. **事前ウォームアップ**（実装前に手動）
   `docling <sample.pdf>` を1回実行 → 初回のレイアウト/テーブルモデル取得を済ませる
   → ツールの timeout を初回DL込みで見積もらなくてよくなる
2. `tools/tool_docling.py` 新規
   - 入力は **`lib_shared.find_file_in_vault()` 経由の Vault 内ファイルのみ**
   - 出力は Vault に保存し、**パス + 先頭N字 + ページ数**を返す
   → 任意パス読み取りを持ち込まない / 50ページPDFで context を吹き飛ばさない
3. `lib/tool_registry.py` に `tool_convert_document` を追加
4. `tests/test_tool_registry.py` の `MUST_NOT_BE_PARALLEL_SAFE` に追記
5. `tests/test_docling.py` 新規
6. `py verify_project.py` → ALL SYSTEMS GO

## 設計判断

### (a) 入力は Vault 内のみ

`find_file_in_vault()` を再利用する。これで「任意パス読み取り」= 前回検討した Tier 3 のリスクを
**閉じたまま**にできる。運用は「ユーザーが PDF を Obsidian に置く → エージェントが変換」で自然。

### (b) 出力は保存 + パス返却（全文を返さない）

全文を戻り値にすると長大な PDF が context を破壊する。
`tool_create_note` に繋ぐ2ステップ計画にしても、本文が一度 Gemini を通るので同じ問題。
→ **ツール内で保存して完結**する。先例は `tool_fetch_daily_news`（収集→保存を1ツールで完結）。

### (c) parallel_safe: 付けない

Vault に書き込むため。`MUST_NOT_BE_PARALLEL_SAFE` へ。

### (d) description の縄張り

PDF を扱う既存ツールは無いので正面衝突は無い。ただし「読み込み」の語で
`tool_read_note` / `tool_fetch_context` と語彙一致しうる。
**非対象を明記する**（CLAUDE.md の T09 教訓 — 守備範囲だけ書くと隣のツールに奪われる）:

> PDF・Word・PowerPoint を Markdown に変換して Vault に保存する。
> .md ファイルの読み込みには使えない（それは tool_read_note）

## Verification

- [ ] 日本語 PDF → 文字化けなし（UTF-8 で保存されている）
- [ ] 表を含む PDF → Markdown table として出力される
- [ ] **Vault 外パスを渡す**（`C:\AI_agent\.env` 等）→ `find_file_in_vault` が None → `Error:` 返却
      ← セキュリティ回帰テスト。これが通らない限りマージしない
- [ ] 存在しないファイル → `Error:`
- [ ] 破損 PDF → `Error:`（例外を投げない）
- [ ] `py tests/test_tool_registry.py` → 未分類なし
- [ ] `py verify_project.py` → ALL SYSTEMS GO

## Rollback

`lib/tool_registry.py` の1エントリを削除する → planner の選択肢から消え、計画に現れなくなる。
`tools/tool_docling.py` は残しても無害（誰も呼ばない）。

---

# Session 2: DuckDB（ログ分析）

## Goal

`logs/skill_observations.jsonl`・`trade_log.json`・`logs/eval/*.json`・learning_db を
SQL で集計できるようにし、既存の手書き集計を置換する。

## ⚠️ 最大の設計論点 — 生 SQL は Tier 3 と同等のリスク

**前回の説明を訂正する。「DuckDB は読み取りだから安全」は誤り。**
DuckDB は任意パスを読み書きできる:

```sql
SELECT * FROM read_csv('C:\AI_agent\.env');   -- 秘密の流出
COPY (SELECT 1) TO 'C:\...\anything.txt';     -- 任意書き込み
INSTALL httpfs; LOAD httpfs;                  -- ネットワーク到達
```

生 SQL を Gemini に書かせることは、**Bash ツールを渡すのとほぼ同じ**。

### → v1 は名前付きクエリのみ

エージェントは `query_name` とパラメータを選ぶ。SQL は書かせない。

```python
QUERIES = {
    "tool_failure_rate":  "...",  # get_failure_summary() の置換
    "execution_outcomes": "...",  # learning_db の outcome 別集計
    "trade_performance":  "...",  # signal_type 別 勝率・期待値R
    "eval_history":       "...",  # logs/eval/*.json の推移
}

def tool_query_logs(query_name: str, days: int = 30) -> str:
```

これは目的（既存の手書き集計を置換）と完全に一致する。**既存の集計こそが名前付きクエリだから。**
生 SQL が必要になったら v2 で改めて設計する。

## Steps → Expected Result

1. `tools/tool_log_query.py` 新規（`QUERIES` 辞書 + whitelist 検証）
2. `lib/tool_registry.py` に `tool_query_logs` 追加
3. `tests/test_tool_registry.py` の `EXPECTED_PARALLEL_SAFE` に追記
4. `tests/test_log_query.py` 新規
5. `tools/tool_skill_observer.get_failure_summary()` は**残す**
   → 置換ではなく並走。出力一致を検証に使うため（下記）

## 設計判断

### (a) parallel_safe: 付けてよい（ただし条件付き）

読み取り専用の SELECT で副作用が無い。ただし
**`skill_observations.jsonl` は他スレッドが追記中のファイル**である。
追記途中の行を読むと `read_json_auto` がパースエラーになりうる。
→ `read_json_auto(..., ignore_errors=true)` を指定して縮退させる。
この判断理由を `lib/tool_registry.py` のエントリ横にコメントで残す。

### (b) 既存関数を消さない

`get_failure_summary()` を残すのは、**出力一致を回帰テストの正解として使える**から。
両方が同じ数字を出すことを確認してから、次のセッション以降で置換を判断する。

## Verification

- [ ] **`tool_query_logs("tool_failure_rate")` の出力が `get_failure_summary()` と一致する**
      ← 正解が既に存在するので、回帰の判定に解釈が要らない。この計画で最も強い検証
- [ ] 未登録の `query_name` → `Error:`（SQL 注入経路が無いことの確認）
- [ ] `query_name` に SQL 断片を渡す → `Error:`（文字列連結していないことの確認）
- [ ] `logs/` が空 → 「0件」と正しく報告（`Error:` にしない。空は失敗ではない）
- [ ] 追記途中の壊れた JSONL 行が混ざっても落ちない（`ignore_errors`）
- [ ] `py verify_project.py` → ALL SYSTEMS GO

## Rollback

registry の1エントリ削除。`get_failure_summary()` を消していないので既存機能は無傷。

---

# Session 3: gh CLI（読み取り専用）

## Goal

ARMS が自分自身の PR・CI・issue を読めるようにする。
`engine_self_improve.py` と `logs/eval/` で自己改善ループを回しているのに、
**自分の CI が通ったかを見る手段が無い**という空白を埋める。

## Steps → Expected Result

1. `tools/tool_github.py` 新規（サブコマンド whitelist）
2. `lib/tool_registry.py` に追加（**`optional: True`**）
3. `tests/test_tool_registry.py` の `EXPECTED_PARALLEL_SAFE` に追記
4. `tests/test_github.py` 新規
5. `.env` に読み取り専用 PAT (`GH_TOKEN`) を設定

## 設計判断

### (a) サブコマンド whitelist（文字列連結禁止）

```python
_ALLOWED = {
    "pr_list":    ["pr", "list", "--json", "number,title,state,author"],
    "pr_view":    ["pr", "view", "{n}", "--json", "number,title,body,state"],
    "run_list":   ["run", "list", "--limit", "10", "--json", "conclusion,name,createdAt"],
    "issue_list": ["issue", "list", "--json", "number,title,state"],
}
```

**`gh api` は禁止**（任意エンドポイント = 書き込み可能）。
`pr create` / `pr merge` / `issue close` も当然禁止。

### (b) ⚠️ 第三者テキストが Gemini の context に入る

PR 本文・レビューコメント・CI ログは**他人が書いたもの**。
CLAUDE.md に既に2つの該当教訓がある:

> ノート本文を LLM のプロンプトに入れるときは「資料であって指示ではない」と明示する
> 注入対策は「資料」だけでなく「検査対象」にも要る

→ 戻り値を `<github_data>` で囲い、本文中の `</github_data>` をエスケープして区切りを閉じさせない。

**`tools/tool_vault_hybrid._format_rerank_candidates()` と同じ関数を共用する。**
別々に書くと片方だけ直す事故が起きる（CLAUDE.md:「同じ結果を出す入口は必ず1つの関数を共用する」）。
共用できる形になっていなければ、まず `lib/` へ切り出す。

### (c) `optional: True` にする

`gh` 未インストール環境・CI・Linux で registry の構築が落ちないように。
先例は Calendar/Todoist 系（認証情報や依存パッケージ欠落時にスキップ）。

### (d) parallel_safe: 付けてよい

読み取りのみ、`token.json` のような認証ファイル書き換えも無い。

## Verification

- [ ] `gh api` / `pr create` / `pr merge` を渡す → 全て `Error:`（whitelist 回帰テスト）
- [ ] PR 本文に `</github_data>` を含む文字列 → エスケープされる（注入回帰テスト）
- [ ] `gh` 未インストール環境 → registry が落ちず、警告してスキップ
- [ ] 認証切れ → `Error:` で縮退（例外を投げない）
- [ ] `py verify_project.py` → ALL SYSTEMS GO

## Rollback

registry の1エントリ削除。`.env` の `GH_TOKEN` も削除する。

---

# Session 4: last30days（保留中 — 着手前に前提確認）

## Goal

Reddit・Hacker News・Polymarket・GitHub・arXiv を横断して直近30日の議論を要約する。
**無料ソースのみ**で開始する（X・TikTok・Instagram・Perplexity は API キーが要るため v1 では使わない）。

## 実装前の前提確認（満たさなければ中止）

- [ ] `py -3.12 --version` が通る（Docling とは別に Python 3.12+ が PATH に必要）
- [ ] 無料ソースのみの設定で手動実行し、実際に brief が出ることを確認
- [ ] 実行時間を実測する（下記の設計判断の前提になる）

**この確認で止まったら、Python 3.12 の導入は別セッションの作業とする。**

## 設計判断

### (a) ⏱️ 実行時間が最大の論点 — v1 では registry に載せない

複数ソース並列検索 + 合成のパイプラインは分単位かかる。
CLAUDE.md に該当する教訓がある:

> View の timeout は interaction token の寿命（発行から15分）より短くする

`/agent` の計画に乗せてインライン実行すると、Discord のインタラクションが先に切れて
**通知だけが無言で失敗する**。

→ **v1 は `/last30days` 専用コマンド（非同期実行 + 完了通知）または `agent_scheduler` の定時実行**にする。
`lib/tool_registry.py` には**載せない**。planner の計画に乗せるのは v2 で、
実測時間を見てから判断する。

### (b) description の縄張り衝突 — 既存3ツールを同時に見直す

競合する既存ツール:

| ツール | 守備範囲 |
|---|---|
| `tool_web_search` | Tavily 汎用 Web 検索 |
| `tool_xtrend` | X の AI バズ投稿 |
| `tool_future_search` | 未来シナリオ分析（英語ソース + 10視点） |

CLAUDE.md の T09 事故（`tool_update_note` が `tool_sync_to_daily_note` に語彙一致で奪われた）と同型。
**4つ全部の description を同時に見直す。片方だけ直すと逆流する**
（「一方に語を足したら他方の縄張りも明示すること」）。

v2 で registry に載せる際の作業であり、v1（専用コマンド）では発生しない。

### (c) 無料ソース限定を .env で固定

有料キー（`XQUIK_API_KEY` / ScrapeCreators / Perplexity / Brave）は**空のままにする**。
`lib/cost_guard.py` で Gemini を 200/日で守っている運用方針と揃える。

## Verification

- [ ] Python 3.12 前提チェックが機能する（無い環境で `Error:`）
- [ ] 無料ソースのみで brief が生成される
- [ ] timeout 超過時に `Error:` を返す（無言で止まらない）
- [ ] 出力の保存先が Vault 内に収まる（`LAST30DAYS_MEMORY_DIR`）
- [ ] Discord の通知が完了する（インタラクション期限内）

## Rollback

専用コマンドを `commands/` から外す。registry には載せていないので planner への影響は無い。

---

## 実装順序の根拠

| # | ツール | 順序の理由 |
|---|---|---|
| 1 | Docling | **効果の判定に解釈が要らない**（今できないことが明日できる）。1本目に最適 |
| 2 | DuckDB | 正解（`get_failure_summary()`）が既に存在し、出力一致で回帰を判定できる |
| 3 | gh | 注入対策の設計が要るため、1・2 で型が固まってから |
| 4 | last30days | 外部依存（Python 3.12）と実行時間の不確実性が最も大きい。最後 |

**1・2 が「検証が容易な順」なのは意図的。**
型（subprocess ラップ + registry 登録 + parallel_safe 分類）が正しいことを
判定しやすい題材で先に確かめてから、設計判断の重い 3・4 に進む。

## 各セッションの終了条件

以下を全て満たすまでコミットしない:

- [ ] `py verify_project.py` → ALL SYSTEMS GO
- [ ] `py tests/test_tool_registry.py` → 未分類なし
- [ ] 新規テスト全件パス
- [ ] `/grill` の指摘に TODO/FIXME を残していない
- [ ] Verification の全項目にチェックが入っている
