# Antigravity AI Agent - Project Rules

## Architecture (Modular)
- **Root**: `main.py` (entry), `bot_discord.py` (Discord host), `agent_hello.py` (compatibility bridge)
- **`commands/`**: Bot logic split by feature (Curator, Calendar, News, etc.)
- **`lib/`**: Core utilities, shared clients, and obsidian helpers.
- **`tools/`**: External API integrations and specialized tools.
- **`tests/`**: Unit tests and debugging stubs.
- **`antigravity_env/`**: Dedicated Python virtual environment.
- **`scripts/`**: One-off scripts and data processors.

## Tools
| File | Purpose |
|------|---------|
| `tool_curator.py` | The Explorer v4.0 "Entropy Injector" (3-slot curation + Tavily trends) |
| `tool_calendar.py` | Google Calendar + Todoist integration (5 tools) |
| `tool_daily_note.py` | Daily note auto-generation from template |
| `tool_lora_factory.py` | LoRA/ComfyUI image generation |

## Coding Standards & Stability
- **Python**: PEP8. Always `# -*- coding: utf-8 -*-` and explicit `encoding='utf-8'` on file I/O.
- **Imports**: **MANDATORY** use of absolute package paths (e.g., `from lib.lib_shared import ...`). Never use local imports that skip the package prefix.
- **Guardian**: Run `py verify_project.py` before any significant commit or bot restart.
- **Paths**: `os.path.join()` or raw strings with `\\`. Never forward slashes on Windows file ops.
- **Launcher**: Use `py` (not `python`) on this machine.
- **Secrets**: `.env` for all API keys. Never commit `.env`, `token.json`, `credentials.json`.

## Commands
```bash
py verify_project.py           # Pre-flight check (MANDATORY before release)
scripts\arms.bat               # ワンクリック制御 (引数なし=メニュー / start|stop|restart|status|logs)
py main.py                     # Universal launcher
py bot_discord.py              # Start Discord bot
py agent_scheduler.py          # Start scheduler
py n8n_patrol.py               # Patrol n8n workflows
py agent_trigger.py            # Trigger webhook
```

## MCP Servers
`.mcp.json` (プロジェクトスコープ・コミット済み) に2つ登録済み。初回は `claude` 起動時に承認プロンプトが出る:
- `context7`: ライブラリの最新ドキュメント参照 (`todoist-api-python` v3+ 等のバージョン差異バグの予防)
- `chrome-devtools`: `tool_browser_use.py` / `tool_xtrend.py` のセレクタ崩れを実ブラウザで確認 (要 Node.js 22+)

GitHub MCP は個人の PAT が要るため `.mcp.json` に含めていない。使う場合は各自:
```bash
claude mcp add --transport http github https://api.githubcopilot.com/mcp/ --header "Authorization: Bearer YOUR_PAT"
```

**Agent Reach は MCP ではない** (よく「Agent Reach MCP」と呼ばれるが誤り)。本体は CLI +
Agent Skill で、同梱の MCP サーバーが公開するツールは `get_status` 1個だけ・かつ mcp SDK 2.x
ではクラッシュするため `.mcp.json` には登録していない。導入手順と落とし穴は
`docs/agent-reach.md` を参照 (`pipx install git+https://github.com/Panniantong/agent-reach.git`
→ `agent-reach skill --install`。Skill はユーザースコープなのでチェックアウト毎に要再実行)。

## Skills (領域別の詳細な既知バグパターン)
`## Known Bug Patterns` は**プロジェクト全域に効く教訓のみ**を残し、特定領域の詳細は `.claude/skills/` に分割した。該当領域のファイルを触るときは Claude が自動でロードするが、新しい教訓を追記するときは **どの skill に属するか判断してからそこに書く** — Known Bug Patterns 本体に無闇に追記して肥大化させない。

