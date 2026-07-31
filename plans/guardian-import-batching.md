# Guardian のフル実行を 165秒 → 10秒前後にする

> **✅ 実行済み (2026-07-31・ブランチ `perf/guardian-import-batching`)**
> 実測 **172.40秒 → 7.5秒**。`--fast` は 1.3秒で退行なし。
> 回帰テスト `tests/test_guardian_batch_import.py` (58チェック) を新設。
> 計画との差分:
> - `sys.path` 汚染は計画どおり**警告止まり**にした。ただし復元しても
>   `sys.modules` は復元されないので、非ルートパス経由で import された副モジュールは
>   後続に見えたままになる ＝ **バッチ化に固有の唯一の検出力低下**が残っている
>   (実測でこのリポジトリに非ルート追加はゼロ。出たら `⚠️` が出る)。
>   エラーへの格上げは要判断
> - `[6]` は「バッチと同じ子プロセスで `_providers` のキーまで取る」形にして
>   専用サブプロセスを廃止 (計画では `[6]` もバッチに含めるとだけ書かれていた)
> 以下は当時の計画のまま残す。

## このセッションの始め方

```
cd C:\AI_agent
git checkout -b perf/guardian-import-batching
```
Claude Code を起動し、Plan Mode に入って最初にこう指示する:
> `plans/guardian-import-batching.md` を読んで、Plan Mode で計画を立てて

**計測はもう終わっている。** 下記「実測値」を出発点にしてよい。数字を取り直す必要はない
(取り直したいなら Session 1 の作法どおり、変更前に測ってから始めること)。

---

## Goal

`py verify_project.py` (フル) の所要時間を **164.69秒 → 20秒未満** にする。
検出力は落とさない。`--fast` (commit hook が呼ぶ) は 1秒台のまま維持する。

## Context — 165秒はどこに消えているか

`--fast` が 1.07秒なので、**残り約164秒は全部 `[3]` import整合性 / `[4]` TOOLSレジストリ /
`[6]` Briefing プロバイダー**。この3つだけがサブプロセスを起動する。

### 実測値 (2026-07-30・master 241c8e3 で計測)

| 測定 | 結果 |
|---|---|
| `py verify_project.py` フル | **164.69秒** / exit 0 |
| `py verify_project.py --fast` | **1.07秒** / exit 0 |
| ユニークなモジュール数 | **27** |
| 実際に起動しているサブプロセス数 | **35** (`[3]` 20件 + `[4]` 15件、うち **8件が重複**) |
| 単価: `lib.pid_guard` | 0.04秒 |
| 単価: `lib.lib_shared` | 3.50秒 |
| 単価: `tools.tool_crypto_stats` | 5.85秒 |
| 単価: `bot_discord` | **7.37秒** |
| **27件を1プロセスでまとめて import** | **6.98秒** |
| サブプロセスを4並列 | 43.86秒 |
| サブプロセスを8並列 | 30.03秒 |
| CPU 論理コア | 16 |

### 結論: 並列化ではなくバッチ化

単価が 0.04秒〜7.37秒とばらけるのは、重いモジュールが ccxt / pandas / google.genai を
引き連れてくるからで、**27回とも同じ重量級スタックをゼロからロードし直している**。
1プロセスなら `sys.modules` にキャッシュされて1回分で済む。

**並列化 (30秒) はバッチ化 (7秒) に4倍負ける。** 16コアあっても、各プロセスが同じ依存を
独立にロードする無駄は消えない。**ここを取り違えて並列化に走らないこと。**

### バッチ化しても検出力が落ちないことは検証済み

細工したモジュールで確認した (5/5 合格):

| 仕込み | 期待 | 結果 |
|---|---|---|
| 存在しないモジュール | 検出 | ✅ `ModuleNotFoundError` |
| import 時に `sys.exit(1)` | 検出 | ✅ `SystemExit: 1` |
| その `sys.exit` の**後続**モジュール | 到達できる | ✅ バッチが死なない |
| import 時に `sys.path` を汚す | 検出 | ✅ 追加パスを報告 |
| 正常なモジュール | 誤検出しない | ✅ |

---

## Steps

1. **重複8件を除去** → 35 → 27 サブプロセス相当。無条件の得
2. **1サブプロセスに集約**。その中で以下をまとめて実行し、結果を **JSON1行**で返す
   - `[3]` 27モジュールの import
   - `[4]` レジストリ関数の `hasattr(mod, func_name)` 判定
   - `[6]` `tools.tool_briefing_manager._providers` の確認
   → 期待 7〜8秒
3. **赤いときだけ隔離して再検査**。バッチが失敗を報告したモジュールだけ、従来どおり
   個別サブプロセスで再実行し、正確な traceback を得る
   → 緑の常態は7秒、赤いときだけ「失敗件数 × 約5秒」
