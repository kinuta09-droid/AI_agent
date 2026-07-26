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
- Windows `os.kill(pid, SIGTERM)` throws `OSError [WinError 87]` for dead PIDs -> catch `OSError`
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
