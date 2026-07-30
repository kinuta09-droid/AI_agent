# 実機(Windows)での検証・調整・マージ計画

## 役割分担 — どのセッションが何をやるか

| セッション | 環境 | やること |
|---|---|---|
| **このリモートセッション** | Linux サンドボックス | **この計画書を `plans/claude-code-setup-verification.md` にコミット&pushするだけ。** Session 1〜3 は一切実行しない (実機に触れないため物理的に不可能) |
| **実機セッション** | Windows / `C:\AI_agent` | **Session 1〜3 のすべて。** 計測・調整・CI・マージ |

### 実機セッションの始め方
```
cd C:\AI_agent
git fetch origin claude/claude-skills-mcp-setup-hrl3jh
git checkout claude/claude-skills-mcp-setup-hrl3jh
```
その上で Claude Code を起動し、最初にこう指示する:
> `plans/claude-code-setup-verification.md` を読んで、Session 1 から始めて

---

## Context

リモートセッション(Linuxサンドボックス)で、Claude Code の運用基盤を6件整備してブランチ
`claude/claude-skills-mcp-setup-hrl3jh` に push 済み (6コミット / 18ファイル / +660 -119)。

| # | 内容 | 状態 |
|---|---|---|
| 1 | CLAUDE.md を7つの Skill に分割 (46,612→18,542 bytes) | push済・**実機未検証** |
| 2 | PreToolUse hook 3本 (`.claude/hooks/`) | push済・**実機未検証** |
| 3 | Subagent 3体 (`.claude/agents/`) | push済・**実機未検証** |
| 4 | `.mcp.json` (context7 / chrome-devtools) | push済・**実機未検証** |
| 5 | `.github/workflows/tests.yml` | push済・**CIは一度も起動していない** |
| 6 | `settings.local.json` を 69→16件に整理 | push済・**実機未検証** |

**すべて Linux サンドボックス上で書かれ、Windows 実機では一度も動いていない。**
この計画の目的は、実機で「本当に動くか」を計測し、計測結果に基づいて調整し、
CIを緑にしてから master にマージすること。

### 未解決の3つの問い (これが計画の駆動力)

1. **`verify_project.py` は実機で何秒かかるか** — `guard_git_commit.py` hook が
   `git commit` のたびにこれを実行する設計になっている。後述の通り、これは
   **設計上の重大な懸念**があり、計測結果次第で作り直しが必要
2. **Windows 実機でテストは何本落ちるか** — Linux では 67本中38本が失敗したが、
   大半は `msvcrt` (Windows専用モジュール) が無いことが原因。実機なら大きく減るはず。
   この数字が CI の `continue-on-error` を外せるかどうかを決める
3. **Node.js 22+ が入っているか** — `chrome-devtools` MCP の前提条件

### ⚠ 最大のリスク: commit hook のコスト

`verify_project.py` のサブプロセス生成回数を実測した:

| 処理 | 回数 | 備考 |
|---|---|---|
| `py_compile` (構文チェック) | **143** | 1ファイル1プロセス |
| `check_import` (重要モジュール) | **20** | 各々が ccxt/pandas/google.genai を全ロード |
| `check_import` (main.py TOOLS) | **16** | 同上 |
| briefing providers | 1 | |
| **合計** | **約180プロセス** | うち **37回が重い依存スタックを毎回ロード** |

Windows はプロセス生成が Linux より遅く、重いimportは1回2〜4秒かかる。
**`verify_project.py` は実機で 1.5〜3分かかる可能性が高い**。
これが `git commit` のたびに走ると実用に耐えない。

→ **Session 1 で必ず実測し、Session 2 で設計判断する**。数字を見る前に作り直さない。

---

## Session 1: 計測（**一切変更しない**）

> CLAUDE.md「1 session, 1 feature」に従い、計測と変更を別セッションに分ける。
> このセッションはデータ収集のみ。commit も push もしない。

### Steps

1. **ブランチを取り込む**
   ```
   cd C:\AI_agent
   git fetch origin claude/claude-skills-mcp-setup-hrl3jh
   git checkout claude/claude-skills-mcp-setup-hrl3jh
   ```
   → 18ファイルが変更された状態になる

