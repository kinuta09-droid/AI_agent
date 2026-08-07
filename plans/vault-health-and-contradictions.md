# Vault RAG Push機能 設計: 週次未使用率 + 矛盾記録の永続化

## Context

CyrilXBTの「話しかけてくるRAG」記事と、ARMSの既存実装を突き合わせた結果、「push型RAG」の中核（`tool_vault_watcher.py`の observe→notice→propose、毎晩03:20自動実行）は既に動いていることが判明した。残る実質的なギャップは2つに絞り込んだ:

1. 「集めるだけで満足」失敗モードを検知する数値指標が無い（CyrilXBT記事の項目⑭）
2. `tool_vault_watcher`が矛盾を検知しても、Discordの一過性メッセージと`logs/`配下のJSONL（Obsidianからは不可視）にしか残らず、Vault内に検索可能な形で永続化されない（項目⑳、かつARMS自身の Known Bug Patterns A「無言の死」パターンと同型）

このドキュメントは設計のみで、実装は次回セッションで行う。このクラウドセッションからは実際のObsidian Vault（`D:\マイドライブ\Obsidian`、Windows上）へ直接I/Oできないため、Vaultへの実書き込みを伴う検証は次回、実マシン上のセッションで行う必要がある。

---

## Feature 1: 週次「未使用ノート率」ヘルスメトリクス（LLM呼び出しなし）

### 配置: 新規モジュール `tools/tool_vault_health.py`

`tools/tool_crypto_stats.py`が`tool_crypto.py`のストアを読むだけの集計モジュールとして分離されているのと同じ流儀。`tool_vault_index.py`（900行超）に機能を足さず、`DB_PATH`/`SOURCE_VAULT`/`_connect`だけを`tools.tool_vault_index`からインポートして使う（`tool_activity_index.py`が同じ手法で"private"ヘルパーを越境インポートしている前例あり）。

### 実装内容

```python
STALE_DAYS = 14
HEALTH_NOTE_PATH = os.path.join(VAULT_ROOT, "00_System", "Vault_Health.md")

def _compute_unused_stats(db_path=DB_PATH, now=None) -> dict:
    # SQL (下記) を実行し {"total": int, "unused": int, "pct": float,
    #  "stale_days": STALE_DAYS, "cutoff": float} を返す。総数0は正常系。

def _format_health_entry(stats, today=None) -> str:
    # 純関数。追記する1行 "| 日付 | 未使用率 | 未使用件数/総ノート数 |" を返す。

def check_vault_health(db_path=DB_PATH, note_path=HEALTH_NOTE_PATH,
                        now=None, today=None) -> str:
    # スケジューラ/手動エントリポイント。例外は握りつぶさず f"Error: {e}" を返す
    # (bot_errors.is_failure_result がそのまま使える契約)。
```

**SQL（`_compute_unused_stats`の核）**:
```sql
SELECT
    COUNT(*) AS total,
    SUM(
        CASE WHEN mtime < :cutoff
             AND filepath NOT IN (
                 SELECT dst_path FROM links WHERE dst_path IS NOT NULL
             )
        THEN 1 ELSE 0 END
    ) AS unused
FROM files
WHERE source = :source;
```
- `cutoff = (now or time.time()) - STALE_DAYS * 86400`、`source = SOURCE_VAULT`
- **重要な落とし穴**: サブクエリの`WHERE dst_path IS NOT NULL`は必須。`links.dst_path`は未解決リンクで常にNULLになり得るため、これを外すと`NOT IN`がSQLの三値論理で全行`NULL`（=false）評価になり、`unused`が常に0という**無言のメトリクス破壊**が起きる。回帰テストで明示的に検証すること。

