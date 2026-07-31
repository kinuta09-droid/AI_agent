# テストスイープを 476秒 → 80秒前後にする

> **実行済み (2026-07-31・ブランチ `perf/test-sweep-parallel`)。**
> 実測: 直列 **484.9秒** → ③適用後 直列 **305.4秒** → ④8並列 **89.1秒** (FAIL 0 / 71本)。
> 計画との差分:
> - **④ で `ThreadPoolExecutor` を使えなかった。** `tests/test_parallel_execute.py`
>   の [15] が使用箇所を3ファイルに限定しており、走査対象に `scripts/` が入っている。
>   Popen をポーリングする形に変更 (親は CPU 仕事をしないのでスレッドは不要)。
>   さらにこの [15] は**文字列走査**だったため、「なぜ使わないか」を書いた docstring
>   自体が違反として報告された → ast へ書き換えた (CLAUDE.md の既知の類型)。
> - **直列レーンは3本になった。** 判明済みの2本に加え、
>   `test_diagnostic_subprocess_encoding` も `tests/` に一時ファイルを書くので隔離した。
> - 並列可否は `scripts/run_test_sweep.py` に宣言を置き、未分類は直列へ倒す。
>   分類漏れは `tests/test_sweep_runner.py` [1] が赤にする。
> - ⑤ (`lib_shared` の `google.genai` 遅延 import) は**未着手**。別 PR のまま。

## このセッションの始め方

```
cd C:\AI_agent
git checkout -b perf/test-sweep-parallel
```
Claude Code を起動し、Plan Mode に入って最初にこう指示する:
> `plans/test-sweep-speedup.md` を読んで、Plan Mode で計画を立てて

**計測はもう終わっている。** 下記の実測値を出発点にしてよい。

---

## Goal

`tests/test_*.py` 全件スイープの所要時間を **476.6秒 → 80秒前後** にする。
ただし **CI の色を信じられる状態を壊さない**ことを最優先とする
(たまに赤くなる CI は、遅い CI より悪い)。

## 前提 — 先に片付けた分 (このプランの対象外)

ブランチ `fix/test-suite-api-leaks` で以下を済ませてある。**やり直さないこと。**

| 直したもの | 内容 |
|---|---|
| `tests/test_create_note.py` | 本番 Vault (`D:\マイドライブ\Obsidian`) に実書き込みし、実 Gemini を呼んでいた。一時 Vault + スタブで密閉化。**実 Vault に TestNote が19件溜まっていた**原因 (同名衝突時のタイムスタンプ退避 = スイープ1周ごとに1件増える) |
| `tests/test_grounding.py` | 実 Gemini + Google Search をゲート無しで呼び、`grounding_test.txt` をリポジトリ直下に書いていた → `ARMS_LIVE_TESTS=1` の後ろへ、出力は一時ディレクトリへ |
| `tests/test_curate_note_save.py` | `dotenv` をスタブするため `cost_guard` が `.env` を読めず既定上限200に落ちる一方、カウンタは**本番の** `logs/gemini_daily_calls.json` を見ていた。結果**本番カウントが200を超えた時刻以降だけ赤くなる** (朝は緑・夜は赤)。日次カウンタを一時ファイルへ隔離 |

**この3件を直すまで、並列化の実測は信用できなかった** — 12/16並列で出た唯一の失敗は
並列起因ではなく、このカウンタ起因だった (後述)。

---

## Context — 476秒の内訳 (2026-07-30 実測)

| 帯 | 本数 | 合計 | 比率 |
|---|---|---|---|
| 20秒以上 | 3 | 185.2s | **38.9%** |
| 10-20秒 | 2 | 33.2s | 7.0% |
| 5-10秒 | 26 | 154.4s | 32.4% |
| 2-5秒 | 24 | 98.0s | 20.6% |
| 2秒未満 | 15 | 5.7s | 1.2% |

遅い順: `test_bitnet_new_features` 76.16s / `test_bitnet_practical` 72.60s /
`test_bitnet_preprocessing` 36.40s / `test_vault_semantic_smoke` 16.64s /
`test_scan_crypto` 16.55s / 以下 8.6s 未満。

固定費: `py -c pass` **0.04秒** に対し `import lib.lib_shared` **3.78秒**。
内訳 (`-X importtime`): `google.genai` 3.11s — うち `google.genai.types` 2.02s、
**`mcp` サーバスタック 1.39s**、`jsonschema` 1.05s、`rfc3987_syntax` 0.94s。
つまり中位帯50本(252秒)の大半は「自分のロジックに到達する前の固定費」。

### Guardian とは答えが逆になる (最重要)

