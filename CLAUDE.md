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
`## Known Bug Patterns` は**プロジェクト全域に効く教訓のみ**。特定領域の詳細は `.claude/skills/` に分割してあり、該当ファイルを触るときに自動ロードされる。新しい教訓は **どの skill に属するか判断してからそこに書く** (本体に足して肥大化させない)。

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

> 新しい教訓は該当するサブ見出し (A〜D) の末尾へ。どれにも該当しなければ E。
> 書式は **太字の結論 → 症状 → 真因 → 対処 → 回帰テスト**。

### A. 表示と実態の食い違い（層をまたいで8回再発）

同じ欠陥が bot層 → engine → 最終要約 → 採点器 → テスト層 と、層を変えて繰り返し出ている。
共通の型は「**成否を、実態ではなく副次的な兆候 (例外が出たか・文字列の前方一致) で判定している**」。
そして誤判定は例外を出さないので**ログに一行も残らない = 無言の死**になる。

- **bot層で `except Exception as e:` して `Error: {e}` を Discord に返すだけにしない**。ログに一行も残らず、壊れていても誰も気付けない (/curate の NameError を5ヶ月隠した真因)。discord.py は未捕捉例外を自前ロガーで stderr に出すので、**この except は書かないほうがマシ**。必ず `lib.bot_errors.log_command_error(where, e, prefix)` を通す。回帰は `verify_project.py` の `check_swallowed_exceptions()`。`tools/` の `return f"Error: {e}"` は戻り値契約なので対象外
- **bot層・スケジューラの見出しに `✅` をハードコードしない**。ツール層は例外を投げず `return f"Error: {e}"` を返す規約なので、「例外が出なかった＝成功」と決めつけると失敗が ✅ で表示される。判定は `lib.bot_errors.is_failure_result()` / `FAILURE_PREFIXES` に一元化。**同じ結果を出す入口は必ず1つの関数を共用する** — `/agent` だけ直して `/chat agent` を取り残したのは入口ごとに見出しを書いていたため。回帰は `tests/test_result_icons.py` の静的スキャン (近傍に ❌ 分岐が無い ✅ 見出しを違反とする)
- **ツール層の Error 文字列を「例外の有無」だけで成否判定しない**。`engine_plan_execute.execute_plan()` が `try: observation = observe(...); success = True` と書いていたため、規約どおりの `return f"Error: {e}"` でも `success=True` になり、Critic に成功として渡り・`log_execution` が `outcome="success"` で learning_db に記録し・`✅ Mission Complete` が出ていた (`Agent_Performance.md` の成功率はこの分だけ水増し)。判定は `lib.bot_errors.is_failure_result()` を通す。**表示層を直したら、その表示に値を渡している層も同じ基準で洗う** — 見出しだけ直しても学習DBの数字は嘘のまま
- **「例外が出なかった＝成功」を潰したら、その上の "戻り値を組み立てる層" も見る**。`execute_plan` を直しても `/agent` は ✅ のままだった: `run_plan_and_execute` が「修正回数を使い切った」「Critic が ok」経路で無条件に `_generate_final_summary()` を返し、Gemini の要約は失敗接頭辞を持たないため。learning_db は `failed`・Discord は `✅ Mission Complete` の食い違いが残る。`_final_report()` に「成功ステップが1件も無い実行は `タスク失敗:` を返す」を集約 (`execute_plan` は最初の失敗で break するので成功0件＝1歩目から進めなかった)。**自己申告の要約を最終判定に使わない**
- **「成功」「失敗」の2状態しか無い見出しは、部分失敗のときに必ず嘘をつく**。3ステップ中2成功・保存だけ失敗の実行が `log_execution` では `outcome="partial"` なのに Discord は `✅ Mission Complete` だった (`_final_report` が「成功が1件でもあれば」Gemini 要約へ落ちるため)。**部分失敗に失敗接頭辞は付けられない**ので第3の印 `lib.bot_errors.PARTIAL_PREFIX` + `is_partial_result()` を足し、`mission_header` を `✅ / ⚠️ / ❌` の3状態にした。**不変条件は「outcome != success ⇒ 見出しが ✅ でない」**と書く (「⇒ ❌」だと partial で過剰判定)。要約は失敗に触れるとは限らないので、失敗したステップ番号とツール名は本文へ別途明示する。回帰は `tests/test_result_icons.py` の [12]
- **失敗接頭辞は tuple では表しきれない**。実際の `tools/` は `Error creating note:` (動詞が挟まる)、`[30_Main] Error merging reports:` (先頭に [タグ])、`[ERROR] ...`、`[xtrend error] ...` (`lib/tool_registry` の lazy wrapper 4種) と揺れ、`FAILURE_PREFIXES = ("Error:", "Error (")` では大半を取りこぼす。`lib.bot_errors._TOOL_FAILURE_RE` に集約し、新しい形はそこにだけ足す。ただし **`\b` は使わない** — Unicode では日本語が単語文字なので `Error\b` が「Errorハンドリングの調査結果」に一致する → `Error(?![^\W\d_])` を使う。回帰は `tests/test_result_icons.py` の [1] (実在の失敗形13種 + 誤検知させてはいけない正常系5種) と [9][10]
- **合否を判定する関数では、説明文を合否と無関係に組み立てない**。`scripts/eval_agent._check(checks, name, ok, detail)` が detail を合否に関わらず格納したため、`ok=true` なのに detail が「回答に一致なし: ...」となり、`logs/eval/*.json` の読み手が合格を失敗と読み違えた。ただし**全 detail を「失敗時のみ」にするのは誤り** — 「実際は 2 ステップ (['tool_update_note'])」のように合格時にも読む価値がある事実が消える。**`detail` (合否に関わらず残す事実) と `fail_detail` (合格時は捨てる)** に分け、呼び出し側に宣言させる。回帰は `tests/test_eval_agent.py` の [15][16]。[16] は **ast で `_check` の呼び出しを走査**する — 動的テストだけでは新しい呼び出し側の再混入に気付けない
- **同じ食い違いはテスト層とCI設定でも起きる**。(a) `tests/test_moc_auto_link.py` 等が判定結果を print するだけで**終了コードに反映しておらず**、全ケース FAIL でも exit 0 = CI からは永久に緑。(b) `tests.yml` の `PYTHONPATH: ${{ github.workspace }}` が `sys.path` bootstrap の欠陥 (`dirname` が1段足りず `tests/` 自身を追加している3ファイル + bootstrap 無し1ファイル) を隠し、**実機で ImportError になるテストが CI では通っていた**。→ 各テストは自前で `sys.path.insert(0, dirname(dirname(abspath(__file__))))` し、CI 側に下駄を履かせない。**判定は必ず `sys.exit(1 if _FAIL else 0)` まで通す**

