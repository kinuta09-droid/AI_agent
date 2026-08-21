# 夜間の自己評価レーダー (知能の回帰テスト)

> 作成: 2026-08-21 / 状態: **実装済み (実機での検証待ち)**
> 参照実装: `9abb931` (gws認証チェック = 定時 silent ジョブ + 異常時のみ Discord 通知)

## Goal

`scripts/eval_retrieval.py` (Hit@5 / MRR@10) と `scripts/eval_agent.py` (計画品質の
合格率) を**週次で自動実行**し、指標の推移を `logs/eval/history.jsonl` に追記する。
閾値割れ = 「知能が劣化した」ときだけ Discord に通知する。

**この機能が守るもの**: 検索チューニングや Planner プロンプト変更の劣化は、例外を
出さない・ログに一行も残らない (Known Bug Patterns A の「無言の死」)。現状 eval は
手動実行のみで、最後に測ったのがいつかも分からない (`logs/eval/` は空)。
「進化しているか」を測れない自律進化は名ばかり、という前提を機械化する。

**非目標** (今回やらない):
- Tier B (`eval_agent --full`) の自動実行 — 実ツールを叩くため。理由は Decision 3
- 劣化の自動修復・自動ロールバック — まず「気付ける」ところまで
- 指標の Obsidian へのレポート化 — Discord 通知と JSONL があれば足りる

---

## 現状の把握 (調査済みの事実)

| 項目 | eval_retrieval.py | eval_agent.py |
|---|---|---|
| 問題数 | 17問 (`tests/fixtures/retrieval_eval.json`) | 20問 (`agent_eval_set.json`) |
| 出力 | `logs/eval/retrieval_<stamp>[_label].json` | `logs/eval/eval_<stamp>.json` + `.md` |
| 主要指標 | `metrics["hit@5"]` / `["mrr@10"]` / `["mean_latency_sec"]` / `["degraded"]` | `summarize()` の `pass_rate` / `passed` / `graded` / `by_category` |
| 終了コード | 0=正常 / 1=前提不足で走れず / **2=走ったが数字が使えない** (`arm_clean=false`) | 0=完走 (**不合格が何問あっても0**) / 1=設定不正・予算不足 |
| Gemini 消費 | multiquery+rerank ON で 1問あたり最大2回 ≒ **34回** | Tier A = 1問1回 ≒ **20回** |
| 前提 | `logs/vault_index.db` 構築済 + Ollama (bge-m3) 起動 | `.env` の Gemini キー / 予算残 |
| 副作用 | 読み取りのみ (書くのは `logs/eval/` だけ) | Tier A は `generate_plan()` のみ = 副作用ゼロ |

既存の日次上限は `DAILY_GEMINI_LIGHT_LIMIT` / `DAILY_GEMINI_HEAVY_LIMIT` = 各200
(`lib/cost_guard.py`)。週次1回で合計 **約54回** = 合算上限400の13.5%。

---

## Decision log

**Decision 1: 週次 (日曜 04:00)、日次にしない**
- Reason: 54回/回 × 7日 = 378回/週は日次上限に効き、朝の生成系ジョブ (06:00〜08:00)
  の予算を食う。指標の劣化は「コミットしたら次の日曜に出る」で十分に早い。
- 時刻: 夜間チェーン (03:20〜03:40) の**後**、朝チェーン (06:00) の**前**。日付が
  変わって `DailyCounter` がリセットされた直後の枠を使う。
- Alternatives: 毎日 / コミット時 (CI) → CI 案は Ollama と Vault 実体を CI に持ち込む
  必要があり不可 (Known Bug Patterns B: Vault は `__file__` 相対にできない外部パス)。

**Decision 2: 成否を終了コードだけで判定しない (最重要)**
- `eval_agent.py` は**20問中20問が不合格でも exit 0**。「例外が出なかった=成功」と
  同型の罠 (Known Bug Patterns A)。判定は必ず**書き出された JSON を読み直して**行う。
- `eval_retrieval.py` の exit 2 (`arm_clean=false` = 施策ONのつもりが途中で
  フォールバックした混合アーム) は「走ったが数字が使えない」。**history に追記しない**。
  汚れた数字をベースラインに混ぜると、以後の比較が恒久的に嘘になる。
- 3値で扱う: `measured` (追記+閾値判定) / `unmeasured` (⚠️通知・追記しない) /
  `breached` (🔴通知)。