| Skill | 対象領域 |
|---|---|
| `windows-encoding` | .bat/.ps1/.vbs、cp932/BOM、ランチャー起動、subprocess/stdout エンコーディング |
| `process-lifecycle` | pid_guard、boot_id、watchdog、タスクスケジューラ、Windows プロセス終了 |
| `retrieval-tuning` | Vault ハイブリッド検索、リランク (Gemini/Ollama)、ツール説明文、eval_retrieval/eval_agent |
| `crypto-backtest` | lib/quant_core.py、tools/tool_crypto.py のシグナル/ゲートロジック |
| `llm-output-safety` | Verifier、プロンプトインジェクション対策、判定器設計、LLM出力のJSONパース |
| `discord-bot-patterns` | discord.ui.View の永続化、interaction token、失敗プレフィックス判定 |
| `parallel-execution-safety` | engine_plan_execute.py の fan-out、parallel_safe 宣言、副作用の順序依存 |

## Known Bug Patterns (プロジェクト全域・常時適用)
- `tool_create_note` fails on duplicate filenames -> use timestamp suffix fallback
- `todoist-api-python` v3+: use `filter_tasks(query=)` not `get_tasks(filter=)`
- **bot層で `except Exception as e:` して `Error: {e}` を Discord に返すだけにしない**。ログに一行も残らないため、壊れていても誰も気付けない (これが /curate の NameError を5ヶ月隠した真因。バグ本体より罪が重い)。しかも discord.py は未捕捉のコマンド/View 例外を自前ロガーで stderr に出すので、**この形の except は書かないほうがマシ**。必ず `lib.bot_errors.log_command_error(where, e, prefix)` を通す (2026-07-26 に bot 層28箇所を一括置換)。`verify_project.py` の `check_swallowed_exceptions()` が再混入を止める。`tools/` の `return f"Error: {e}"` は呼び出し元への戻り値契約なので対象外
- **bot層・スケジューラの見出しに `✅` をハードコードしない**。ツール層は例外を投げずに `return f"Error: {e}"` を返す規約なので、「例外が出なかった＝成功」と決めつけると失敗が ✅ で表示される。判定は `lib.bot_errors.is_failure_result()` / `FAILURE_PREFIXES` に一元化する (2026-07-26 に5箇所を一掃: `/agent`・`/chat agent`・深掘りボタン・定時ジョブ完了通知・`_summarize_results`)。**同じ結果を出す入口は必ず1つの関数を共用する** — `/agent` だけ直して `/chat agent` を取り残したのは、入口ごとに見出しを書いていたため。`tests/test_result_icons.py` の静的スキャン (近傍に ❌ 分岐が無い ✅ 見出しを違反とする) が再混入を止める
- **ツール層の Error 文字列を「例外の有無」だけで成否判定しない**。同じ穴が bot 層 (2026-07-26) の次に **エンジン中核** で見つかった: `engine_plan_execute.execute_plan()` が `try: observation = observe(...); success = True` と書いていたため、ツールが規約どおり `return f"Error: {e}"` を返しても `success=True` になり、(a) Critic に成功として渡り、(b) `log_execution` が `outcome="success"` で learning_db に記録し、(c) `✅ Mission Complete` が出ていた。`Agent_Performance.md` の成功率はこの分だけ水増しされていた。判定は必ず `lib.bot_errors.is_failure_result()` を通す (2026-07-27)。**表示層を直したら、その表示に値を渡している層も同じ基準で洗う** — 見出しだけ直しても、学習DBに残る数字は嘘のままになる
- **「例外が出なかった＝成功」を潰したら、その上の "戻り値を組み立てる層" も見る**。`execute_plan` の成否判定を直しただけでは `/agent` の表示は ✅ のままだった: `run_plan_and_execute` が「修正回数を使い切った」「Critic が ok と言った」経路で無条件に `_generate_final_summary()` (Gemini の要約) を返しており、要約は失敗を説明する文章であって失敗接頭辞を持たないため。結果、learning_db は `failed`・Discord は `✅ Mission Complete` という食い違いが残る。`_final_report()` で「成功ステップが1件も無い実行は `タスク失敗:` を返す」を一箇所に集約した (`execute_plan` は最初の失敗で break するので、成功0件＝どの修正計画も1歩目から進めなかった、と判断してよい)。**自己申告の要約を最終判定に使わない**
- **「成功」「失敗」の2状態しか無い見出しは、部分失敗のときに必ず嘘をつく**。全滅ケース (`タスク失敗:` を返す) だけを直した時点では、3ステップ中2つ成功・保存だけ失敗のような実行が `log_execution` では `outcome="partial"` なのに Discord には `✅ Mission Complete` で出ていた。`_final_report` が「成功が1件でもあれば」無条件に Gemini 要約へ落ちるため。**部分失敗に失敗接頭辞は付けられない** (成功が混ざっているので `タスク失敗:` は嘘になる) ので、第3の印 `lib.bot_errors.PARTIAL_PREFIX` + `is_partial_result()` を足し、`mission_header` を `✅ / ⚠️ / ❌` の3状態にした (2026-07-27)。**不変条件は「outcome != success ⇒ 見出しが ✅ でない」**と書く (「⇒ ❌」と書くと partial で過剰判定になる)。Gemini の要約は失敗に触れるとは限らないので、失敗したステップ番号とツール名は要約とは別に本文へ明示する。回帰は `tests/test_result_icons.py` の [12]
- **失敗接頭辞は tuple では表しきれない**。実際に `tools/` を洗うと `Error creating note:` / `Error searching web:` (動詞が挟まる)、`[30_Main] Error merging reports:` (先頭に [タグ])、`[ERROR] ...` (gmail/vault_index/future_search)、`[xtrend error] ...` (`lib/tool_registry` の lazy wrapper 4種) と揺れており、`FAILURE_PREFIXES = ("Error:", "Error (")` では大半を取りこぼす。`lib.bot_errors._TOOL_FAILURE_RE` に集約し、新しい形はそこにだけ足す。ただし **`\b` は使わない**: Unicode では日本語が単語文字なので `Error\b` が「Errorハンドリングの調査結果」に一致してしまう -> `Error(?![^\W\d_])` (直後が文字でない) を使う。回帰は `tests/test_result_icons.py` の [1] (実在の失敗形13種 + 誤検知させてはいけない正常系5種) と [9][10] (execute_plan / log_execution)
- **合否を判定する関数では、説明文を合否と無関係に組み立てない**。「表示が実態と食い違う」類型は、表示層 (bot 見出し)・戻り値層 (`_final_report`)・記録層 (`log_execution`) の次に **判定器そのもの** で見つかった: `scripts/eval_agent._check(checks, name, ok, detail)` が detail を合否に関わらず格納していたため、`ok=true` なのに detail が「回答に一致なし: ...」「順序が逆」「失敗が成功として表示される」となり、`logs/eval/*.json` を読む人が合格を失敗と読み違える (2026-07-28)。ただし **全 detail を「失敗時のみ」にするのは誤り** — `max_steps` の「実際は 2 ステップ (['tool_update_note'])」のように合格時にも読む価値がある事実が消える。引数を **`detail` (合否に関わらず残す事実) と `fail_detail` (合格時は捨てる)** に分け、呼び出し側に「これは失敗時にしか意味を成さない文か」を宣言させる。回帰は `tests/test_eval_agent.py` の [15] (合格時 detail が空 + 逆方向: 実測値は残る) と [16] (**ast で `_check` の呼び出しを走査**し、detail 引数に失敗語彙があれば落とす)。動的テストだけだと「私が書いたシナリオを実行する呼び出し側」しか守れず、新しい呼び出し側の再混入に気付けない
- **リンクを「出す側」と「解決する側」は必ず対で直す**。検索結果の出典を `[[ファイル名]]` から `[[ファイル名#見出し]]` にした瞬間、`lib_obsidian_utils.get_obsidian_uri_from_link` が `foo.md#見出し.md` を探して必ず空文字を返すようになり、Discord の「Obsidianで開く」ボタンが**無言で消える**ところだった (`obsidian://open` の `file` パラメータは見出しを解釈しない)。ボタンが出ないだけでログには一行も残らない = 所見2と同じ無言の死。`format_wikilink` (出す) と `split_heading_anchor` (解決する) を同じモジュールに並べて置き、片方だけ変えられないようにする。**リンク形式を変えるときは `\[\[(.*?)\]\]` を検索している箇所を先に洗う** (2026-07-28)
- **stacked PR の base ブランチを `--delete-branch` 付きでマージすると、積んでいた PR が自動クローズされる**。閉じた PR は base の変更も再オープンもできない (`Cannot change the base branch of a closed pull request`) ので作り直しになる。**stack しているときは `--delete-branch` を後回しにする**
- **「表示と実態の食い違い」はテスト層とCI設定でも起きる**。bot層 → engine → 最終要約 → 採点器と潰してきた同じ類型が、次は **テスト自身** で見つかった (2026-07-30)。(a) `tests/test_moc_auto_link.py` 等は判定結果を print するだけで **終了コードに反映していなかった**ため、全ケース FAIL でも exit 0 = CI からは永久に緑。`tests.yml` が「スクリプトとして実行し終了コードで判定する」設計なのに、その終了コードが嘘をついていた。(b) `tests.yml` の `PYTHONPATH: ${{ github.workspace }}` が、`sys.path` bootstrap の欠陥 (`dirname` が1段足りず `tests/` 自身を追加している3ファイル + bootstrap が全く無い1ファイル) を丸ごと隠しており、**実機で ImportError になるテストが CI では通っていた**。「CI の失敗一覧が実機と一致するか」で切り分ける手順が原理的に成立しなくなる。→ 各テストは自前で `sys.path.insert(0, dirname(dirname(abspath(__file__))))` し、CI 側に下駄を履かせない。**判定は必ず `sys.exit(1 if _FAIL else 0)` まで通す**
- **「CI と実機の失敗一覧が一致しない」とき、疑うのは CI ではなく実機側の下駄**。CI の `PYTHONPATH: ${{ github.workspace }}` を外して「今度は一致するはず」と予測した直後の CI 初起動が、実機 69/0 に対し **58/11** だった (2026-07-30)。11件のうち6件は `sys.path.insert(0, r'C:\AI_agent')` という**ルートの直書き**で、開発機ではリポジトリが実際にそこにあるので永久に緑になる。つまり CI 側の下駄を外しても、**同じ下駄がテストファイルの中に移っていただけ**。ローカルの緑は「このマシンでは動く」以上を意味しない。`verify_project.py` の `check_machine_locked_syspath()` が再混入を止める (純 ast なので `--fast` にも乗り commit hook で捕まる)。**検査は文字列走査ではなく ast で書く** — この欠陥を説明する docstring/コメント自体が `sys.path.insert(0, r'C:\AI_agent')` を含むので、grep 的な検査は「教訓を書いた行」を違反として報告する。回帰は `tests/test_verify_fast_mode.py` の [3] (違反を置くと赤 + 教訓コメントは緑の両方向)
- **import 時に `sys.exit()` してはいけない**。`bot_discord.py` が module scope で `if not DISCORD_TOKEN: sys.exit(1)` していたため、`.env` を持たない環境では `import bot_discord` そのものが失敗し、(a) `verify_project.py` の import 整合性検査が原理的に成立せず、(b) コマンド定義を読むだけのテストまで巻き込まれて落ちた。**起動しようとした時に初めて落ちる** (`run_bot()` の先頭で判定する)。import は副作用でプロセスを殺さない
- **「下駄を外した」と言えるのは sys.path だけ見たときではない**。2026-07-30 に `sys.path.insert(0, r'C:\AI_agent')` を __file__ 相対へ直し `check_machine_locked_syspath()` で門を作ったが、**同じルート直書きが「実行時に読み書きするパス」に21箇所残っていた** — `learning_db.json` / コストガードの日次カウンタ4種 / OHLCVキャッシュ / `token.json`・`credentials.json` / `load_dotenv()` 12箇所。検査が sys.path 経路しか見ていないので検出対象外だった。**症状の重さは逆**: sys.path の直書きは ImportError で派手に落ちるが、データパスの直書きは**落ちずに間違ったファイルを読み書きする** (別チェックアウトが本番の learning_db やコストガードのカウンタを更新すれば、学習DBの統計もコスト上限も静かに嘘になる = 所見2の「無言の死」と同型)。→ `lib/paths.py` に `PROJECT_ROOT`/`ENV_PATH`/`LOGS_DIR` を集約し、**絶対パスのリテラルを書いてよいのはこのファイルだけ**にした。`verify_project.check_hardcoded_abs_paths()` が再混入を止める (純 ast なので `--fast` に乗り commit hook で捕まる)。誤検知を避けるため **「パスを組み立てている文脈」(os.path.join/load_dotenv/open の引数、語尾が `_PATH`/`_DIR`/`_ROOT`/`_FILE` の代入) に限定する** — 全リテラルを赤にすると、教訓を書いた docstring・`scripts/search_dev_logs.py` の「移行の痕跡を探す検索語リスト」・唯一の宣言場所である `lib/paths.py` 自身が巻き添えになり、検査ごと外される方向に力が働く。回帰は `tests/test_verify_fast_mode.py` の [5](門に配線されているか)[6](書き方8種の網羅 + 誤検知5種)[7](検査ではなく実体側 `lib/paths.py` を縛る)
- **リポジトリ外の絶対パス (Vault / BitNet) は `.env` 上書き + 既定値=現行値 の形で `lib/paths.py` に置く**。`D:\マイドライブ\Obsidian` (Googleドライブ同期) と `C:\BitNet` は __file__ 相対では表せないので「直書きを全廃する」ことはできない。かといって各ファイルに散らすと、Vault を移した瞬間に**このファイルだけ旧パスを見る**分裂が起きる (実際 `tool_daily_note.py`・`tool_calendar.py`・`trigger_daily_news.py`・`agent_trigger.py` の4箇所に同じリテラルが複製されていた)。`OBSIDIAN_VAULT_ROOT` / `BITNET_DIR` で上書き可・未設定なら従来値にフォールバック、とすれば **既存環境は .env を1行も足さずに挙動不変**のまま、移行時は1行で済む。**リテラルを「消す」のではなく「1箇所に集めて宣言にする」** のが外部パスの正解。なお `tools/tool_lora_factory.py` は旧Vault (`C:\obsidian\Obsidian\...`) を指したままで、書き込み先が変わる変更になるため据え置き — `verify_project._ABS_PATH_EXEMPT` に理由付きで登録してある (**新規追加禁止**)
- **`__file__` 相対にすると「チェックアウト毎に .env が要る」ようになる**。ルート直書きの時代は worktree や別クローンからでも `C:\AI_agent\.env` を読めていたので、新しい worktree でいきなり `GOOGLE_API_KEY not found` で死ぬようになる (`lib_shared` は import 時に `sys.exit(1)`)。これは**正しい挙動**(チェックアウトが自己完結する)だが、探した場所が分からないと無言の死に見えるので、エラー文に `ENV_PATH` を必ず含めること。CI は `.env` を使わず env 変数で API キーを渡しているので影響を受けない
- **テストを一周回すだけで課金や実副作用が起きてはいけない**。実 Discord へ投稿する / YouTube を叩く / 受信箱を Gemini で要約する テストは、前提の有無 (`shutil.which("gws")` 等) だけをゲートにすると **開発機では前提が揃っているので必ず実行される**。副作用と課金のあるものは `ARMS_LIVE_TESTS=1` のオプトインの後ろに置き、既定は SKIP + exit 0 (`tests/test_vault_semantic_smoke.py` の作法) にそろえる (2026-07-30)
- **「ゲートを付けた」を宣言で確かめない。カウンタと実ファイルの前後差分で確かめる**。上のゲーティングをやった後も漏れが2件残っていた: `test_grounding.py` は素で実 Gemini + Google Search を呼び、`test_create_note.py` は**本番 Vault** (`D:\マイドライブ\Obsidian`) に書き込みつつ `tool_create_note` 内部の MOC 分類で間接的に Gemini を呼んでいた。**明示的に API を呼ぶテストは目で見つかるが、ツールの内部で間接的に呼ぶものは見落とす**。`logs/gemini_daily_calls.json` のカウント差分で1本ずつ絞ると即座に特定できた。しかも `tool_create_note` は同名衝突時にタイムスタンプ接尾辞で退避する仕様なので、**上書きではなくスイープ1周ごとに1件増え**、実 Vault に19件溜まっていた (2026-07-30)
- **スタブは「呼ばれなくする」だけでなく「設定が読まれなくなる」**。`test_curate_note_save.py` が `dotenv` をスタブしたため `lib/cost_guard.py` の `load_dotenv` が無効化され、`.env` の `DAILY_GEMINI_CALL_LIMIT=0` (無効) が読まれず**既定値200に落ちた**。一方 `GEMINI_COUNTER` は**本番の** `logs/gemini_daily_calls.json` を見続けていたため、**本番カウントが200を超えた時刻以降だけこのテストが赤くなる** = 同じコードが朝は緑・夜は赤 (実測 count 212 で再現)。テストが本番と共有する可変資源 (カウンタ・PIDファイル・Vault) は**必ず一時パスへ隔離する**。`lib_shared` は `client.models.generate_content` を予算チェックで包む (`lib_shared.py:302-308`) ので、**`google.genai` をスタブしてもカウンタは進む**点に注意 (2026-07-30)

