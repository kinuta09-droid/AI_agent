## Review Protocol
- After implementation, run `/grill` to trigger adversarial self-review
- Verify behavior diff against main branch when applicable
- Run relevant `test_*.py` files before committing

### CI (GitHub Actions)
`.github/workflows/tests.yml` が push/PR で自動実行される (`windows-latest`)。
**`**.md` だけの変更では起動しない** — この skip は「追跡下の `.md` を読む Python が0件」という
前提の上に乗っており、`tests/test_ci_docs_exemption.py` が前提そのものを ast で縛っている。
手動で全部走らせたいときは workflow_dispatch。
- **ジョブは `checks` 1つだけ**。以前の guardian / tests の2ジョブでは 355 job秒のうち
  **165秒 (46%) が二重に払った段取り** (checkout + setup-python + pip) だった → 統合で約274 job秒 (-23%)。
  **`needs:` の段階ゲートは採らない** — 直列化しても job 秒は減らず wall だけ +101秒で、
  実績も 26 run 中 guardian のみ赤は1件しかない
- `guardian (verify_project.py)` / `tests (...)` の2ステップ (どちらもブロッキング)。tests には
  `if: ${{ !cancelled() }}` を付けて **guardian が赤でも必ず走らせる** — 失敗一覧が片方だけになると
  「CI と実機の一致」で切り分ける手順が使えない。`always()` ではないのは、PR の run を
  キャンセルしたときに走らせないため
- **手元と同じ `py scripts/run_test_sweep.py` を呼ぶ**。1ファイル1プロセスで実行し終了コードで判定。
  **`pytest tests/` では判定しない** — 多くのファイルの `check()` ヘルパーは失敗時も例外を投げないため、
  pytest 経由だとチェックが全部failしていても緑になる (Known Bug Patterns A そのもの)。
  **実行方法は1つしか持たない** — CI と手元で別々に書くと切り分け手順が壊れる
- 並列可否は `scripts/run_test_sweep.py` の `SERIAL_TESTS`/`PARALLEL_TESTS` で **宣言する**。
  未分類は直列レーンへ倒れる (安全側) が、`tests/test_sweep_runner.py` が未分類の存在を赤にする
- 遅いだけのベンチは `ARMS_SLOW_TESTS=1`、課金・外部副作用のあるものは `ARMS_LIVE_TESTS=1`。
  **この2つを統合しない** — 課金ゲートを緩めたいときに遅いベンチまで走ってしまう

### Backtest Gate (crypto logic)
- `lib/quant_core.py` / `tools/tool_crypto.py` の変更は **before/after バックテスト比較が必須**。
  詳細な規約と既知バグパターンは skill `crypto-backtest` を参照 (該当ファイル編集時に自動ロード)

### Grill Me Rules
1. **Before PR**: Claude must challenge every change — "Why is this necessary?"
2. **Edge cases**: Identify at least 3 edge cases per function change
3. **Evidence**: Show diff or execution results to prove correctness
4. **Bug fix re-implementation**: After fixing a bug, ask: "Now discard this fix and implement the most elegant solution from scratch"
5. **No mercy**: Do not approve changes with TODO/FIXME left behind
6. **Skill Recommendations**: 変更内容に応じて `antigravity-awesome-skills` (`@architecture`, `@test-driven-development` 等) や `gws_cli` (`gws-*`, `recipe-*`) を強く推奨する