**Decision 3: eval_agent は Tier A のみ。`--full` は自動実行しない**
- Reason: Tier B は本物のツールを実行する。このリポジトリには「テスト/評価の実行が
  本番 Vault に書き込んだ」事故が2件記録されている (Known Bug Patterns C の
  `tool_create_note` 19件混入、`skill_radar_latest.json` 汚染)。**無人の定時ジョブに
  実書き込みの可能性を持たせない**。Tier B は人が見ている手動実行に限定する。

**Decision 4: 閾値は「絶対下限」と「直近中央値からの下落幅」の二段**
- 絶対下限だけだと、じわじわ下がって下限に触れるまで気付けない。
- 直近1回との比較だけだと、Gemini の非決定性で毎週フラップして通知が狼少年になる。
- → `median(直近3回・同一設定)` から `EVAL_RADAR_MAX_DROP` 以上下がったら通知。
  既定 0.13 ≒ 17問中2問ぶん (retrieval) / 0.10 = 20問中2問ぶん (agent)。
- **latency は通知対象にしない** (Ollama の VRAM 状態で揺れる)。推移には記録する。

**Decision 5: 設定が変わったら比較しない (`config_key`)**
- `--rerank off` に変えた週の Hit@5 低下を「劣化」と通知するのは誤報。history の各行に
  設定のハッシュ (`multiquery`/`rerank`/`loop`/`link_expand`/`rerank_backend`/
  `fixture` から生成) を持たせ、**同じ `config_key` の行同士でしか比較しない**。
  新しい `config_key` の初回は「ベースラインを引き直しました」と1回だけ通知する。

**Decision 6: 「レーダー自体が止まったこと」を検知する別ジョブを置く**
- これが無いと、ジョブが発火しなくなった / 毎週予算不足でスキップされ続けた、で
  結局「気付けない」に戻る (レーダーの無言の死)。
- 月曜 06:35 に `logs/eval/history.jsonl` の最終行の日付だけを見る**ゼロコスト**の
  チェックを置き、10日以上古ければ通知する。

**Decision 7: 実行は subprocess (`sys.executable`)、スケジューラ内 import ではない**
- 両 eval スクリプトは `sys.stdout.reconfigure()` と `os.environ[...]` の書き換えを
  行い `sys.exit(main())` で終わる CLI。スケジューラのプロセス内で呼ぶと 標準の
  ストリームと環境を汚す。`9abb931` の gws チェックと同じ subprocess 方式に揃える。
- `sys.executable` を使う (本番は `py` = システム Python で動き `antigravity_env` を
  activate しない — CLAUDE.md / Known Bug Patterns B)。

---

## Steps

### 1. `scripts/eval_radar.py` を新規作成 (純関数 + オーケストレーション)

純関数 (テストから直接呼べる。副作用なし):
- `config_key(config: dict) -> str` — 比較可能性の鍵 (Decision 5)
- `append_history(row: dict, path=HISTORY_PATH) -> None` — JSONL 1行追記
- `load_history(path=HISTORY_PATH) -> list[dict]` — 壊れた行はスキップして続行
- `evaluate(metric_name, value, history, floors, max_drop) -> Verdict` —
  `("ok" | "breach_floor" | "breach_drop" | "baseline", 説明文)` を返す
- `format_alert(verdicts, rows) -> str` — Discord 本文 (2000字上限を守る)
- `check_staleness(history, now, max_age_days) -> str` — 古ければ理由、正常なら `""`

オーケストレーション:
- `_run_one(kind) -> dict` — subprocess で eval を起動 →
  `logs/eval/` の**最新 JSON を読み直して**指標を取る (Decision 2)。
  タイムアウト (retrieval 1800s / agent 900s)、`arm_clean`/`meta.full` の検証を含む。
- `run_eval_radar() -> str` — 予算予約チェック → 2本実行 → history 追記 → 閾値判定
  → 異常時のみ `send_discord_notification`。
  **戻り値の契約**: 正常 = `""` (silent) / 測れなかった = `"Error: ..."`。
  → スケジューラ側の `is_failure_result()` が拾い、`catch_up` の成功記録を**残さない**
  ので、次回起動時に自動で追いかけ実行される (既存の仕組みに乗る)。
- `run_eval_staleness() -> str` — Decision 6。API 呼び出しゼロ。
- CLI: `--dry-run` (subprocess を起こさず、実行予定コマンドと Gemini 見積りを表示)、
  `--report` (推移表を標準出力へ)、`--only retrieval|agent`。