## Encoding Safety Rules (Windows Japanese Environment)
> Lesson learned: 2026-02-27 — AI agent caused system-wide contamination by setting
> PYTHONIOENCODING at the OS/session level instead of fixing encoding at the script level.

### Scope Hierarchy — Never exceed the minimum required scope
```
LEVEL 4: Registry (setx / reg add)          ← NEVER TOUCH
LEVEL 3: PowerShell profile / .bashrc       ← NEVER without explicit user instruction
LEVEL 2: os.environ["PYTHONIOENCODING"]     ← NEVER (pollutes all child processes)
LEVEL 1: subprocess(env={...}, encoding=)   ← OK: per-process only
LEVEL 0: sys.stdout.reconfigure() / open()  ← PREFERRED: per-script/handle only
```

### NEVER (絶対禁止)
- `setx PYTHONIOENCODING utf-8` (persistent registry write)
- `os.environ["PYTHONIOENCODING"] = "utf-8"` (propagates to all subprocesses)
- Adding encoding settings to PowerShell profile or `.bashrc` without explicit user request
- `chcp 65001` as a persistent system change

### ALLOWED (正しい対処)
```python
# Script-level stdout fix (entry point only)
sys.stdout.reconfigure(encoding='utf-8')

# All file I/O — always explicit
with open(path, encoding='utf-8') as f: ...

# Subprocess — per-call, with error resilience
subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace')

# Subprocess with env injection (child process only)
subprocess.Popen(cmd, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
```