| | タスクの性質 | 効く手 |
|---|---|---|
| Guardian (`plans/guardian-import-batching.md`) | 「import できるか」→ **1プロセスに畳める** | **バッチ化** (7s vs 並列30s) |
| スイープ | 「1ファイル1プロセスで終了コードが判定」→ **畳めない** | **並列化** |

畳めない理由は CLAUDE.md が `pytest tests/` を禁じている理由そのもの:
各ファイルの `sys.exit(1 if _FAIL else 0)` が判定なので、1プロセスにまとめると
最初の `sys.exit` で全部終わる。**畳めない = import コストを共有できない**ため、
Guardian で4倍負けた並列化が、ここでは正解になる。

**共有ワーカープール (1プロセスで複数テストを `runpy` する) は採らない。**
テストは意図的にグローバル状態を壊す (`sys.modules` 差し替え、`os.environ` 書き換え、
`sys.stdout.reconfigure`、モジュール定数の再束縛)。1プロセス1ファイルを守っているから
それが安全でいられる。共有すると「A の後だと B が通る」が生まれ、Session 3 で
潰したのと同じ「開発機だけ緑」を作り直すことになる。

### 並列化の実測

| 構成 | wall | FAIL |
|---|---|---|
| 直列 70本 (現状) | 476.6s | 0 |
| BitNet 3本を除いた直列 67本 | 291.4s | 0 |
| 8並列 (67本) | **77.6s** | 0 |
| 12並列 (67本) | 65.2s | 1 ← カウンタ起因。並列起因ではない |
| 16並列 (67本) | 63.8s | 1 ← 同上 |

論理コア16。8→12で12秒しか縮まないのは、固定費が CPU バウンドで
物理コア数に律速されるため。

---

## Steps

### ③ BitNet 3本を opt-in の後ろへ (-185秒 / 38.9%)

3本ともローカル LLM 推論なので **これは固有の計算量**であり、速くはできない。
ただし2本 (`practical` / `preprocessing`) はアサーションを持たない計時ベンチで、
`new_features` も TEST 1 (`quick_filter_discord`) 以外はベンチ。

- 新しい env ゲート (`ARMS_SLOW_TESTS=1` 等) の後ろに置き、既定は SKIP + exit 0
- **`ARMS_LIVE_TESTS` と混同しないこと。** あちらは「課金・外部副作用があるもの」、
  こちらは「遅いだけで無料・無副作用のもの」。同じ変数にまとめると、
  課金ゲートを緩めたい時に遅いテストも一緒に走ってしまう
- **`new_features` の TEST 1 は BitNet 不要なので必ず残す** (既に前提ゲートで
  そういう構造にしてある。壊さないこと)
- 既存の「前提が無ければ SKIP」(= `C:\BitNet` 不在) は別の話。**両方必要**:
  CI は前提不在で SKIP、開発機は opt-in 未指定で SKIP

### ④ 8並列にする (291秒 → 78秒)

- ランナーは `Get-ChildItem | ForEach-Object` の逐次から並列へ。
  対象は **手元スイープと `.github/workflows/tests.yml` の両方**
  (片方だけ直すと「CI と実機の失敗一覧が一致するか」の切り分けが崩れる)
- **並列度は 8 で止める。** 12以上は12秒の追加しか得られず、隠れた共有状態を
  掘り起こすリスクに見合わない
- **並列不可のテストを明示的に宣言する。** `lib/tool_registry` の
  `parallel_safe` 宣言と同じ作法 (skill `parallel-execution-safety` に前例)。
  推測で並列に投げない
- 判定は従来どおり **終了コードのみ**。並列化で出力が混ざるので、
  ファイル名と exit code の対応が読める形で集約する

#### 現時点で判明している並列不可ペア

`tests/test_verify_fast_mode.py` ↔ `tests/test_hook_guards.py`

前者は `tests/` に**構文エラーのファイルを置いた状態で** `verify_project --fast` を
走らせる。後者は `guard_git_commit` 経由で同じ `verify --fast` を走らせる。
`verify` は `tests/` を走査するので、窓が重なると後者が誤って赤くなる。

**実証済み** (窓を人工的に開いたまま実行):

```
[窓を閉じた] exit=0 test_hook_guards.py   exit=0 test_verify_fast_mode.py
[窓を開いた] exit=1 test_hook_guards.py ❌ exit=1 test_verify_fast_mode.py ❌
```

8並列で FAIL 0 だったのは**窓が約1秒しかなく偶然当たらなかっただけ**。
→ この2本は直列レーンに隔離する (実測: 隔離すると +13.8秒)

---

## 踏んではいけない罠

