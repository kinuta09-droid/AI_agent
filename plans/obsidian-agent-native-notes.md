# 記事「AIエージェントのためのObsidian活用術」→ ARMS実装プラン

## Context

ユーザーが読んだ記事（@ObsidianOtaku、原典は cyrilXBT の X 投稿）は、ノートを
「type / when / topic / status」の4手がかりで書き、ファイル名を
`YYYY-MM-DD-[種類]-[主題].md`、frontmatterに5項目、タグは `/` 階層で分類し、
Claude Code に Vault フォルダを渡して自然言語で聞けば関連ノートが返ってくる状態を作る、
と提案している。

**先行事実**: `plans/vault-health-and-contradictions.md` が同じ原典（cyrilXBTの
「話しかけてくるRAG」記事、項目①〜㉑）を既にARMSの実装と突き合わせ済みで、
「個人の意思決定ジャーナル・テンプレート運用の話はVault側の話であり、ARMSのコード機能では
ない」と明示的に切り分けている（項目⑲）。今回はその境界線を、frontmatter/命名/タグという
より具体的なレイヤーに対して同じ基準で当てはめ直した。

ユーザー自身も「個人利用では会議のような日次入力がそもそも無い」と指摘しており、記事が
前提とする「毎日の会議録が溜まる」状況とARMSの実際の使われ方（ニュース/暗号資産/論文/
アイデアの自動生成が主）にはギャップがある。したがって記事の提案を無条件に実装せず、
ARMSの既存アーキテクチャで**既に解決済みのもの**・**Vault運用（テンプレート編集）の話で
ARMSのコードではないもの**・**今のノート量では時期尚早なもの**を先に削り、残った実質的な
ギャップだけをコードに落とす。

## 調査で判明した現状（既に持っているもの）

- **命名**: `01_Daily`(`YYYY-MM-DD.md`)・`02_News`・`03_Crypto` は既に日付先頭命名。
  週次アーカイブ (`tools/tool_obsidian.py:_date_of_file`) がファイル名の日付を最優先で読む
  設計まである。一方 `tool_create_note`(30_Main) と `tool_save_idea`(00_inbox) は自由文
  タイトルのみで日付プレフィックス無し。
- **type**: フォルダ配置自体が種類を表す（Daily/News/Crypto/Report/Main/Inbox）＋
  `type/fleeting` のような階層タグ（`tests/test_inbox_clip_tagging.py` で確認）。記事の言う
  独立した `type:` frontmatterキーは無いが、実質的に二重管理されていないだけで代替済み。
- **topic**: `tools/tool_obsidian.py:_determine_parent_moc` が Gemini で
  `up: "[[MOC名]]"` を自動付与、かつ `tools/tool_bitnet.py:generate_note_tags` が英語
  トピックタグを自動生成 (`tag_untagged_clips` 経由)。
- **status**: `00_inbox` の Fleeting Note のみ `status: open` を使用し、
  `tool_list_open_ideas` が正規表現 `^status:\s*(\S+)` で読んでいる。30_Main等の他ノートには
  無い。
- **検索/引き出し**: `tools/tool_vault_hybrid.py` が multiquery展開 + RRF融合 + LLMリランク
  + 再検索ループを持つ、記事が想定する「Claude CodeがVaultを素で読む」より遥かに高度な実装
  が既に稼働中。
- **健全性メトリクス**: `tools/tool_vault_health.py` が週次で「未使用ノート率」を
  `Vault_Health.md` に追記済み（`vault_index.db` の `files`/`links` テーブルを読むだけ、
  LLM不使用）。ただし `files` テーブルは `filepath/mtime/chunk_count/source` のみで
  frontmatter内容は保持していない。

## 取捨（記事の主張 → 判定 → 理由）

| # | 記事の主張 | 判定 | 理由 |
|---|---|---|---|
| 1 | `type:` frontmatterキー新設 | **不要** | フォルダ配置＋`type/xxx`タグで既に代替。二重管理は「表示と実態の食い違い」系バグ（Known Bug Patterns A）の温床になるだけ |
| 2 | `topic:` frontmatterキー新設 | **不要** | `up:` MOCリンク＋自動topicタグで既に代替 |
| 3 | ファイル名 `YYYY-MM-DD-種類-主題` | **不要（ARMSコードとしては）** | Daily/News/Cryptoは既に日付先頭。30_Main/00_inboxへの追加は人間が眺めるときの整列にしか効かない — ARMSの検索はfrontmatter/mtimeを読み、ファイル名の日付をパースしていないので引き出し能力は変わらない |
| 4 | frontmatter 5項目テンプレート | **要・ただし検証のみ** | `Tpl_Permanent.md`/`Tpl_Fleeting.md` はVault側（このクラウドセッションからは読めない）。`date:`が本文置換用の`{{date}}`でなくfrontmatterキーとしても存在するか実機で確認要。無ければVaultテンプレート編集で足す（ARMSのPythonコード変更ではない） |
| 5 | `status:` の徹底 | **要（小さいコード変更）** | 30_Main（記事が言う「決定/知見」に一番近いノート種別）にstatusが無い。ここだけ埋める価値がある |
| 6 | タグの `/` 階層化・5件以上ルール | **不要（既に部分実装）** | `type/`・`moc/`・`clip/processed` は既に階層化済み。運用ルールの明文化のみで足りる |
| 7 | `project/`タグ新設 | **見送り（時期尚早）** | 現状クライアント案件ノートが存在しない。実データが増えてから作る。CLAUDE.md「削減効果が明確でない先回りの汎用化までは正当化しない」に該当 |
| 8 | Claude Codeが自然言語でVaultを直接引く | **不要** | `tool_vault_hybrid_search` が既にこれより高性能。記事の前提（素のCode CLI）はARMSでは既に上位互換で解決済み |
| 9 | 診断チェックリスト（7項目セルフチェック） | **要（コード化）** | 一回きりの手動チェックではなく、`tool_vault_health.py`と同じ週次メトリクスとして自動化する方が「集めるだけで満足」の再発防止になる（既存Feature 1と同型） |