### Decision Flow for encoding errors
1. Identify scope: which process/file is failing?
2. Fix at LEVEL 0 or LEVEL 1 — never higher
3. If higher scope seems needed → STOP and ask user first
4. After fix: verify `git log` and other tools still display Japanese correctly

## Workflow
1. Analyze the request
2. Check existing files (`agent_hello.py`, relevant `tool_*.py`)
3. Propose changes (Plan Mode for complex tasks)
4. Execute changes and run verification
5. If Claude makes a mistake -> fix it, then update this CLAUDE.md with the lesson

## AI Instruction Principles (Vibe Coding 2.0)
> From "18 Rules for Not Failing at Vibe Coding" — process rules applicable to this project.

- **1 session, 1 feature**: Never mix multiple fixes or features in the same conversation. Each new feature or bug fix gets a fresh chat to keep context clean.
- **50-line checkpoints**: For complex implementations, stop and wait for human review every ~50 lines. Do not generate large blocks of code in one shot.
- **Commit after every working state**: As soon as a feature works (or a meaningful change is confirmed), commit immediately — before moving to the next task.
- **Stop and reset when confused**: If debugging loops with no progress, stop editing code. Return to plain language, restate the goal from scratch, and re-enter Plan Mode.

## Plan Mode Protocol
- Complex tasks (3+ files, new features, architectural changes): always start in Plan Mode
- **Verification plan is MANDATORY**: Every plan must include HOW to prove it works
- If implementation goes wrong, **STOP immediately** and return to Plan Mode — never push through
- Never micromanage — give intent, let Claude execute