### B. 環境の固定・絶対パス（「下駄」の類型）

ローカルの緑は「このマシンでは動く」以上を意味しない。**マシン固有の前提はコードの外にも中にも移動する**。

- **「CI と実機の失敗一覧が一致しない」とき、疑うのは CI ではなく実機側の下駄**。CI の `PYTHONPATH` を外した直後の初起動が、実機 69/0 に対し **58/11** だった。11件中6件は `sys.path.insert(0, r'C:\AI_agent')` の**ルート直書き**で、開発機ではリポジトリが実際にそこにあるので永久に緑 = **同じ下駄がテストファイルの中に移っていただけ**。`verify_project.py` の `check_machine_locked_syspath()` が止める (純 ast なので `--fast` に乗り commit hook で捕まる)。**検査は文字列走査ではなく ast で書く** — この欠陥を説明する docstring 自体が `sys.path.insert(0, r'C:\AI_agent')` を含むので、grep 的な検査は「教訓を書いた行」を違反として報告する。回帰は `tests/test_verify_fast_mode.py` の [3] (違反を置くと赤 + 教訓コメントは緑の両方向)
- **「下駄を外した」と言えるのは sys.path だけ見たときではない**。同じルート直書きが「実行時に読み書きするパス」に**21箇所**残っていた — `learning_db.json` / コストガードの日次カウンタ4種 / OHLCVキャッシュ / `token.json`・`credentials.json` / `load_dotenv()` 12箇所。**症状の重さは逆**: sys.path の直書きは ImportError で派手に落ちるが、データパスの直書きは**落ちずに間違ったファイルを読み書きする** (別チェックアウトが本番の learning_db やコストガードのカウンタを更新すれば、統計もコスト上限も静かに嘘になる = 無言の死と同型)。→ `lib/paths.py` に `PROJECT_ROOT`/`ENV_PATH`/`LOGS_DIR` を集約し、**絶対パスのリテラルを書いてよいのはこのファイルだけ**にした。`verify_project.check_hardcoded_abs_paths()` が止める。誤検知を避けるため **「パスを組み立てている文脈」(os.path.join/load_dotenv/open の引数、語尾が `_PATH`/`_DIR`/`_ROOT`/`_FILE` の代入) に限定する** — 全リテラルを赤にすると、教訓の docstring・`scripts/search_dev_logs.py` の検索語リスト・唯一の宣言場所である `lib/paths.py` 自身が巻き添えになり、検査ごと外される方向に力が働く。回帰は `tests/test_verify_fast_mode.py` の [5][6][7]
- **リポジトリ外の絶対パス (Vault / BitNet) は `.env` 上書き + 既定値=現行値 の形で `lib/paths.py` に置く**。`D:\マイドライブ\Obsidian` と `C:\BitNet` は __file__ 相対では表せず全廃できない。各ファイルに散らすと、Vault を移した瞬間に**このファイルだけ旧パスを見る**分裂が起きる (実際 `tool_daily_note.py`・`tool_calendar.py`・`trigger_daily_news.py`・`agent_trigger.py` の4箇所に複製されていた)。`OBSIDIAN_VAULT_ROOT` / `BITNET_DIR` で上書き可・未設定なら従来値、とすれば **既存環境は .env を1行も足さずに挙動不変**。**リテラルを「消す」のではなく「1箇所に集めて宣言にする」** のが外部パスの正解。なお `tools/tool_lora_factory.py` は旧Vault を指したままで書き込み先が変わるため据え置き — `verify_project._ABS_PATH_EXEMPT` に理由付きで登録 (**新規追加禁止**)
- **`__file__` 相対にすると「チェックアウト毎に .env が要る」ようになる**。ルート直書きの時代は worktree や別クローンからでも `C:\AI_agent\.env` を読めていたので、新しい worktree がいきなり `GOOGLE_API_KEY not found` で死ぬ (`lib_shared` は import 時に `sys.exit(1)`)。これは**正しい挙動** (チェックアウトが自己完結する) だが、探した場所が分からないと無言の死に見えるので、エラー文に `ENV_PATH` を必ず含めること。CI は `.env` を使わず env 変数で渡すので影響を受けない
- **「劣化フォールバックが正しく動く」こと自体が、任意依存の導入環境の食い違いを無症状化させる** (2026-08-01・docling統合)。原因の種類が初めて「例外処理の書き方」ではなく「**同じマシンに2つある Python 環境のどちらに依存を入れたか**」だった。本番の起動チェーン (`scripts\arms_core.bat` → `py -u main.py`) は `antigravity_env` を activate せず、`main.py` は子プロセスを `sys.executable` で spawn するため **Bot もスケジューラも常にシステムPython で動く**。一方 `plans/docling-integration.md` の手順は `antigravity_env\Scripts\activate` の直後に `py -m pip install` を実行しており、このマシンの `py` ランチャーは activate 後の venv を解決したため docling は **antigravity_env にだけ**入った。`lib/docling_client.py` の `is_available()` による「import失敗→HTML抽出へ劣化フォールバック」の設計自体は正しいのに、本番では毎回これが発火し続け、エラーは一切出ない (`✅` のまま)。Discord `/doc` も夜間ジョブも「docling 未導入のためスキップ (0件)」を成功扱いで返し続けていた。**対処**: (a) 「入れた」と判断する前に、実際に**本番が使う python** で確認する (`py -m pip show <pkg>`。`antigravity_env\Scripts\pip3.exe show` や activate 後の `pip show` は別環境を見ている)、(b) 任意依存の手順には「システムPython (`py`、本番用) と `antigravity_env` (開発検証用) のどちらに入れたか」を明記し、本番機能で使うなら両方に入れる、(c) activate した状態で `py -m pip install` と書かない — `py` が activate を尊重するかはマシン依存。venv へ確実に入れるなら `python -m pip install` (activate で PATH 先頭に来た venv の python.exe を使う)。恒久チェックは未実装