→ 期待結果: `py scripts/eval_radar.py --dry-run` が Gemini を1回も叩かずに
  「何をどれだけ消費して実行するか」を印字する。

### 2. 予算予約ガード

`GEMINI_LIGHT_COUNTER.limit + GEMINI_HEAVY_COUNTER.limit` の残量に対し、
`残り - 見積り(≒54) < EVAL_RADAR_MIN_RESERVE` (既定 150) ならスキップし、
`"Error: 予算不足のため測定を見送りました (残り N回)"` を返す。

- Why: `catch_up=True` なので、PC が日曜に落ちていれば**平日の起動直後**に発火し得る。
  そのとき朝のブリーフィング等の予算を奪わない。スキップは⚠️通知する
  (黙ってスキップ = 測っていないのに気付けない = この機能の存在意義の否定)。

→ 期待結果: `DAILY_GEMINI_LIGHT_LIMIT` を一時的に小さくすると、スキップ理由が
  Discord に届き、history に行が増えない。

### 3. `agent_scheduler.py` に2ジョブ追加 (薄いラッパのみ)

```python
{"time": "04:00", "name": "Eval Radar (Weekly)",  "func": _run_eval_radar,
 "weekday": 6, "silent": True, "catch_up": True},
{"time": "06:35", "name": "Eval Radar Staleness", "func": _run_eval_staleness,
 "weekday": 0, "silent": True, "catch_up": True},
```
`silent=True` の理由は gws チェックと同じ: 正常時は無言、異常時のみ自前通知。

→ 期待結果: `py agent_scheduler.py` の起動ログに2ジョブが載り、`--dry-run` 相当の
  手動実行で本番 Discord に届くことを確認できる。

### 4. `.env.example` / `CLAUDE.md` にノブを追記

| 変数 | 既定 | 意味 |
|---|---|---|
| `EVAL_RADAR_MIN_HIT5` | ブートストラップ後に実測から決める | Hit@5 絶対下限 |
| `EVAL_RADAR_MIN_MRR10` | 同上 | MRR@10 絶対下限 |
| `EVAL_RADAR_MIN_AGENT_PASS_RATE` | 同上 | 合格率の絶対下限 |
| `EVAL_RADAR_MAX_DROP` | `0.13` | 直近中央値からの許容下落 |
| `EVAL_RADAR_MIN_RESERVE` | `150` | 実行後に残すべき Gemini 予算 |
| `EVAL_RADAR_SKIP_RETRIEVAL` | `0` | Ollama を常時起動しない運用のための逃げ道 |
| `EVAL_RADAR_STALE_DAYS` | `10` | 推移が途絶えたと判断する日数 |

**閾値は推測で書かない**。手順6のブートストラップで出た実測値から
「1問ぶん (retrieval なら 1/17≒0.06) 下」を初期値にする。

### 5. `tests/test_eval_radar.py` を新規作成 + sweep へ登録

- [1] `evaluate` の境界: 下限ちょうど=OK / 1つ下=breach、`max_drop` ちょうど=OK
- [2] history が0〜1行のときは `baseline` を返し、**通知しない**
- [3] `config_key` が変わったら過去行を比較に使わない (誤報の回帰)
- [4] `arm_clean=false` / exit 1 / タイムアウト → history に追記されない
- [5] `unmeasured` の戻り値が `lib.bot_errors.is_failure_result()` で真になる
      (= `catch_up` が成功記録を残さない、という結合部分の契約)
- [6] `check_staleness` の境界 (9日=OK / 11日=通知)
- [7] **資源隔離**: `HISTORY_PATH` は `LOGS_DIR` から import 時に計算される定数なので、
      `LOGS_DIR` だけ差し替えても連動しない (Known Bug Patterns C の
      `skill_radar_latest.json` 事故と同型)。テストは `radar.HISTORY_PATH` 自体を
      tmpdir へ退避し、実行後に**本番 `logs/eval/history.jsonl` が増えていないこと**を
      アサートする
- [8] ast 静的検査: `run_eval_radar` が subprocess の `returncode` だけで
      成功判定していないこと (JSON 読み直しを経ていること) を構造で縛る
- `scripts/run_test_sweep.py` の `PARALLEL_TESTS` に `test_eval_radar.py` を追加
  (未分類は `tests/test_sweep_runner.py` が赤にする)

