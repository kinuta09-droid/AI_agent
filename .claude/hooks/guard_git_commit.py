# -*- coding: utf-8 -*-
"""PreToolUse hook (Bash): `git commit` の前に verify_project.py を強制実行する。

CLAUDE.md Coding Standards & Stability:
  「Guardian: Run `py verify_project.py` before any significant commit or bot restart.」
は今まで Claude の記憶力に依存する「お願い」でしかなく、実行を忘れても誰も気付けなかった。
このフックは `git commit` を含む Bash コマンドを検知したら verify_project.py を
実際に実行し、失敗していればコミット自体をブロックする。

stdin: PreToolUse の JSON (tool_input.command)
exit 2 = ブロック (verify_project.py 失敗、または未検証)
exit 0 = 許可
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

COMMIT_PATTERN = re.compile(r"(^|[;&|]\s*)git\s+commit\b")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not COMMIT_PATTERN.search(command):
        return 0

    project_dir = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        return 0

    verify_script = Path(project_dir) / "verify_project.py"
    if not verify_script.exists():
        return 0

    launcher = "py" if os.name == "nt" else "python3"
    try:
        result = subprocess.run(
            [launcher, str(verify_script)],
            cwd=str(project_dir),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(
            f"Blocked: verify_project.py を実行できませんでした ({e})。"
            " 手動で `py verify_project.py` を実行して確認してください。",
            file=sys.stderr,
        )
        return 2

    if result.returncode != 0:
        tail = "\n".join((result.stdout or "").splitlines()[-40:])
        print(
            "Blocked: verify_project.py が失敗しています。CLAUDE.md の"
            " Guardian 規約により、これを解消するまでコミットできません。\n\n"
            f"{tail}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