**`check_vault_health`の流れ**:
1. `total == 0` → Vaultにまだ書き込まず、スキップメッセージのみ返す（空DBでゴミ行を残さない）
2. `note_path`が存在しなければヘッダー付きで新規作成（`tool_curator.py`のPROFILE_PATH初期化と同じ「初回だけ`'w'`、以降は`'a'`」idiom）、既存行は絶対に書き換えず追記のみ
3. 戻り値は `f"Vault Health: 未使用率 {pct}% ({unused}/{total}件, {STALE_DAYS}日以上更新なし＆被リンク0) → {format_wikilink('Vault_Health')}"`
4. `FileLock`は不要（このジョブ以外に`Vault_Health.md`の書き手がいないため）。将来手動トリガーを追加するときが`FileLock`導入のタイミング

### スケジューラ登録（`agent_scheduler.py`）

`03:25 Activity Index`の直後に追加（同じ夜の`03:15 Vault Semantic Index`で`links`/`files`が最新化された後に読むため）:
```python
{
    "time": "03:30",
    "name": "Vault Health Check (未使用ノート率)",
    "func": check_vault_health,
    "weekday": 6,  # 日曜。週次トレンドとして意味があるのは週1回
    "catch_up": True,  # 他の週次ジョブ (Weekly Crypto Report 等) と同じ
},
```
`silent`は付けない（既存の週次ジョブ4種はどれも付けていない — 週1回の通知はスパムに当たらない）。他の`optional import`ブロックと同じ形でインポートガードを追加。

### テスト: `tests/test_vault_health.py`（新規）

`tests/test_vault_links.py`と同じ流儀（tempfile SQLite、Ollama/Gemini不使用）。必須ケース:
1. 基本の未使用/使用済み分類（新旧×被リンク有無の組み合わせ）
2. **NULL dst_pathトラップの回帰テスト**（上記の落とし穴を再現し、正しく検出できることを確認）
3. `source != 'vault'`の行が集計から漏れずに除外される
4. 空DB→`total==0`でノート未作成
5. 追記のみ・ヘッダー重複なし（2回連続実行）
6. 境界値（`mtime == cutoff`ちょうどはカウントしない）

---

## Feature 2: Vault Insight Watcherに矛盾記録の永続化を追加

### 対象: `tools/tool_vault_watcher.py`（既存334行、拡張）

既存の`_build_notice_prompt`は既に`relation: complements|contradicts|extends`をJSONスキーマに持っている。今回のギャップは「矛盾を検知した後、Discordの一過性メッセージと`logs/`のJSONL（Obsidianから不可視）にしか残らない」こと。`relation=="contradicts"`のときだけ、Vault内の追記専用ノートに構造化して残す。

### 実装内容

**新モジュール定数**（既存の`SCAN_DIR`等と同じ並び）:
```python
CONTRADICTIONS_NOTE = os.path.join(VAULT_ROOT, "00_System", "Vault_Contradictions.md")
```

**新関数** `_default_write_contradiction(relpath, title, result) -> None`:
- `lib.lib_obsidian_utils.format_wikilink` / `split_heading_anchor` を対で使う（CLAUDE.md §E: 出す側と解決側の形式が食い違うとObsidianの「開く」ボタンが無言で壊れる、という既知パターンの再発防止）
- ノート未存在なら`- [ ] resolved`チェックボックス方式のヘッダーを`'w'`で作成（`tool_daily_note.py`のタスク表記と同じidiom、Dataviewフィールドのような前例のない形式は使わない）
- 以降は`'a'`で追記のみ。1エントリ = 日時 + 自ノートへのwikilink + 関連ノートへのwikilink + `insight`本文

**`check_vault_insights`のシグネチャ変更**（純粋な追加、既存呼び出し元は無変更で動く）:
```python
def check_vault_insights(max_files=MAX_FILES_PER_RUN, notify=True,
                          search_fn=None, llm_fn=None, notify_fn=None,
                          contradiction_writer_fn=None) -> str:
    contradiction_writer_fn = contradiction_writer_fn or _default_write_contradiction
```
`agent_scheduler.py`側の変更は不要（デフォルト引数で完結）。

