---
name: process-lifecycle
description: Bot/Scheduler の多重起動防止、PID/boot_id 管理、watchdog、Windows プロセス終了 (os.kill/OpenProcess)、タスクスケジューラ登録まわりの既知バグパターン。lib/pid_guard.py, lib/watchdog.py, scripts/arms_ctl.py, main.py の起動・停止ロジックを触るとき、または「Botが無言で起動しない」「プロセスが多重起動している」を調査するときに読む。
---

# Process Lifecycle & Windows Process Management

- **Bot/Scheduler duplicate processes -> always acquire via `lib.pid_guard.acquire()`**. Never hand-roll a "check PID file then write it" guard: the two steps are not atomic, so processes starting within seconds of each other (heavy imports run *before* the guard) all decide "no live instance" and all write the lock. This let a 2nd Discord bot connect to the gateway, so one `/agent` ran twice in parallel and `logs/skill_observations.jsonl` got same-second duplicate entries (2026-07-17)
- Duplicate entries in `logs/skill_observations.jsonl` are **not** a nested-wrapper bug: `observe()` has exactly one call site (`engine_plan_execute.py`) and the registry stores raw callables. Same-second entries with *different* args = two concurrent processes, so count the processes first (`Get-CimInstance Win32_Process`, check who holds the Discord socket)
- PID は再起動をまたいで一意ではない。電源断で残った PID ファイルの PID が次のブートで再利用されていると `is_process_alive` が(安全側に倒れて)「生存」と答え、`acquire()` が永久に False -> Bot が無言で起動しない。`main.py` は即 exit を 5秒毎に再起動するだけで heartbeat 判定に到達しないため通知も出ない。**PID と一緒にブート ID を記録**して前ブートの残骸を無条件 stale にする (`lib/pid_guard.boot_id`)
- heartbeat が守るのは「上がったプロセスが止まったか」だけ。「そもそも上がってこない」は守備範囲外 -> `lib/watchdog.is_fast_exit` / `restart_delay` / `should_alert` で crash loop を検知・通知する。PC 起動時に立ち上がらないときの確認順は `scripts/AUTOSTART.md`
- 2026-07-25 朝、PC再起動後にBotが無言で起動しない事象が発生 -> 実機で `Get-ScheduledTask` を確認したところ `ARMS Launcher` タスク自体が未登録だった。`scripts/AUTOSTART.md` の `schtasks /create` は手動一回限りの手順で git 管理外のため、コード側をどれだけ直しても `main.py` が一度も走らなければ無意味。新しいマシン/ユーザーでセットアップする際は必ず `schtasks /query /tn "ARMS Launcher"` で登録有無を先に確認する
- Windows `os.kill(pid, SIGTERM)` throws `OSError [WinError 87]` for dead PIDs -> catch `OSError`
- `os.kill` は CPython 内部で `OpenProcess(PROCESS_ALL_ACCESS)` を要求する。タスクスケジューラが `/rl highest` で起動した高整合性プロセスを非管理者プロセス (`scripts/arms_ctl.py` 等) から終了させようとすると `PermissionError [WinError 5]` になる。**「殺せない」のではなく要求権限が過剰なだけ**で、終了に必要なのは `PROCESS_TERMINATE` のみ (同一ユーザーなら整合性レベルをまたいでも DACL で許可される) -> `lib/watchdog._terminate_via_handle` にフォールバックする
- `OpenProcess` failing does **not** mean the process is dead: `ERROR_ACCESS_DENIED(5)` = alive but not queryable. Treating it as dead re-opens the duplicate-startup hole -> fall back to "alive" when in doubt (see `lib/pid_guard.is_process_alive`)
- WMI `CommandLine LIKE '%foo%'` matches **your own query process** -> filter on `Name = 'python.exe'` before believing a stray process exists