### C. テストの隔離と課金

- **テストを一周回すだけで課金や実副作用が起きてはいけない**。実 Discord へ投稿 / YouTube を叩く / 受信箱を Gemini で要約する テストを、前提の有無 (`shutil.which("gws")` 等) だけでゲートすると **開発機では前提が揃っているので必ず実行される**。副作用と課金のあるものは `ARMS_LIVE_TESTS=1` のオプトインの後ろに置き、既定は SKIP + exit 0 (`tests/test_vault_semantic_smoke.py` の作法)
- **「ゲートを付けた」を宣言で確かめない。カウンタと実ファイルの前後差分で確かめる**。ゲーティング後も漏れが2件残っていた: `test_grounding.py` は素で実 Gemini + Google Search を呼び、`test_create_note.py` は**本番 Vault** に書き込みつつ `tool_create_note` 内部の MOC 分類で間接的に Gemini を呼んでいた。**明示的に API を呼ぶテストは目で見つかるが、ツール内部で間接的に呼ぶものは見落とす**。`logs/gemini_daily_calls.json` のカウント差分で1本ずつ絞ると即特定できた。しかも `tool_create_note` は同名衝突時にタイムスタンプ接尾辞で退避するので、**上書きではなくスイープ1周ごとに1件増え**、実 Vault に19件溜まっていた
- **スタブは「呼ばれなくする」だけでなく「設定が読まれなくなる」**。`test_curate_note_save.py` が `dotenv` をスタブしたため `lib/cost_guard.py` の `load_dotenv` が無効化され、`.env` の `DAILY_GEMINI_CALL_LIMIT=0` (無効) が読まれず**既定値200に落ちた**。一方 `GEMINI_COUNTER` は**本番の** `logs/gemini_daily_calls.json` を見続けたため、**本番カウントが200を超えた時刻以降だけ赤くなる** = 同じコードが朝は緑・夜は赤 (実測 count 212 で再現)。テストが本番と共有する可変資源 (カウンタ・PIDファイル・Vault) は**必ず一時パスへ隔離する**。`lib_shared` は `client.models.generate_content` を予算チェックで包む (`lib_shared.py:302-308`) ので、**`google.genai` をスタブしてもカウンタは進む**

