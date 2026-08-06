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
