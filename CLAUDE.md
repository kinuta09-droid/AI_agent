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

## Known Bug Patterns
- Gemini defaults to 2024 without explicit date -> always inject `today_str` in prompts
- Critic proposes non-existent tools -> pass `valid_tools` list in Critic prompt
- `tool_create_note` fails on duplicate filenames -> use timestamp suffix fallback
- Planner over-relies on `tool_vault_search` -> explicit tool selection guidelines needed
- Agent ignores negation constraints -> explicit constraint extraction in Planner/Critic
- `todoist-api-python` v3+: use `filter_tasks(query=)` not `get_tasks(filter=)`
- Bot/Scheduler duplicate processes -> **always acquire via `lib.pid_guard.acquire()`**. Never hand-roll a "check PID file then write it" guard: the two steps are not atomic, so processes starting within seconds of each other (heavy imports run *before* the guard) all decide "no live instance" and all write the lock. This let a 2nd Discord bot connect to the gateway, so one `/agent` ran twice in parallel and `logs/skill_observations.jsonl` got same-second duplicate entries (2026-07-17)
- Duplicate entries in `logs/skill_observations.jsonl` are **not** a nested-wrapper bug: `observe()` has exactly one call site (`engine_plan_execute.py`) and the registry stores raw callables. Same-second entries with *different* args = two concurrent processes, so count the processes first (`Get-CimInstance Win32_Process`, check who holds the Discord socket)
- PID は再起動をまたいで一意ではない。電源断で残った PID ファイルの PID が次のブートで再利用されていると `is_process_alive` が(安全側に倒れて)「生存」と答え、`acquire()` が永久に False -> Bot が無言で起動しない。`main.py` は即 exit を 5秒毎に再起動するだけで heartbeat 判定に到達しないため通知も出ない。**PID と一緒にブート ID を記録**して前ブートの残骸を無条件 stale にする (`lib/pid_guard.boot_id`)
- heartbeat が守るのは「上がったプロセスが止まったか」だけ。「そもそも上がってこない」は守備範囲外 -> `lib/watchdog.is_fast_exit` / `restart_delay` / `should_alert` で crash loop を検知・通知する。PC 起動時に立ち上がらないときの確認順は `scripts/AUTOSTART.md`
- 2026-07-25 朝、PC再起動後にBotが無言で起動しない事象が発生 -> 実機で `Get-ScheduledTask` を確認したところ `ARMS Launcher` タスク自体が未登録だった。`scripts/AUTOSTART.md` の `schtasks /create` は手動一回限りの手順で git 管理外のため、コード側をどれだけ直しても `main.py` が一度も走らなければ無意味。新しいマシン/ユーザーでセットアップする際は必ず `schtasks /query /tn "ARMS Launcher"` で登録有無を先に確認する
- **bot層で `except Exception as e:` して `Error: {e}` を Discord に返すだけにしない**。ログに一行も残らないため、壊れていても誰も気付けない (これが /curate の NameError を5ヶ月隠した真因。バグ本体より罪が重い)。しかも discord.py は未捕捉のコマンド/View 例外を自前ロガーで stderr に出すので、**この形の except は書かないほうがマシ**。必ず `lib.bot_errors.log_command_error(where, e, prefix)` を通す (2026-07-26 に bot 層28箇所を一括置換)。`verify_project.py` の `check_swallowed_exceptions()` が再混入を止める。`tools/` の `return f"Error: {e}"` は呼び出し元への戻り値契約なので対象外
- **bot層・スケジューラの見出しに `✅` をハードコードしない**。ツール層は例外を投げずに `return f"Error: {e}"` を返す規約なので、「例外が出なかった＝成功」と決めつけると失敗が ✅ で表示される。判定は `lib.bot_errors.is_failure_result()` / `FAILURE_PREFIXES` に一元化する (2026-07-26 に5箇所を一掃: `/agent`・`/chat agent`・深掘りボタン・定時ジョブ完了通知・`_summarize_results`)。**同じ結果を出す入口は必ず1つの関数を共用する** — `/agent` だけ直して `/chat agent` を取り残したのは、入口ごとに見出しを書いていたため。`tests/test_result_icons.py` の静的スキャン (近傍に ❌ 分岐が無い ✅ 見出しを違反とする) が再混入を止める
- **ツール層の Error 文字列を「例外の有無」だけで成否判定しない**。同じ穴が bot 層 (2026-07-26) の次に **エンジン中核** で見つかった: `engine_plan_execute.execute_plan()` が `try: observation = observe(...); success = True` と書いていたため、ツールが規約どおり `return f"Error: {e}"` を返しても `success=True` になり、(a) Critic に成功として渡り、(b) `log_execution` が `outcome="success"` で learning_db に記録し、(c) `✅ Mission Complete` が出ていた。`Agent_Performance.md` の成功率はこの分だけ水増しされていた。判定は必ず `lib.bot_errors.is_failure_result()` を通す (2026-07-27)。**表示層を直したら、その表示に値を渡している層も同じ基準で洗う** — 見出しだけ直しても、学習DBに残る数字は嘘のままになる
- **「例外が出なかった＝成功」を潰したら、その上の "戻り値を組み立てる層" も見る**。`execute_plan` の成否判定を直しただけでは `/agent` の表示は ✅ のままだった: `run_plan_and_execute` が「修正回数を使い切った」「Critic が ok と言った」経路で無条件に `_generate_final_summary()` (Gemini の要約) を返しており、要約は失敗を説明する文章であって失敗接頭辞を持たないため。結果、learning_db は `failed`・Discord は `✅ Mission Complete` という食い違いが残る。`_final_report()` で「成功ステップが1件も無い実行は `タスク失敗:` を返す」を一箇所に集約した (`execute_plan` は最初の失敗で break するので、成功0件＝どの修正計画も1歩目から進めなかった、と判断してよい)。**自己申告の要約を最終判定に使わない**
- **「成功」「失敗」の2状態しか無い見出しは、部分失敗のときに必ず嘘をつく**。全滅ケース (`タスク失敗:` を返す) だけを直した時点では、3ステップ中2つ成功・保存だけ失敗のような実行が `log_execution` では `outcome="partial"` なのに Discord には `✅ Mission Complete` で出ていた。`_final_report` が「成功が1件でもあれば」無条件に Gemini 要約へ落ちるため。**部分失敗に失敗接頭辞は付けられない** (成功が混ざっているので `タスク失敗:` は嘘になる) ので、第3の印 `lib.bot_errors.PARTIAL_PREFIX` + `is_partial_result()` を足し、`mission_header` を `✅ / ⚠️ / ❌` の3状態にした (2026-07-27)。**不変条件は「outcome != success ⇒ 見出しが ✅ でない」**と書く (「⇒ ❌」と書くと partial で過剰判定になる)。Gemini の要約は失敗に触れるとは限らないので、失敗したステップ番号とツール名は要約とは別に本文へ明示する。回帰は `tests/test_result_icons.py` の [12]
- **失敗接頭辞は tuple では表しきれない**。実際に `tools/` を洗うと `Error creating note:` / `Error searching web:` (動詞が挟まる)、`[30_Main] Error merging reports:` (先頭に [タグ])、`[ERROR] ...` (gmail/vault_index/future_search)、`[xtrend error] ...` (`lib/tool_registry` の lazy wrapper 4種) と揺れており、`FAILURE_PREFIXES = ("Error:", "Error (")` では大半を取りこぼす。`lib.bot_errors._TOOL_FAILURE_RE` に集約し、新しい形はそこにだけ足す。ただし **`\b` は使わない**: Unicode では日本語が単語文字なので `Error\b` が「Errorハンドリングの調査結果」に一致してしまう -> `Error(?![^\W\d_])` (直後が文字でない) を使う。回帰は `tests/test_result_icons.py` の [1] (実在の失敗形13種 + 誤検知させてはいけない正常系5種) と [9][10] (execute_plan / log_execution)
- **合否を判定する関数では、説明文を合否と無関係に組み立てない**。「表示が実態と食い違う」類型は、表示層 (bot 見出し)・戻り値層 (`_final_report`)・記録層 (`log_execution`) の次に **判定器そのもの** で見つかった: `scripts/eval_agent._check(checks, name, ok, detail)` が detail を合否に関わらず格納していたため、`ok=true` なのに detail が「回答に一致なし: ...」「順序が逆」「失敗が成功として表示される」となり、`logs/eval/*.json` を読む人が合格を失敗と読み違える (2026-07-28)。ただし **全 detail を「失敗時のみ」にするのは誤り** — `max_steps` の「実際は 2 ステップ (['tool_update_note'])」のように合格時にも読む価値がある事実が消える。引数を **`detail` (合否に関わらず残す事実) と `fail_detail` (合格時は捨てる)** に分け、呼び出し側に「これは失敗時にしか意味を成さない文か」を宣言させる。回帰は `tests/test_eval_agent.py` の [15] (合格時 detail が空 + 逆方向: 実測値は残る) と [16] (**ast で `_check` の呼び出しを走査**し、detail 引数に失敗語彙があれば落とす)。動的テストだけだと「私が書いたシナリオを実行する呼び出し側」しか守れず、新しい呼び出し側の再混入に気付けない
- **`discord.ui.View` の `timeout=None` は「Bot 再起動をまたいで生きる」宣言であって、単なる無期限ではない**。`bot.add_view()` での再登録とセットでなければ、再起動後はボタンが押しても無反応 (Discord 側に「インタラクションに失敗しました」が出るだけで、ログには一行も残らない = 所見2と同じ無言の死)。おまけに discord.py の View ストアから永久に消えずメモリを食う。永続化の条件は **固定 custom_id + コールバックがステートレス** (毎回ファイルを読み直す) の2つで、instance 変数に作業状態を持つ View は原理的に永続化できない -> その場合は**有限 timeout + `on_timeout` でボタンを無効化**する (2026-07-26 に `ClientReviewLoopView` / `OpenInObsidianView` を是正)。`tests/test_button_views.py` の静的スキャンが「timeout=None なのに add_view されていない View」を落とす
- `on_timeout` から `interaction.followup.send()` の戻り (WebhookMessage) を編集して期限切れを通知する場合、**View の timeout は interaction token の寿命 (発行から15分) より短くする**。15分ちょうどにすると通知の edit がトークン失効と同時になり、通知だけが無言で失敗する。なお `discord.ui.View` に `message` 属性は自動では入らない (自分で代入する)。`view.timeout = 秒数` は実行中のタイマーの期限も更新できるので、長時間処理の前後で延長/復帰させる
- 例外メッセージを整形し直すときは **`startswith("Error:")` で分岐している箇所を先に洗う**。`commands/bot_commands_agent._FAILURE_PREFIXES` はコロン固定だったため、`Error (RuntimeError): ...` 形式にすると失敗結果を成功扱いして次回 `/agent` のセッション文脈に混ぜるところだった
- **キーワード判定を部分文字列一致で書かない**。`any(w in content for w in ["sec","ban","war",...])` は "sec" が seconds/security/sector/securities に、"war" が warning/toward に、"ban" が banks/abandon に当たる。`tool_crypto` のニュースゲートがこれで、SYSTEMIC_RISK は `_decide_signal` の最優先分岐でテクニカルを無効化する (STAY AWAY / confidence=0) 上に、検索が走るのは `|24h変動| >= 3%` のとき **だけ** = BUY が出る局面に限って起動するという最悪の組み合わせだった。実測でニュース検索が走った9件中7件(78%)が誤爆し、`trade_log.json` が5ヶ月ゼロ件になった (2026-07-27)。必ず単語境界の正規表現にし、**判定根拠をレポート/ログに併記**する (旧実装は根拠を一切残さず、だから5ヶ月気付けなかった)。おまけ: 同じ罠を「関連性ゲート」側で作り直しかけた ("eth" が whether/together/method に一致)。**フィルタを直すときは、そのフィルタを守るために足したゲートも同じ基準で書く**
- **naive datetime を跨いで比較しない**。`trade_log` の timestamp は `datetime.now().isoformat()` = ローカル(JST) naive、ccxt の足は epoch ms → `pd.to_datetime(unit='ms')` = **UTC naive**。見た目はどちらも tz なしなので比較が通ってしまい、`_resolve_trade` がエントリー直後9時間の足を「entry より前」として捨てていた (その間に SL/TP へ到達したトレードを取りこぼす)。旧監査は窓全体の `min/max` で時刻を見ていなかったため、判定を時系列走査に統一した瞬間に顕在化した。`lib/quant_core` 側は全て UTC naive で閉じているので **ライブを UTC に寄せる** (`tool_crypto._to_utc_naive`)。なお `.timestamp()` はローカル naive を正しくエポックに直すので、**fetch に渡す時刻はローカルのまま**にすること (両方 UTC にすると取得開始が9時間ずれる)
- **「嘘を潰す修正」は、符号を逆にした同じ嘘を作りやすい**。`_generate_final_summary` は自分が何を持っているか知らないまま報告を書いていたため、Web検索でヒットした他社製品 (Zendesk の音声AI) を「当エージェントの機能」として断定し、対応プランまで列挙していた (eval T12)。`lib/tool_registry` から機能一覧を注入して直したが、**初版は「下記が全てであり、これ以外の機能は存在しない」と断定させており、今度は実在する機能を「無い」と答えた** — capability block はツール29件だけで、対応するツールを持たないスラッシュコマンドが12件ある (`/memory` `/watchlist` `/persona-add` `/persona-list` `/client-review` `/agent-stats` `/article` 等)。実測で「ペルソナ機能は存在しません」と回答 (`/persona-add` は実在)。**根拠リストを注入するときは、そのリストの網羅範囲を必ず言葉で限定する**。「これで全て」と書けるのは、リストが問いの全域を覆っているときだけ (2026-07-28・PR #19)
- **どのチェックが落ちたかを見ずに原因のレイヤーを決めつけない**。T12 は plan 側のアサーションが全て pass で `exec_assertions` (回答テキスト) だけが落ちていたのに、「Planner のプロンプト問題」と要約されていた。実際の修正箇所は最終要約層で、Planner を触っても一生直らなかった。`logs/eval/*.json` の `checks` / `exec_checks` を必ず開く
- **アサーションを緩めて自分の変更を通していないか、証拠で示す**。同 PR で T12 が一度 fail したが、回答は「ツール一覧に含まれていません」と正しく、`answer_must_match` が否定の言い回しを4種しか列挙していなかっただけ。正規表現を広げる前に**旧バグ回答が広げた後のパターンにも一致しないこと**を確認し、それを固定するテストを置く (`tests/test_answer_grounding.py` の [6] がベースライン JSON を読んで検証する)
- **ツールの `description` は Planner の唯一の選択根拠**。`tool_update_note` が `"追記"` の2文字だった頃、「既存のデイリーノートに追記して」は説明文に「デイリーノート」を含む `tool_sync_to_daily_note` (予定/タスク同期) に語彙一致で奪われていた (eval T09)。守備範囲だけでなく**非対象**も書く。ただし一方に語を足したら他方の縄張りも明示すること — 逆流して今度は同期要求を奪う
- Windows `os.kill(pid, SIGTERM)` throws `OSError [WinError 87]` for dead PIDs -> catch `OSError`
- `os.kill` は CPython 内部で `OpenProcess(PROCESS_ALL_ACCESS)` を要求する。タスクスケジューラが `/rl highest` で起動した高整合性プロセスを非管理者プロセス (`scripts/arms_ctl.py` 等) から終了させようとすると `PermissionError [WinError 5]` になる。**「殺せない」のではなく要求権限が過剰なだけ**で、終了に必要なのは `PROCESS_TERMINATE` のみ (同一ユーザーなら整合性レベルをまたいでも DACL で許可される) -> `lib/watchdog._terminate_via_handle` にフォールバックする
- 子プロセスの stdout をファイルへリダイレクトすると Python は既定でブロックバッファリングし、`print()` が 8KB 貯まるまでログに現れない。`logs/launcher.log` は起動失敗時に真っ先に読むログなので、**ランチャー (`scripts/start_arms.bat` の `py -u main.py`) と子 (`main.py` の `Popen([python, "-u", script])`) の両方**に `-u` が要る (片方だけだと `[Monitor]` 行か `[Discord]` 行のどちらかが消える)
- Windows PowerShell 5.1 は `.ps1` を **BOM が無いと ANSI (cp932)** として読む -> 日本語コメント入りの `.ps1` を BOM なし UTF-8 で保存すると全体が構文エラーになる。`open(p, 'w', encoding='utf-8-sig')` で保存すること。文字列内の環境変数も `"$env:X\..."` ではなく `"$($env:X)\..."` と部分式で囲む
- **タスクスケジューラから .bat を直接実行すると可視のコンソールウィンドウが残る**。その × を押すと `CTRL_CLOSE_EVENT` が飛んで `main.py`・Bot・Scheduler が一斉に死ぬ (タスクの Last Result が `-1073741510` = `0xC000013A` = STATUS_CONTROL_C_EXIT なら必ずこれ)。デスクトップに「スタック全滅ボタン」を置くのと同じ -> `scripts/start_arms.bat` は `wscript //B scripts/arms_hidden.vbs` 経由で非表示起動し、実処理は `scripts/arms_core.bat` に置く (2026-07-26 実障害)
- **`.bat` は ASCII のみで書く**。cmd.exe は `.bat` を UTF-8 ではなく OEM コードページ (cp932) で読むため、UTF-8 の日本語コメントが化ける。特に `。` (E3 80 82) は末尾バイト 0x82 が cp932 の先行バイトになり **行末の CR を食べる** -> 直後の実行行がコメント行と連結してコマンドとして実行される (`'<化けた文字>py' は、内部コマンドまたは...` + 終了コード 9009)。日本語の説明は `.md` 側に書く。`.vbs` も同様 (WSH は ANSI 読み) に ASCII のみ
- **実行中の .bat を編集しない**。cmd.exe はバッチをバイトオフセットで逐次読み直すため、実行中に行を挿入すると次のコマンド読み込み位置がずれ、同じ行を再実行したりする (2026-07-26: `start_arms.bat` 編集中に `py main.py` 行が二度走り、停止したはずのスタックが独りでに復活した)
- `OpenProcess` failing does **not** mean the process is dead: `ERROR_ACCESS_DENIED(5)` = alive but not queryable. Treating it as dead re-opens the duplicate-startup hole -> fall back to "alive" when in doubt (see `lib/pid_guard.is_process_alive`)
- WMI `CommandLine LIKE '%foo%'` matches **your own query process** -> filter on `Name = 'python.exe'` before believing a stray process exists

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

### Backtest Gate (crypto logic)
- `lib/quant_core.py` / `tools/tool_crypto.py` のシグナル・水準・ゲートロジック変更は
  **before/after バックテスト比較が必須**:
  `py scripts/backtest_crypto.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --days 730 --compare ...`
- 比較表（signal_type別 勝率・期待値R・トレード数）をコミットメッセージに貼る。比較なしはマージ禁止
- バックテストとライブは `lib/quant_core.py` の純関数を共有する。**ロジックのフォーク禁止**
  （ライブ側だけ/バックテスト側だけの判定分岐を作らない）
- 既知の制約: news_risk はバックテストで再現不能（NEUTRAL固定）、評価密度はライブと異なる

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