### D. CI とビルド

- **CI を「速くする」変更は、速さの根拠と安全の根拠を別々に出す**。3件やって3件とも別の形で足をすくわれかけた。**(a) path filter は前提の上に乗る**: `paths-ignore: ['**.md']` が安全なのは「追跡下の `.md` を読む Python が1本も無い」からで、導入時は実際に0件だった。だが**前提が崩れても skip は黙って続く** — 将来「テストが読む `.md`」が1本入れば、それを編集した PR はそのテストを一度も走らせずマージされる (無言の死と同型)。→ 前提そのものを `tests/test_ci_docs_exemption.py` の検査に変え、**破壊して赤くなることまで確認**してから入れる。検査は ast で書く (この教訓の文章自体が違反の形をしている)。なお `**/*.md` はルート直下を取りこぼす実装があるので `**.md` と書く。**(b) `concurrency` は `cancel-in-progress: false` でもグループが効く**: «1グループにつき pending は1つ» なので `group: ...{{ github.ref }}` は連続マージ時に**間の master run をキャンセルする** (待たせるのではない) = CI 記録の無い commit が master に生まれ、上の「失敗一覧の一致」で切り分ける手順が壊れる。→ master push には `github.run_id` を混ぜて**毎回ユニークなグループ**にする。**(c) 「速くなるはず」を根拠として残さない**: `cache: pip` を「140秒→15秒」と見込んだが cache hit 実測は128秒 = **縮んだのは12秒**。律速はダウンロードではなく **Windows の site-packages への展開 (78パッケージで53秒)** で、DLキャッシュは原理的にここを縮められない。外れた見込みを残すと後の読み手が「pip は対処済み」と誤解して本当の律速に手を付けなくなる → **実測で置き換える**
- **import 時に `sys.exit()` してはいけない**。`bot_discord.py` が module scope で `if not DISCORD_TOKEN: sys.exit(1)` していたため、`.env` を持たない環境では `import bot_discord` そのものが失敗し、(a) `verify_project.py` の import 整合性検査が原理的に成立せず、(b) コマンド定義を読むだけのテストまで落ちた。**起動しようとした時に初めて落ちる** (`run_bot()` の先頭で判定する)。import は副作用でプロセスを殺さない

### E. 単発

- **リンクを「出す側」と「解決する側」は必ず対で直す**。出典を `[[ファイル名]]` から `[[ファイル名#見出し]]` にした瞬間、`lib_obsidian_utils.get_obsidian_uri_from_link` が `foo.md#見出し.md` を探して必ず空文字を返すようになり、Discord の「Obsidianで開く」ボタンが**無言で消える**ところだった (`obsidian://open` の `file` は見出しを解釈しない)。ボタンが出ないだけでログには一行も残らない = 無言の死。`format_wikilink` (出す) と `split_heading_anchor` (解決する) を同じモジュールに並べ、片方だけ変えられないようにする。**リンク形式を変えるときは `\[\[(.*?)\]\]` を検索している箇所を先に洗う**
- **stacked PR の base ブランチを `--delete-branch` 付きでマージすると、積んでいた PR が自動クローズされる**。閉じた PR は base の変更も再オープンもできない (`Cannot change the base branch of a closed pull request`) ので作り直しになる。**stack しているときは `--delete-branch` を後回しにする**
- `tool_create_note` fails on duplicate filenames -> use timestamp suffix fallback
- `todoist-api-python` v3+: use `filter_tasks(query=)` not `get_tasks(filter=)`