### 6. ブートストラップ (人間が1回だけやる)

```
py scripts/eval_retrieval.py --label baseline
py scripts/eval_agent.py
py scripts/eval_radar.py --report      # history 2行と推移表を確認
```
→ 出た実測値から手順4の3つの下限を `.env` に書く。

---

## Verification

- [ ] `py scripts/eval_radar.py --dry-run` 実行前後で `logs/gemini_daily_calls.json` の
      カウントが**1も増えない** (宣言ではなくカウンタ差分で確かめる)
- [ ] ブートストラップ2本を実行 → `logs/eval/history.jsonl` が2行、
      `--report` の推移表が実測値と一致
- [ ] **故障注入 (a) 前提不足**: `logs/vault_index.db` を一時リネーム → radar が
      ⚠️ を Discord へ送り、戻り値が `Error:` で始まり、history が**増えない**
- [ ] **故障注入 (b) 閾値割れ**: `EVAL_RADAR_MIN_HIT5` を現状値より上に設定して実行 →
      🔴 通知が出て、本文に「どの指標が・いくつから・いくつへ」が載る
      (1週間待たずに通知経路が生きていることを確認する)
- [ ] **故障注入 (c) 汚れたアーム**: Ollama を落として `--rerank on` で実行 →
      exit 2 を検知し、history に追記されないこと
- [ ] **故障注入 (d) 停止検知**: history 最終行の timestamp を12日前に書き換え →
      `run_eval_staleness()` が通知、戻すと無言
- [ ] Tier B が走っていないこと: 生成された `eval_*.json` の `meta.full == false`
- [ ] 実 Vault の差分ゼロ: 実行前後で Vault のファイル数が変わらない
- [ ] `py scripts/run_test_sweep.py --filter eval_radar` が緑
- [ ] `py verify_project.py` フルが緑 (絶対パス直書き検査・import 整合性を含む)
- [ ] スケジューラ実機: `Eval Radar (Weekly)` を一時的に直近時刻へずらして発火させ、
      正常時に**何も通知されない** (silent) ことと、`agent_job_state.json` に成功が
      記録されることを確認 → 時刻を戻す

## Rollback

1. **機能停止のみ**: `agent_scheduler.py` の SCHEDULE から2エントリを削除して再起動。
   `scripts/eval_radar.py` は誰からも呼ばれなくなるだけで無害 (手動実行は残る)。
2. **完全撤回**: `git revert <commit>`。`logs/eval/history.jsonl` は gitignore 配下
   なので残るが、`config_key` が変わらない限り再導入時にそのまま推移として使える。
3. 通知がうるさい場合の中間手段: `EVAL_RADAR_MAX_DROP` を上げる /
   `EVAL_RADAR_SKIP_RETRIEVAL=1` で片肺運用。

---

## Open questions (回答済み・2026-08-21)

1. **PC が落ちていた週の扱い** → **次回起動時に繰り越す**。`catch_up=True` に加えて、
   「測れなかった実行は成功として記録しない」戻り値の契約でこれを実現した
   (Ollama 停止・予算不足・タイムアウトも同じ扱い = 次回起動時に再挑戦)。
   ただし失敗直後の再起動連打で Gemini を浪費しないよう、
   `EVAL_RADAR_RETRY_COOLDOWN_HOURS` (既定6時間) の再試行間隔を設けた
   (繰り越し自体は維持したまま、実行だけ見送る)。
2. **通知先** → 既存のログチャンネル (`DISCORD_LOG_CHANNEL_ID`)。
   `lib.watchdog.notify_discord` を共用する。
3. **history の git 管理** → しない (現状どおり `logs/` は gitignore)。

---

## 実装結果 (2026-08-21)

| ファイル | 変更 |
|---|---|
| `scripts/eval_radar.py` | 新規。純関数 (`evaluate` / `config_key` / `check_staleness` 等) + ジョブ本体 + CLI |
| `agent_scheduler.py` | `_run_eval_radar` / `_run_eval_staleness` と SCHEDULE 2件 (日曜04:00 / 月曜06:35) |
| `tests/test_eval_radar.py` | 新規。10グループ 71 チェック |
| `scripts/run_test_sweep.py` | `PARALLEL_TESTS` に登録 (未分類は sweep テストが赤にする) |
| `CLAUDE.md` | Commands に `--dry-run` / `--report` を追記 |