- **「並列で1回緑だった」を安全の証拠にしない。** 上の実証のとおり、これは競合。
  緑が出ても「今回は当たらなかった」以上の意味はない。並列不可の判定は
  **共有する資源を列挙して**行う (ファイルパス・PIDファイル・カウンタ・env)
- **12/16並列で落ちた `test_curate_note_save` を「並列バグ」として追わない。**
  真因は本番共有の日次カウンタで、`fix/test-suite-api-leaks` で解消済み。
  並列度を上げる前にそのブランチを取り込むこと
- **`ARMS_SLOW_TESTS` と `ARMS_LIVE_TESTS` を統合しない** (上記)
- **CI 側にだけ並列化を入れない。** 手元と CI で実行方法が違うと、
  Session 3 でようやく成立させた「失敗一覧の一致で CI 固有問題を切り分ける」が壊れる
- **共有ワーカープールに手を出さない** (理由は上記「Guardian とは答えが逆」)
- スイープが**本番の資源に触っていないこと**を毎回確認する。
  `logs/gemini_daily_calls.json` のカウント差分と、
  `D:\マイドライブ\Obsidian\30_Main` のファイル数差分が **どちらも 0** であること

---

## Verification (必須)

- [ ] **変更前**に直列スイープを実測 (基準 476.6秒 / 70本 / FAIL 0)
- [ ] ③ 適用後: 直列で **291秒前後**・FAIL 0・BitNet 3本が SKIP で exit 0
- [ ] ③ の逆方向: `ARMS_SLOW_TESTS=1` を付けると BitNet 3本が**実際に実行される**
      (ゲートを付けて永久に走らなくなる、が最悪のパターン)
- [ ] ④ 適用後: 8並列で **80秒前後**・FAIL 0
- [ ] ④ の要: **並列でも直列と同じ結果になる**ことを確認する。
      わざと1本壊して、並列ランナーがその1本を FAIL として報告し
      **全体の終了コードが 1 になる**こと (壊した後は必ず戻す)
- [ ] 並列不可ペアの隔離が効いていること: `test_verify_fast_mode` と
      `test_hook_guards` を**同時に**投げて100回程度回しても赤が出ない、
      または直列レーンに分離されていることをコードで保証
- [ ] 本番副作用ゼロ: Gemini カウント差分 0 / `30_Main` ファイル数差分 0
- [ ] `py verify_project.py` (フル) が緑
- [ ] PR を作って **CI の guardian / tests が両方緑** (`continue-on-error` は無い)

## Rollback

ランナーの変更 (`tests.yml` + 手元スクリプト) とテスト側のゲート追加は
別コミットにする。並列化で不安定になったら ④ のコミットだけ revert すれば
③ の -185秒は残る。

---

## ⑤ 別フェーズ: `lib_shared` の `google.genai` 遅延 import

**これは別 PR にすること。** 効果は大きいがコアモジュールの変更で、
テストだけでなく Bot 起動・全ツールに影響する。

- 現状: `lib/lib_shared.py:19` が `from google import genai` をモジュール直下で行い、
  `:44` で `genai.Client(...)` を生成する。これが1プロセスあたり 3.11秒
- 遅延化できれば固定費が 3.78 → 0.7秒程度に落ち、④ 後の80秒がさらに縮む見込み。
  Bot / Scheduler の起動も速くなる
- 注意: `client` はモジュール属性として広く参照されており
  (`lib_shared:302-308` が `client.models.generate_content` を予算チェックで包む、
  テストが `ls.client` を差し替える等)、遅延化は**この包み込みの順序**を壊しやすい。
  遅延プロキシにするなら、包み込みがプロキシ越しでも効くことを必ず実測する
- `mcp` (1.39s) と `jsonschema` (1.05s) は `google.genai` の内部依存なので
  こちらからは削れない。遅延化以外に手は無い

---

## 依存・注意

- 並行して2つのセッションが動いている (2026-07-30 時点):
  `lib/` `tools/` のルート直書き修正 と `gws`/MCP プローブの読み取りタイムアウト。
  後者は `tests/test_gws_mcp.py` / `tests/test_mcp_create.py` を触るので、
  **④ の並列不可リストを書くときに衝突しないよう、先にそちらを取り込む**
- `--delete-branch` は使わない (CLAUDE.md の既知バグパターン)
- 関連: `plans/guardian-import-batching.md` (逆の結論になる理由がそこに書いてある)、
  skill `parallel-execution-safety`、skill `windows-encoding`
  (`.ps1` は BOM 無し UTF-8 で保存すると PowerShell 5.1 が cp932 で読んで壊れる —
  並列ランナーを `.ps1` で書くなら必ず踏む)