### Plan Template (Required for complex tasks)
```
## Goal
[What we're trying to achieve]

## Steps
1. [Step] → [Expected result]
2. ...

## Verification
- [ ] [How to prove each step worked]
- [ ] [Test command or manual check]

## Rollback
[How to undo if something goes wrong]
```

## Review Protocol
- After implementation, run `/grill` to trigger adversarial self-review
- Verify behavior diff against main branch when applicable
- Run relevant `test_*.py` files before committing

### CI (GitHub Actions)
`.github/workflows/tests.yml` が push/PR で自動実行される (`windows-latest`)。
- `guardian` ジョブ (ブロッキング): `verify_project.py` を実行
- `tests` ジョブ (現状 `continue-on-error: true`): `tests/test_*.py` を1本ずつ
  スクリプトとして実行し終了コードで判定する。**`pytest tests/` では判定しない** —
  多くのファイルの `check()` ヘルパーは失敗時も例外を投げないため、pytest 経由だと
  チェックが全部failしていても緑になる (このリポジトリで繰り返し出ている
  「表示と実態の食い違い」パターンそのもの)
- `tests` が bootstrap 扱いなのは、`tests/` に実ネットワーク/実APIキー/外部バイナリ
  (gws CLI等) が要る「手動デバッグスクリプト」が混在するため
  (Architecture 節の "tests/: Unit tests and debugging stubs" 参照)。
  一度実行結果を確認し、要否を仕分けてから `continue-on-error` を外すこと

