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

- **`sys.stdout.reconfigure(encoding='utf-8')` の無いテストは、cp932 コンソールで中身が全部通っていても赤くなる**。`tests/test_verifier.py` がこれで、失敗の原因が本物の不合格なのか文字化けなのか区別できない状態だった (18件すべて通っていた)。テストを新規追加するときは冒頭の LEVEL 0 の1行を必ず入れる
- 子プロセスの stdout をファイルへリダイレクトすると Python は既定でブロックバッファリングし、`print()` が 8KB 貯まるまでログに現れない。`logs/launcher.log` は起動失敗時に真っ先に読むログなので、**ランチャー (`scripts/start_arms.bat` の `py -u main.py`) と子 (`main.py` の `Popen([python, "-u", script])`) の両方**に `-u` が要る (片方だけだと `[Monitor]` 行か `[Discord]` 行のどちらかが消える)
- Windows PowerShell 5.1 は `.ps1` を **BOM が無いと ANSI (cp932)** として読む -> 日本語コメント入りの `.ps1` を BOM なし UTF-8 で保存すると全体が構文エラーになる。`open(p, 'w', encoding='utf-8-sig')` で保存すること。文字列内の環境変数も `"$env:X\..."` ではなく `"$($env:X)\..."` と部分式で囲む
- **タスクスケジューラから .bat を直接実行すると可視のコンソールウィンドウが残る**。その × を押すと `CTRL_CLOSE_EVENT` が飛んで `main.py`・Bot・Scheduler が一斉に死ぬ (タスクの Last Result が `-1073741510` = `0xC000013A` = STATUS_CONTROL_C_EXIT なら必ずこれ)。デスクトップに「スタック全滅ボタン」を置くのと同じ -> `scripts/start_arms.bat` は `wscript //B scripts/arms_hidden.vbs` 経由で非表示起動し、実処理は `scripts/arms_core.bat` に置く (2026-07-26 実障害)
- **`.bat` は ASCII のみで書く**。cmd.exe は `.bat` を UTF-8 ではなく OEM コードページ (cp932) で読むため、UTF-8 の日本語コメントが化ける。特に `。` (E3 80 82) は末尾バイト 0x82 が cp932 の先行バイトになり **行末の CR を食べる** -> 直後の実行行がコメント行と連結してコマンドとして実行される (`'<化けた文字>py' は、内部コマンドまたは...` + 終了コード 9009)。日本語の説明は `.md` 側に書く。`.vbs` も同様 (WSH は ANSI 読み) に ASCII のみ
- **実行中の .bat を編集しない**。cmd.exe はバッチをバイトオフセットで逐次読み直すため、実行中に行を挿入すると次のコマンド読み込み位置がずれ、同じ行を再実行したりする (2026-07-26: `start_arms.bat` 編集中に `py main.py` 行が二度走り、停止したはずのスタックが独りでに復活した)
