---
name: windows-encoding
description: Windows/日本語環境でのエンコーディング・.bat・.ps1・cp932・BOM・ランチャー起動まわりの既知バグパターン。.bat/.ps1/.vbs の編集、PowerShellプロファイル変更、subprocess/stdout のエンコーディング設定、タスクスケジューラ経由の起動スクリプトを触るときに読む。
---

# Windows Encoding & Launcher Safety

## Scope Hierarchy — Never exceed the minimum required scope
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

> Lesson learned: 2026-02-27 — AI agent caused system-wide contamination by setting
> PYTHONIOENCODING at the OS/session level instead of fixing encoding at the script level.

## Known Bug Patterns

- **`sys.stdout.reconfigure(encoding='utf-8')` の無いテストは、cp932 コンソールで中身が全部通っていても赤くなる**。`tests/test_verifier.py` がこれで、失敗の原因が本物の不合格なのか文字化けなのか区別できない状態だった (18件すべて通っていた)。テストを新規追加するときは冒頭の LEVEL 0 の1行を必ず入れる。**LLM の出力を print するテストではこれが「実行ごとに緑にも赤にもなる」形で出る**: `tests/test_bitnet_practical.py` は BitNet の生成に簡体字 (仅 等) が混ざった回だけ `UnicodeEncodeError` で落ちていた。手で叩くと緑・スイープ (stdout がパイプ) だと赤、しかも生成内容次第なので再現しない — **スイープの結果そのものが信用できなくなる**。「基準 70/0 が緑だった」は「その回の生成に該当文字が無かった」でしかない (2026-07-31)
- 子プロセスの stdout をファイルへリダイレクトすると Python は既定でブロックバッファリングし、`print()` が 8KB 貯まるまでログに現れない。`logs/launcher.log` は起動失敗時に真っ先に読むログなので、**ランチャー (`scripts/start_arms.bat` の `py -u main.py`) と子 (`main.py` の `Popen([python, "-u", script])`) の両方**に `-u` が要る (片方だけだと `[Monitor]` 行か `[Discord]` 行のどちらかが消える)
- Windows PowerShell 5.1 は `.ps1` を **BOM が無いと ANSI (cp932)** として読む -> 日本語コメント入りの `.ps1` を BOM なし UTF-8 で保存すると全体が構文エラーになる。`open(p, 'w', encoding='utf-8-sig')` で保存すること。文字列内の環境変数も `"$env:X\..."` ではなく `"$($env:X)\..."` と部分式で囲む
- **タスクスケジューラから .bat を直接実行すると可視のコンソールウィンドウが残る**。その × を押すと `CTRL_CLOSE_EVENT` が飛んで `main.py`・Bot・Scheduler が一斉に死ぬ (タスクの Last Result が `-1073741510` = `0xC000013A` = STATUS_CONTROL_C_EXIT なら必ずこれ)。デスクトップに「スタック全滅ボタン」を置くのと同じ -> `scripts/start_arms.bat` は `wscript //B scripts/arms_hidden.vbs` 経由で非表示起動し、実処理は `scripts/arms_core.bat` に置く (2026-07-26 実障害)
- **`.bat` は ASCII のみで書く**。cmd.exe は `.bat` を UTF-8 ではなく OEM コードページ (cp932) で読むため、UTF-8 の日本語コメントが化ける。特に `。` (E3 80 82) は末尾バイト 0x82 が cp932 の先行バイトになり **行末の CR を食べる** -> 直後の実行行がコメント行と連結してコマンドとして実行される (`'<化けた文字>py' は、内部コマンドまたは...` + 終了コード 9009)。日本語の説明は `.md` 側に書く。`.vbs` も同様 (WSH は ANSI 読み) に ASCII のみ
- **実行中の .bat を編集しない**。cmd.exe はバッチをバイトオフセットで逐次読み直すため、実行中に行を挿入すると次のコマンド読み込み位置がずれ、同じ行を再実行したりする (2026-07-26: `start_arms.bat` 編集中に `py main.py` 行が二度走り、停止したはずのスタックが独りでに復活した)
- **hook はマッチする「ツール名」で守備範囲が決まる。主シェルが漏れていないか必ず実測する**。`.claude/hooks/guard_bash_encoding.py` / `guard_git_commit.py` は初版が `if payload.get("tool_name") != "Bash": return 0` で始まり、`.claude/settings.json` の matcher も `Bash` だけだった。しかしこのマシンの主シェルは PowerShell なので、**Bash ツールで exit 2 になるのと一字一句同じコマンドが PowerShell ツールでは素通り**する。LEVEL 4 の永続化コマンドも、無検証の `git commit` も塞げていなかった (2026-07-30 実測)。ガードを書いたら「効いた」ではなく「**どのツール経由なら迂回できるか**」を確かめる。判定は `SHELL_TOOLS` に一元化し、`tests/test_hook_guards.py` が settings.json の matcher とのズレを検出する。PowerShell 固有の LEVEL 4 経路 (`[Environment]` クラスの環境変数書き込みを User/Machine スコープで行うもの、`reg add` / `Set-ItemProperty` で `\Environment` や `Command Processor` を書くもの) は Bash 用の正規表現では一切引っかからないので個別に足すこと
- **hook 自身に `sys.stderr.reconfigure(encoding='utf-8')` が要る**。hook がブロック理由を Claude に伝える唯一の経路は stderr で、reconfigure が無いと Windows では cp932 で書かれ日本語の理由が全部化ける。ブロックはできていても「何を直せばよいか」が読めないので実用上は半分壊れている (この規約を強制する hook 自身が LEVEL 0 に違反していた、2026-07-30)
- **コマンド文字列を正規表現で見るガードは「実行する」と「言及する」を区別できない**。禁止コマンドの literal をコミットメッセージ本文に書いた `git commit -m "..."` は、そのままガードに引っかかる (2026-07-30 に実際に発生し、この教訓を書き足す作業自体もヒアドキュメント経由では通らなかった)。回避は `git commit -F <file>` / Write ツールでのファイル経由。「引用の中なら通す」を実装するとヒアドキュメントに隠した実行を見逃す方向の穴になるので、**ガードは安全側 (ブロック) に据え置く**
- **`subprocess` の `text=True` に `encoding=` を付け忘れると、失敗の理由だけが消える**。`verify_project.py` の `check_import` が `subprocess.run(cmd, capture_output=True, text=True)` だったため、cp932 の開発機では日本語 traceback が偶然復号できて何事も起きなかったが、**cp1252 ロケールの GitHub Actions runner では reader thread が `UnicodeDecodeError` で死に、`result.stderr` が空になった**。`returncode` は正しく返るので Guardian は「Import Error: bot_discord」とだけ出力し、**なぜ落ちたのかを一切説明できない**状態になった (2026-07-30 の CI 初起動)。ロケール既定は「マシンによって違う」のではなく「**壊れ方がマシンによって違う**」= 手元では再現しない。`text=True` を書いたら必ず `encoding='utf-8', errors='replace'` を同時に書く (LEVEL 1)。とくに **診断を出力する側のコード** (Guardian・hook・テストランナー) でこれを落とすと、欠陥そのものより発見が遅れる。**子プロセス側の `stdin` も同じ**: 親が `input=` で渡す文字列は親側で utf-8 にできても、子が `sys.stdin` をロケール既定で読めば同じ「開発機では通り CI で落ちる」形になる。子スクリプトの冒頭で `sys.stdin.reconfigure(encoding='utf-8')` まで書くこと (2026-07-31 に Guardian のバッチワーカーで先回りして塞いだ)
- **`git commit` フックが重い検証を呼ぶなら、timeout は実測値に張り付かせない**。初版の `guard_git_commit.py` は `verify_project.py` のフル実行 (実測 174秒) を `timeout=180` で待っており、余裕は6秒しかなかった。Bot/Scheduler 常駐時に超えれば **全コミットが間欠的に封鎖**され、しかもメッセージが「実行できませんでした」なので *検証が失敗した* のか *時間が足りなかった* のか読み手に区別できない。`--fast` (静的検査のみ・0.6秒) を呼び、`TimeoutExpired` と `OSError` を別文言にした (2026-07-30)