### Backtest Gate (crypto logic)
- `lib/quant_core.py` / `tools/tool_crypto.py` の変更は **before/after バックテスト比較が必須**。
  詳細な規約と既知バグパターンは skill `crypto-backtest` を参照 (該当ファイル編集時に自動ロードされる)

### Grill Me Rules
1. **Before PR**: Claude must challenge every change — "Why is this necessary?"
2. **Edge cases**: Identify at least 3 edge cases per function change
3. **Evidence**: Show diff or execution results to prove correctness
4. **Bug fix re-implementation**: After fixing a bug, ask: "Now discard this fix and implement the most elegant solution from scratch"
5. **No mercy**: Do not approve changes with TODO/FIXME left behind
6. **Skill Recommendations**: Analyze the changes/task and strongly recommend relevant skills from `antigravity-awesome-skills` (e.g., `@architecture`, `@test-driven-development`) or `gws_cli` (e.g., `gws-*`, `recipe-*`) if applicable.

## Output Style: Explanatory + Learning
- When making changes, always explain **why** (not just what)
- For non-trivial logic, include a brief rationale in the response
- When introducing new patterns, explain trade-offs vs alternatives
- For bug fixes, explain the root cause before the fix
- **Learning mode**: When the user asks about unfamiliar concepts, provide:
  - A one-paragraph summary
  - A concrete code example
  - A "gotcha" or common mistake to avoid