## Encoding Safety Rules (Windows Japanese Environment)
> 2026-02-27: AI エージェントがスクリプト単位で直さず OS/セッション単位で PYTHONIOENCODING を
> 設定し、システム全体を汚染した。**最小スコープを超えない**。

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

LEVEL 2 以上が必要に見えたら **STOP してユーザーに確認する**。
正しい書き方の具体例・判断フロー・既知バグは skill `windows-encoding` (該当ファイル編集時に自動ロード)。

## Workflow
1. Analyze the request
2. Check existing files (`agent_hello.py`, relevant `tool_*.py`)
3. Propose changes (Plan Mode for complex tasks)
4. Execute changes and run verification
5. If Claude makes a mistake -> fix it, then update this CLAUDE.md with the lesson

## AI Instruction Principles (Vibe Coding 2.0)
- **1 session, 1 feature**: 1つの会話に複数の修正/機能を混ぜない。新しい機能・バグ修正は新しいチャットで
- **50-line checkpoints**: 複雑な実装は ~50行ごとに止めてレビューを待つ。一度に大きな塊を生成しない
- **Commit after every working state**: 動いた時点で即コミット。次のタスクへ進む前に
- **Stop and reset when confused**: 進捗のないデバッグループに入ったらコード編集をやめ、平易な言葉で目的を言い直して Plan Mode へ戻る
- **効果的な時間削減は根源的な財産**: 再テスト・再検証のたびに払う固定費を削る変更 (絞り込み実行・並列化・キャッシュ) は、機能追加に見えなくても後回しにしない (`scripts/run_test_sweep.py --filter` 追加の根拠)。ただし削減効果が明確でない先回りの汎用化までは正当化しない

## Plan Mode Protocol
- Complex tasks (3+ files, new features, architectural changes): always start in Plan Mode
- **Verification plan is MANDATORY**: Every plan must include HOW to prove it works
- If implementation goes wrong, **STOP immediately** and return to Plan Mode — never push through
- Never micromanage — give intent, let Claude execute
- テンプレート: `## Goal` / `## Steps` (1.[Step]→[Expected result]) / `## Verification` (`- [ ]` で検証手段) / `## Rollback`

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

## Output Style: Explanatory + Learning
- 変更するときは常に **why** を説明する (what だけで終わらせない)。非自明なロジックには根拠を、
  新パターン導入時はトレードオフを、バグ修正では真因を先に述べる
- **Learning mode**: 未知の概念を聞かれたら「1段落の要約 + 具体的なコード例 + よくある落とし穴」で答える
- **Decision log**: アーキテクチャ上の決定は Decision → Reason → Alternatives considered で残す

## Visualization Protocol
- 複雑なシステムの説明には **ASCII図** か **Mermaid** を使う (データフロー=シーケンス図 /
  アーキテクチャ=コンポーネント図 / 状態遷移=状態図)。要素が5個を超えたら凡例を付ける
- 網羅的な説明が要るときは **HTML プレゼン**の生成を提案する

## Sub-agent Delegation Protocol

| Agent (`.claude/agents/`) | 使いどころ |
|---|---|
| `verifier` | PR説明文/最終要約/ドキュメントの主張が証跡に基づいているか検査 (コミット/投稿前) |
| `test-writer` | 新規バグ修正・機能に対応するテストを既存作法 (LEVEL 0 encoding, ast静的検査) で追加 |
| `bug-pattern-archivist` | バグ修正後の教訓を CLAUDE.md 本体か該当 skill かを判断して追記 (肥大化防止) |

**委譲する条件**: 3ファイル以上に及ぶ / プロジェクト全域のスキャン (`/techdebt`, 全ファイル grep) /
ドキュメント生成 (README, API docs, CHANGELOG) / テストスイート一式の作成 / 大量データ処理

**Rules**: 重いタスクはプロンプトに `use subagents` を付ける / 各サブエージェントには
ONE focused task だけ渡す / 結果はファイルに書かせる (メインコンテキストに返さない) /
メインはマージ・レビュー・コミットのみ

**Do NOT**: ❌ `CLAUDE.md` や `.env` を編集させる / ❌ 50行未満の変更で起動する /
❌ 3体以上を直列に連鎖させる (並列化する) / ❌ 出力をレビューせずにコミットする