2. **`verify_project.py` のベースラインと所要時間を測る**
   ```powershell
   Measure-Command { py verify_project.py } | Select-Object TotalSeconds
   py verify_project.py
   ```
   → 終了コード0(緑)か、失敗内容と**秒数**を記録

3. **Node.js を確認**
   ```
   node -v
   ```

4. **テストスイートを実機で流す**
   ```powershell
   $fail=@(); $pass=@()
   Get-ChildItem tests/test_*.py | ForEach-Object {
     python $_.FullName *> $null
     if ($LASTEXITCODE -ne 0) { $fail += $_.Name } else { $pass += $_.Name }
   }
   Write-Host "PASS: $($pass.Count)  FAIL: $($fail.Count) / 67"
   $fail
   ```
   → 失敗した**ファイル名の一覧**を記録 (Linux基準値: 29 pass / 38 fail)

5. **Claude Code を再起動して新しい仕掛けの発火を確認**
   `.claude/settings.json` と `.mcp.json` は新規ファイルなので、
   信頼ダイアログと MCP 承認プロンプトが出る。承認後:

   | 対象 | 確認方法 | 期待 |
   |---|---|---|
   | MCP | `claude mcp list` | `context7` `chrome-devtools` が `✔ Connected` |
   | Skills | `/` を打って補完一覧 | 7つ (`crypto-backtest` 等) が並ぶ |
   | Subagents | 「使える subagent を教えて」と質問 | `verifier`/`test-writer`/`bug-pattern-archivist` |
   | Hook (edit) | 「`scripts/tmp_test.bat` に日本語コメントを書いて」と依頼 | **ブロックされる** |
   | Hook (bash) | 「`setx PYTHONIOENCODING utf-8` を実行して」と依頼 | **ブロックされる** |

   ※ hook の確認で作られた一時ファイルがあれば消すこと

### Verification
- [ ] `verify_project.py` の終了コードと**秒数**が記録されている
- [ ] `node -v` の出力が記録されている
- [ ] テストの失敗本数と**ファイル名一覧**が記録されている
- [ ] 上表5項目の発火可否が記録されている
- [ ] `git status` がクリーン (何も変更していないこと)

### Rollback
変更しないので不要。ブランチを離れるだけなら `git checkout master`。

---

## Session 2: 計測結果に基づく調整

> Session 1 の数字を持って開始する。数字が無いまま始めない。

### 判断ゲート A — commit hook の設計

| `verify_project.py` の実測 | 判断 |
|---|---|
| **15秒未満** | そのまま維持。何もしない |
| **15〜45秒** | `guard_git_commit.py` に「前回成功マーカー」を導入。追跡下の `.py` の最終更新時刻がマーカーより古ければスキップし、コード変更後の初回コミットだけコストを払う |
| **45秒超** | **hook を高速版に作り替える**。`verify_project.py` を `--fast` 対応にし、重い `check_import` 系(37プロセス)を除いた静的検査のみ(構文/エンコーディング/未定義名/例外握りつぶし)を hook から呼ぶ。`py_compile` のサブプロセスも `compile()` のインプロセス実行に置き換える。フル検査は CI と手動実行が担当する |

- 対象ファイル: `.claude/hooks/guard_git_commit.py`, 場合により `verify_project.py`
- **緊急脱出**: hook が邪魔で作業が進まないときは `.claude/settings.json` の
  `hooks` ブロックを削除すれば即無効化できる。また hook が捕まえるのは
  **Claude Code の Bash ツールだけ**なので、通常の PowerShell から `git commit`
  すれば常に迂回できる

### 判断ゲート B — chrome-devtools MCP の去就

| `node -v` | 判断 |
|---|---|
| **v22 以上** | そのまま維持 |
| **v22 未満** | Node.js LTS を入れ直すか、`.mcp.json` から `chrome-devtools` を削除 (`context7` は HTTP 接続なので Node 不要・影響なし) |
| **未インストール** | 同上。`chrome-devtools` を削除する方を推奨 (後で Node を入れたら再追加できる) |

### 判断ゲート C — テストの仕分け

Session 1 で失敗したファイルを3分類する:
- **(a) 実機バグ** → 直す (別セッション。ここでは一覧化のみ)
- **(b) 実ネットワーク/実APIキー/外部バイナリが要る手動デバッグスクリプト** →
  CLAUDE.md Architecture が言う "debugging stubs"。CI 対象から外すか、
  Ollama スモークテスト (`tests/test_vault_semantic_smoke.py`) と同じく
  **前提が無ければ SKIP して正常終了**する形に揃える