- **Decision log**: For architectural decisions, document: Decision → Reason → Alternatives considered

## Custom Commands (Claude Code)
- `/sync` — Load 7-day context from Git, Obsidian, and learning_db
- `/techdebt` — Scan for duplicate code, dead code, TODOs, and consistency issues
- `/grill` — Adversarial code review of recent changes
- `/subagent-task` — Generate a delegation template for sub-agent tasks
- `/visualize` — Generate architecture diagrams and flow visualizations

## Visualization Protocol
- When explaining complex systems, generate **ASCII diagrams** or **Mermaid flowcharts**
- For data flows: use sequence diagrams
- For architecture: use component diagrams
- For state machines: use state diagrams
- Offer to generate **HTML presentations** for comprehensive explanations
- Always label nodes, edges, and include a legend if >5 elements

## Sub-agent Delegation Protocol

### Defined Subagents (`.claude/agents/`)
| Agent | 使いどころ |
|---|---|
| `verifier` | PR説明文/最終要約/ドキュメントの主張が証跡に基づいているか検査 (コミット/投稿前) |
| `test-writer` | 新規バグ修正・機能に対応するテストを既存作法 (LEVEL 0 encoding, ast静的検査) で追加 |
| `bug-pattern-archivist` | バグ修正後の教訓を CLAUDE.md 本体か該当 skill かを判断して追記 (肥大化防止) |

### When to Delegate
| Trigger | Example |
|---------|---------|
| 3+ files affected | Function extraction, API unification |
| Project-wide scan | `/techdebt`, grep across all files |
| Documentation generation | README, API docs, CHANGELOG |
| Test case creation | Full unit test suite for new features |
| Bulk data processing | Full log analysis, migration scripts |

### Rules
1. **Keyword trigger**: Append `use subagents` to prompt for heavy tasks
2. **Single responsibility**: Each sub-agent gets ONE focused task
3. **File-based output**: Sub-agents write results to files (never return to main context)
4. **Main = integrator**: Main agent only merges, reviews, and commits

### Anti-patterns (Do NOT)
- ❌ Let sub-agents modify `CLAUDE.md` or `.env`
- ❌ Run sub-agents for tasks under 50 lines of change
- ❌ Chain 3+ sub-agents sequentially (parallelize instead)
- ❌ Skip review of sub-agent output before committing