**挿入位置**: 既存の`_append_insight_log`呼び出し直後、**`with FileLock(SEEN_MTIMES_CACHE):`のスコープ内のまま**:
```python
if bool(result.get("notable")) and result.get("relation") == "contradicts":
    try:
        contradiction_writer_fn(relpath, title, result)
    except Exception as e:
        print(f"[VaultWatcher] ⚠️ 矛盾記録の書き込みに失敗、続行します: {relpath} ({e})")
```
- `FileLock`スコープの外に出さないこと（このロックが夜間ジョブと手動`/vault_watch`のレースを既に防いでいる。新しい`FileLock`を別途用意する必要はない）
- `notable`と`relation=="contradicts"`の両方を見る理由: `_sanitize_result`が必須フィールド欠落時に`notable`だけをFalseに倒すことがあるため、「偽陰性側に倒す」既存方針をこの新しい書き込み経路にも適用する
- Gemini呼び出しは増えない（既存の`llm_fn`が返した`result`を見るだけ）。新しい`cost_guard`カウンタは不要

### テスト: `tests/test_vault_watcher.py`（既存に追加）

`_reset_watcher`に`watcher.CONTRADICTIONS_NOTE`のリセットを追加した上で:
1. `relation=="contradicts"`のときだけ発火（`complements`/`extends`では`_fail`センチネルで未呼び出しを確認）
2. `_sanitize_result`によるnotable降格時は書き込まれない
3. 実`_default_write_contradiction`統合テスト（ヘッダー・wikilink・`- [ ] resolved`・insightが揃っているか）
4. 追記のみ・ヘッダー重複なし（2回の矛盾検知をまたいで）
5. `contradiction_writer_fn`が例外を投げても`check_vault_insights`全体は失敗しない（既存の`llm_fn`例外耐性テストと同型）
6. `format_wikilink`/`split_heading_anchor`の対応関係の単体テスト（`related_note`に`#見出し`が混ざっても壊れたリンクにならないか）

---

## 対象外（今回は設計しない）

- N件たまったら横断レポート生成（CyrilXBT項目⑩）
- 壊れたリンク/重複/未整理の一括修復（項目⑮）
- 個人の意思決定ジャーナル・事前結論→1ヶ月後答え合わせ（項目⑲）— Vault側のテンプレート運用の話であり、ARMSのコード機能ではないと判断

## Critical Files

- `tools/tool_vault_health.py` (新規 — Feature 1)
- `tools/tool_vault_watcher.py` (拡張 — Feature 2)
- `agent_scheduler.py` (Feature 1のみジョブ登録追加。Feature 2はスケジューラ変更不要)
- `lib/lib_obsidian_utils.py` (参照のみ — `format_wikilink`/`split_heading_anchor`)
- `tools/tool_vault_index.py` (参照のみ — `DB_PATH`/`SOURCE_VAULT`/`_connect`/スキーマ)
- `tests/test_vault_health.py` (新規)
- `tests/test_vault_watcher.py` (拡張)

## Verification（次回実装セッションで実施）

1. `py scripts/run_test_sweep.py --filter vault_health,vault_watcher` で新規/拡張テストが通ること
2. `py verify_project.py` (Guardian) が通ること
3. 実マシン上で `py -c "from tools.tool_vault_health import check_vault_health; print(check_vault_health())"` を実行し、実Vaultの`00_System/Vault_Health.md`が正しい形式で作成/追記されることを目視確認（このクラウドセッションでは実施不可）
4. 同様に矛盾が起きるダミーノート2枚を実Vaultに置いて`check_vault_insights()`を手動実行し、`Vault_Contradictions.md`が期待通り追記されることを確認
5. `git commit`前に`Grill Me Rules`（review-protocol.md）に沿ってエッジケース（NULL dst_path、空Vault、ロック競合、format_wikilink異常入力）をレビュー