計画から変わった点 (実装中に判明):
- **予算確認は fail-closed にした**。`lib.cost_guard` の import やカウンタ読み取りに
  失敗したとき `""` (=予算OK) を返すと、日次上限の意味が黙って消える。
  「確認できなかった」も見送り理由として扱う。静的検査 [9](c) で縛った。
- **結果 JSON の特定に更新時刻のフォールバックを足した**。両ハーネスのファイル名は
  分単位 (`%Y%m%d_%H%M`) なので、同じ分に2回走らせると同名を上書きする。
  「新規ファイルが無い = 測れなかった」と誤判定しないようにした。
- **通知の重複抑止 (20時間)** を足した。catch_up により測定失敗は起動のたびに
  再試行されるため、抑止が無いと同じ ⚠️ が再起動のたびに飛ぶ。

### この環境で確認できたこと
- `py tests/test_eval_radar.py` → **71 PASS / 0 FAIL**
  (前提不足 exit≠0 / JSON 未生成 / 混合アーム / Tier B 混入 / タイムアウト /
   全問スキップ / 予算不足 の7経路すべてで「履歴に1行も増えない」ことを実ファイルで確認)
- `py scripts/run_test_sweep.py --filter eval_radar` → 1/1 緑
- `py scripts/run_test_sweep.py --filter sweep_runner` → 22/0 緑 (レーン分類の登録漏れなし)
- `verify_project` の静的検査 (encoding / syntax / undefined / swallowed /
  machine-locked syspath / hardcoded abs path) を変更4ファイルに対して個別実行 → 指摘なし
- `py scripts/eval_radar.py --dry-run` / `--report` が API を1回も叩かずに動作

### この環境では確認できていないこと (実機で必要)
Linux コンテナのため `msvcrt` を import する `lib/file_lock.py` 系
(= `agent_scheduler` を import するテスト) と、`google.genai` を要するテストが動かない。
- [ ] `py verify_project.py` (フル)
- [ ] `py scripts/run_test_sweep.py` (全件)
- [ ] 下記のブートストラップと故障注入

> 参考: この環境の `verify_project --fast` は `tools/tool_paper_digest.py` を
> Syntax Error として報告するが、これは Python 3.11 では f-string 内のバックスラッシュが
> 構文エラーになるため (実機は 3.13 で合法)。今回の変更とは無関係で、当該ファイルは未変更。

### 実機でやること (この順に)

1. **ブートストラップ** (閾値を実測から決める)
   ```
   py scripts\eval_radar.py --dry-run     # 見積りと予算の確認
   py scripts\eval_radar.py               # 初回測定 (ℹ️ ベースライン通知が届く)
   py scripts\eval_radar.py --report      # 履歴2行を確認
   ```
   出た値から `.env` に3つの下限を書く (実測より1問ぶん下を目安に)。
2. **故障注入で通知経路が生きていることを確認** (1週間待たない)
   - `EVAL_RADAR_MIN_HIT5` を現状値より上にして実行 → 🔴 通知が届く → 元に戻す
   - `logs\vault_index.db` を一時リネームして実行 → ⚠️ 通知 + 履歴が増えない
   - `logs\eval\history.jsonl` の日時を12日前に書き換え →
     `py scripts\eval_radar.py --staleness` で 🟠 通知 → 元に戻す
3. **スケジューラ実機**: `Eval Radar (Weekly)` を直近時刻へ一時的にずらして発火させ、
   正常時に無言であること・`logs\agent_job_state.json` に成功が記録されることを確認 →
   時刻を戻す。

---

## 残る運用上の注意 (Ollama)

日曜 04:00 に **Ollama が起動していない**場合、検索評価は毎週 ⚠️「測定できませんでした」
になる。今回の繰り越し設計により、この週の分は**次回のスケジューラ起動時に自動で再挑戦**
されるので、日中に PC を立ち上げて Ollama が動いていれば拾える (再試行の間隔は
`EVAL_RADAR_RETRY_COOLDOWN_HOURS`=6時間)。それでも毎週失敗が続くようなら:

- (a) 検索評価だけ止める: `.env` に `EVAL_RADAR_SKIP_RETRIEVAL=1`
      (エージェント評価は Ollama 不要なので単独で回り続ける)
- (b) 実行時刻を Ollama が確実に上がっている枠へ移す

初回のブートストラップで `--dry-run` と実測を回せば、この判断はすぐ付く。