## Goal

30_Main配下のノートで `status:` frontmatterが欠落している割合を、既存の
`tool_vault_health.py` の週次ジョブに相乗りする形で可視化する。それ以外の記事の提案
（項目1,2,3,6,7,8）はARMSのコードとしては実装しない。

## Steps

1. `lib/lib_obsidian_utils.py` に `read_frontmatter_field(path, key, head_bytes=500)` を
   追加する（`tool_list_open_ideas` と新設メトリクスが同じ「frontmatter先頭を正規表現で
   読む」処理を別々に書かないための共通化）。
   → 期待結果: ファイル未存在/読み取り失敗/キー未設定はすべて `None`、例外を投げない。
2. `tools/tool_obsidian.py` の `tool_list_open_ideas` を `read_frontmatter_field` 使用に
   置き換える（挙動は変えない、既存の `status != 'open'` 判定ロジックのみ流用）。
   → 期待結果: 既存テスト（あれば）が無変更で通る。
3. `tools/tool_vault_health.py` に `_compute_frontmatter_gap_stats(vault_root=VAULT_ROOT,
   target_subdir="30_Main")` を追加する。`vault_index.db` は frontmatter内容を持たないため
   `tools/tool_obsidian.py:_keyword_hits` と同じ `os.walk` 方式で `30_Main` 配下の `.md` を
   直接読み、`read_frontmatter_field(path, "status")` が `None` の件数/率を返す。LLM不使用。
   → 期待結果: `{"total": int, "missing_status": int, "pct": float}` を返す。0件は正常系。
4. `_format_health_entry` / `check_vault_health` を拡張し、`Vault_Health.md` の追記行に
   status欠落率の列を追加する。**既存行は書き換えず、新規追記分からのみ4列にする**
   （後方互換を優先。既存ヘッダーが3列のままの既存ファイルでは、新しい行だけ列数が増えて
   見た目が揃わなくなるが許容する — 追記専用ノートなのでデータ破損にはならない）。
   → 期待結果: `check_vault_health()` の戻り値文字列にstatus欠落率が含まれる。
5. `tests/test_vault_health.py` に新メトリクスのテストを追加する（既存の6ケースと同じ
   tempfile方式。本番Vault/DBには一切触れない）。
6. `tests/test_lib_obsidian_utils.py` を新規作成し、`read_frontmatter_field` の単体テストを
   書く（正常系・キー無し・frontmatter無し・壊れたYAML風テキスト・ファイル未存在）。
7. `scripts/run_test_sweep.py` の `PARALLEL_TESTS` に `test_lib_obsidian_utils.py` を追加する
   （純粋関数のテストで共有状態を持たないため並列レーンに分類。`test_sweep_runner.py` の
   未分類検知に引っかからないようにするため必須）。

## Verification

- `py scripts/run_test_sweep.py --filter vault_health,lib_obsidian_utils,inbox_clip_tagging`
  で新規/既存テストがPASS
- `py verify_project.py`（Guardian、ハードコード絶対パス・握りつぶし例外の検査）
- 実Vaultへの反映確認はこのクラウドセッションから実施不可。実機セッションで
  `py -c "from tools.tool_vault_health import check_vault_health; print(check_vault_health())"`
  を実行し、`00_System/Vault_Health.md` に新列が正しい形式で追記されることを目視確認する
  （次回実機タスク）。
- `Tpl_Permanent.md`/`Tpl_Fleeting.md` の `date:`/`status:` frontmatterキー有無も実機で
  目視確認する（取捨表 #4・#5 の裏取り。無ければVault側でテンプレートに追記する — ARMSの
  コード変更ではなくVault運用）。

## Rollback

`tool_vault_health.py` の追加関数・列と `read_frontmatter_field` 呼び出し箇所を削除するだけ
で完全に無効化できる。`Vault_Health.md` は追記専用なので、ロールバック後も過去の列違いの
行がそのまま残るのみで実害は無い（表示上、列数が途中で変わる行が混在するだけ）。

## 対象外・明示的に見送り

- `type:`/`topic:` frontmatterキーの新設（取捨表 #1, #2）
- 30_Main/00_inboxのファイル名日付プレフィックス強制（取捨表 #3）
- `project/`タグ運用（取捨表 #7）— 案件ノートが実在するタイミングで再検討
- Vaultテンプレート（`Tpl_Permanent.md`等）自体の編集 — Vaultはこのクラウドセッションから
  直接I/Oできない。実機での確認・編集はユーザー側タスク
- status/type/topicによる検索フィルタ機能（`tool_vault_hybrid_search`の拡張）— ノート量が
  少ない現状では効果を測る材料が無い
