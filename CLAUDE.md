# Antigravity AI Agent - Project Rules

## Architecture
- **Core Agent**: `agent_hello.py` — Plan-and-Execute + ReAct + Self-Improvement (Gemini gemini-2.5-flash)
- **Discord Bot**: `bot_discord.py` — Slash commands, PID guard, voice input (Whisper STT)
- **Universal Launcher**: `main.py` — AI router for all tools
- **Scheduler**: `agent_scheduler.py` — Cron-like (07:00 daily note, 08:00 news, 08:30 BTC, 09:00 scan, 23:00 dashboard)
- **Knowledge Base**: Obsidian Vault (`C:\obsidian\Obsidian`)
- **Output**: `30_Main/` in vault (source for n8n -> Google Docs -> NotebookLM pipeline)
- **Archive**: `50_Archive/` in vault
- **Automation**: n8n (self-hosted), backups in `C:\obsidian\Obsidian\Dev_Logs\n8n_backups\`

## Tools
| File | Purpose |
|------|---------|
| `tool_curator.py` | The Explorer v4.0 "Entropy Injector" (3-slot curation + Tavily trends) |
| `tool_calendar.py` | Google Calendar + Todoist integration (5 tools) |
| `tool_daily_note.py` | Daily note auto-generation from template |
| `tool_lora_factory.py` | LoRA/ComfyUI image generation |

## Coding Standards
- **Python**: PEP8. Always `# -*- coding: utf-8 -*-` and explicit `encoding='utf-8'` on file I/O.
- **Paths**: `os.path.join()` or raw strings with `\\`. Never forward slashes on Windows file ops.
- **Launcher**: Use `py` (not `python`) on this machine.
- **Safety**: Never delete user data in `30_Main` without confirmation.
- **Secrets**: `.env` for all API keys. Never commit `.env`, `token.json`, `credentials.json`.

## Commands
```bash
py agent_hello.py              # Run core agent
py bot_discord.py              # Start Discord bot
py agent_scheduler.py          # Start scheduler
py main.py                     # Universal launcher
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
- Bot duplicate processes -> PID guard (`bot_discord.pid`) prevents multiple instances
- Windows `os.kill(pid, SIGTERM)` throws `OSError [WinError 87]` for dead PIDs -> catch `OSError`

## Workflow
1. Analyze the request
2. Check existing files (`agent_hello.py`, relevant `tool_*.py`)
3. Propose changes (Plan Mode for complex tasks)
4. Execute changes and run verification
5. If Claude makes a mistake -> fix it, then update this CLAUDE.md with the lesson

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

### Grill Me Rules
1. **Before PR**: Claude must challenge every change — "Why is this necessary?"
2. **Edge cases**: Identify at least 3 edge cases per function change
3. **Evidence**: Show diff or execution results to prove correctness
4. **Bug fix re-implementation**: After fixing a bug, ask: "Now discard this fix and implement the most elegant solution from scratch"
5. **No mercy**: Do not approve changes with TODO/FIXME left behind

## Output Style: Explanatory
- When making changes, always explain **why** (not just what)
- For non-trivial logic, include a brief rationale in the response
- When introducing new patterns, explain trade-offs vs alternatives
- For bug fixes, explain the root cause before the fix

## Custom Commands (Claude Code)
- `/sync` — Load 7-day context from Git, Obsidian, and learning_db
- `/techdebt` — Scan for duplicate code, dead code, TODOs, and consistency issues
- `/grill` — Adversarial code review of recent changes
- `/subagent-task` — Generate a delegation template for sub-agent tasks

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
