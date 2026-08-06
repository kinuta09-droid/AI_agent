# Antigravity AI Agent - Project Rules

## Architecture (Modular)
- **Root**: `main.py` (entry), `bot_discord.py` (Discord host), `agent_hello.py` (compatibility bridge)
- **`commands/`** Bot logic by feature / **`lib/`** core utilities & obsidian helpers / **`tools/`** external API integrations / **`tests/`** Unit tests and debugging stubs / **`scripts/`** one-off scripts
- **`antigravity_env/`**: 開発・手動検証用の venv。**本番プロセス (`main.py`・Bot・Scheduler) はこれを activate せず常にシステムPython (`py`) で動く** — 任意依存はどちらに入れたか区別すること (→ Known Bug Patterns B)

## Tools
| File | Purpose |
|------|---------|
| `tool_curator.py` | The Explorer v4.0 "Entropy Injector" (3-slot curation + Tavily trends) |
| `tool_calendar.py` | Google Calendar + Todoist integration (5 tools) |
| `tool_daily_note.py` | Daily note auto-generation from template |
| `tool_lora_factory.py` | LoRA/ComfyUI image generation |
| `tool_paper_radar.py` | 注目論文の自動判別 (arXiv × HF upvote × 被引用速度 × 新しさ)。Gemini不使用=課金ゼロ |
| `tool_paper_digest.py` | 論文本文を docling でキャプチャ→要約→X スレッド/note 記事の下書き生成 |

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
py scripts\run_test_sweep.py   # tests/test_*.py を8並列でスイープ (CI と同じ実装)
scripts\arms.bat               # ワンクリック制御 (引数なし=メニュー / start|stop|restart|status|logs)
py main.py                     # Universal launcher
py tools/tool_paper_radar.py --field ai_agent --top 5   # 注目論文レーダー (課金ゼロ)
py tools/tool_paper_digest.py --index 1 --mode both     # 本文キャプチャ→要約→SNS/note下書き
py -m pip install -r requirements-docling.txt           # docling (任意・未導入ならHTML抽出に劣化)
py bot_discord.py              # Start Discord bot
py agent_scheduler.py          # Start scheduler
py n8n_patrol.py               # Patrol n8n workflows
py agent_trigger.py            # Trigger webhook
```

## MCP Servers
`.mcp.json` (プロジェクトスコープ・コミット済み) に3つ登録済み。初回は承認プロンプトが出る:
- `context7`: ライブラリの最新ドキュメント参照 (`todoist-api-python` v3+ 等のバージョン差異バグの予防)
- `chrome-devtools`: `tool_browser_use.py` / `tool_xtrend.py` のセレクタ崩れを実ブラウザで確認 (要 Node.js 22+)
- `code-review-graph`: Tree-sitterでコード構造をグラフ化し、変更影響範囲・テストギャップをトークン効率よく返す (`pipx install code-review-graph`)。`/review-changes` `/explore-codebase` `/debug-issue` `/refactor-safely` の4スキル付き。**既知の罠**: (a) `.mcp.json`の`command`は`code-review-graph`本体(`~/.local/bin`)の絶対パス直呼びにすること — `install`が既定で書く`uvx code-review-graph serve`は`pipx`とは別環境で、`pipx inject code-review-graph sentence-transformers`(意味検索用embedding、¥0)が効かない。(b) `detect_changes_tool`等の既定`base`は`HEAD~1`。未コミット分だけ見たいときは`base="HEAD"`を明示する。(c) スキル内のツール名(`list_graph_stats`等)は実際のMCP登録名(`..._tool`サフィックス付き)と食い違うので、ToolSearchで正式名を引く。(d) PostToolUseフックが編集のたびにグラフを自動更新する(実測0.74秒/回のブロッキング遅延)

GitHub MCP は個人の PAT が要るため `.mcp.json` に含めていない。使う場合は各自:
`claude mcp add --transport http github https://api.githubcopilot.com/mcp/ --header "Authorization: Bearer YOUR_PAT"`

## Skills (領域別の詳細な既知バグパターン)
`.claude/rules/known-bug-patterns.md` の Known Bug Patterns は**プロジェクト全域に効く教訓のみ**。特定領域の詳細は `.claude/skills/` に分割してあり、該当ファイルを触るときに自動ロードされる。新しい教訓は **どの skill に属するか判断してからそこに書く** (本体に足して肥大化させない)。

| Skill | 対象領域 |
|---|---|
| `windows-encoding` | .bat/.ps1/.vbs、cp932/BOM、ランチャー起動、subprocess/stdout エンコーディング |
| `process-lifecycle` | pid_guard、boot_id、watchdog、タスクスケジューラ、Windows プロセス終了 |
| `retrieval-tuning` | Vault ハイブリッド検索、リランク (Gemini/Ollama)、ツール説明文、eval_retrieval/eval_agent |
| `crypto-backtest` | lib/quant_core.py、tools/tool_crypto.py のシグナル/ゲートロジック |
| `llm-output-safety` | Verifier、プロンプトインジェクション対策、判定器設計、LLM出力のJSONパース |
| `discord-bot-patterns` | discord.ui.View の永続化、interaction token、失敗プレフィックス判定 |
| `parallel-execution-safety` | engine_plan_execute.py の fan-out、parallel_safe 宣言、副作用の順序依存 |

@.claude/rules/known-bug-patterns.md

@.claude/rules/encoding-safety.md

@.claude/rules/workflow.md

@.claude/rules/review-protocol.md