- **(c) 環境依存 (ディレクトリ列挙順など)** → テスト側を環境非依存に直す

→ この仕分け結果を `.github/workflows/tests.yml` に反映する

### Verification
- [ ] 変更した hook を、Session 1 と同じ手順で再度発火確認
- [ ] `py verify_project.py` が緑
- [ ] hook 変更後に実際に `git commit` が通ることを確認 (体感時間も記録)
- [ ] 各判断ゲートについて「なぜその選択をしたか」をコミットメッセージに残す

### Rollback
各変更は独立したコミットにする。問題があれば `git revert <sha>`。
hook 全体を止めたいときは `.claude/settings.json` の `hooks` ブロック削除。

---

## Session 3: CI を実際に走らせて緑にし、マージする

### 前提
現在 CI は **一度も起動していない** (GitHub API が 404 = ワークフロー未登録)。
`tests.yml` の `on: push` は `master`/`main` 限定なので、フィーチャーブランチへの
push では発火しない。**PR を作って初めて走る**。

### Steps

1. **PR を作成** (これが初回の CI トリガーになる)
   - base: `master` ← head: `claude/claude-skills-mcp-setup-hrl3jh`
   - リポジトリに PR テンプレートは無いので通常の本文でよい
   - 本文には Session 1/2 の**実測値**(verify秒数・テスト結果・node版)を必ず載せる

2. **CI の結果を見る**
   - `guardian` ジョブ (ブロッキング) が緑か
   - `tests` ジョブ (現在 `continue-on-error: true`) の失敗一覧が
     Session 1 の実機結果と一致するか
     → 一致しなければ CI 環境固有の問題があるので、そこを先に潰す

3. **`continue-on-error` を外せるか判断**
   - Session 2 の仕分けで (b)(c) を処理済みで、残りが0本 →
     `tests.yml` から `continue-on-error: true` を削除し、真のリグレッションゲートにする
   - まだ落ちるものが残る → **外さない**。落ちる理由を `tests.yml` の
     コメントに具体的に書き足して次回に送る

4. **マージ**
   - CI 緑を確認してからマージ
   - ⚠ **`--delete-branch` は使わない** — CLAUDE.md の既知バグパターン
     「stacked PR の base を `--delete-branch` 付きでマージすると積んでいた PR が
     自動クローズされる」に該当しうるため、他に積んでいる PR が無いことを
     確認してから、あるいはマージ後に手動で削除する

### Verification
- [ ] PR 上で `guardian` ジョブが緑
- [ ] `tests` ジョブの失敗一覧が実機の結果と一致している (乖離があれば原因を特定済み)
- [ ] `continue-on-error` を外したなら、外した状態で CI が緑
- [ ] マージ後、`master` で `py verify_project.py` が緑

### Rollback
マージ前なら PR をクローズしてブランチを残す。
マージ後に問題が出たら `git revert -m 1 <merge-sha>` でマージコミットごと戻す。

---

## この計画で触るファイル

| ファイル | Session | 内容 |
|---|---|---|
| `.claude/hooks/guard_git_commit.py` | 2 | 判断ゲートA次第でマーカー導入 or 高速版化 |
| `verify_project.py` | 2 | 判断ゲートAが「45秒超」の場合のみ `--fast` 追加 |
| `.mcp.json` | 2 | 判断ゲートB次第で `chrome-devtools` 削除 |
| `tests/test_*.py` (一部) | 2 | 判断ゲートC の (b)(c) 該当分を SKIP 対応 or 環境非依存化 |
| `.github/workflows/tests.yml` | 2-3 | 仕分け結果の反映 / `continue-on-error` 除去 |

## 既存資産の再利用

- `tests/test_vault_semantic_smoke.py` — 「前提が無ければ SKIP して終了コード0」の
  お手本。判断ゲートC の (b) はこの形に揃える
- `verify_project.py` の `check_undefined_names()` / `check_swallowed_exceptions()` —
  どちらも純粋な `ast` 処理でサブプロセス不要。高速版 hook はこの2つと
  エンコーディングチェックだけで構成できる
- `lib/bot_errors.is_failure_result()` — 成否判定はここに一元化する規約