4. **`sys.path` 汚染をモジュール毎にスナップショット＆復元**して報告
5. **回帰テスト `tests/test_guardian_batch_import.py` を新設**。上表5ケースを固定する

---

## 踏んではいけない罠 (すべて実測・調査済み)

- **並列化に逃げない。** 実測で4倍負ける (上記)
- **`sys.path` 汚染検出をハードエラーにしない。** 正当な `__file__` 相対の挿入が
  リポジトリルートを追加するため、全部を違反にすると誤検知だらけになる。
  実例 (いずれも正しいコード): `tools/tool_user_memory.py:25`, `tools/tool_xtrend.py:29`,
  `tools/tool_vault_index.py:30`, `tools/tool_activity_index.py:45`,
  `tools/tool_browser_use.py:28`, `lib/engagement_log.py:40`。
  → **「リポジトリルート以外の追加」だけを報告する**
- **バッチ化は思想的な後退ではない。** `check_import()` の docstring は
  「現在のプロセスを汚さず」と書いているが、**`[4]` はすでにこれを破っている**:
  `verify_project.py:457` が `importlib.import_module()` で Guardian 自身のプロセスに
  import して `hasattr` を見ている。つまり各レジストリモジュールは今、サブプロセスと
  インプロセスで**二重に** import されている。docstring も実態に合わせて直すこと
- **`except BaseException` で受ける。** `Exception` だと `SystemExit` を取り逃す。
  2026-07-30 に直した `bot_discord.py` の import 時 `sys.exit(1)` が再発したとき、
  検査が黙るのが最悪のパターン
- **`subprocess` には必ず `encoding='utf-8', errors='replace'`。**
  `text=True` だけだとロケール既定で復号し、cp1252 の CI runner で
  **失敗の理由だけが消える** (2026-07-30 に実際に踏んだ。skill `windows-encoding` 参照)
- **hook は `--fast` のまま。** フルが10秒になっても hook をフルに切り替えないこと。
  コミット毎に1秒→10秒は10倍の後退で、「気軽に細かくコミットする」という
  行動変化を潰す。効くのは「リリース前のフル検証」と「CI」が3分→10秒になること
- **「表示と実態の食い違い」を作らない。** バッチが緑を返す条件を明示的にテストする。
  検査が空回りしていても「違反0件」と同じ出力になる (このリポジトリの署名的欠陥)

---

## Verification (必須)

- [ ] **変更前**に `Measure-Command { py verify_project.py }` を記録 (基準 164.69秒)
- [ ] 変更後 `py verify_project.py` が **exit 0 かつ 20秒未満**
- [ ] `py verify_project.py --fast` が **exit 0 かつ 2秒未満** (退行していない)
- [ ] **わざと壊して赤くなることを確認**:
      - 存在しないモジュールを `critical_modules` に足す → exit 1・モジュール名が出る
      - `main.py` の TOOLS の `function` を存在しない関数名に書き換える → exit 1
      - import 時に `sys.exit(1)` するモジュールを足す → exit 1・**後続も検査される**
      - いずれも確認後に必ず元へ戻す
- [ ] 失敗時に**隔離再検査が走り、traceback が読める**こと (理由が空でない)
- [ ] `tests/test_guardian_batch_import.py` が 5ケース緑
- [ ] `tests/test_verify_fast_mode.py` (29チェック) が緑 — `--fast` の守備範囲を壊していない
- [ ] `tests/test_diagnostic_subprocess_encoding.py` が緑 — `encoding=` を落としていない
- [ ] `tests/test_*.py` 全件スイープが **0 FAIL** (基準 70/0・約460秒)
- [ ] PR を作って **CI の guardian / tests が両方緑** (`continue-on-error` は無い)

## Rollback

`verify_project.py` 単体の変更なので `git revert <sha>` で戻る。
段階的にコミットする (①重複除去 → ②バッチ化 → ③隔離再検査 → ④回帰テスト) と
どこで壊れたか切り分けやすい。

---

## 依存・注意

- 並行して別セッションが `lib/` `tools/` のルート直書き (load_dotenv・learning_db・
  コストガードのカウンタ等) を `__file__` 相対に直している。**ファイルは衝突しないが**、
  あちらは module-level の `load_dotenv` を触るので import 挙動に関わる。
  マージ順が前後したら `py verify_project.py` を再測すること
- `--delete-branch` は使わない (CLAUDE.md の既知バグパターン: stacked PR が自動クローズされる)
- 関連する既知バグパターン: CLAUDE.md 本体 (import 時に `sys.exit()` するな /
  CI と実機が一致しないとき疑うのは実機側) と skill `windows-encoding`
  (`subprocess` の `encoding=`)
