# -*- coding: utf-8 -*-
"""PreToolUse hook (Bash): エンコーディング設定のスコープ逸脱を機械的に禁止する。

守る規約 (CLAUDE.md Encoding Safety Rules の NEVER 節と対応):
  - LEVEL 4: `setx PYTHONIOENCODING ...` (レジストリ永続化) は絶対禁止
  - LEVEL 3: PowerShell プロファイル ($PROFILE) / .bashrc への追記は
             ユーザーの明示指示なしに禁止
  - `chcp 65001` の永続変更 (レジストリ経由) も禁止

stdin: PreToolUse の JSON (tool_input.command)
exit 2 = ブロック
exit 0 = 許可
"""
import json
import re
import sys

BLOCKED_PATTERNS = [
    (
        re.compile(r"\bsetx\s+PYTHONIOENCODING\b", re.IGNORECASE),
        "setx PYTHONIOENCODING はレジストリへの永続書き込み (LEVEL 4) です。"
        " 絶対に実行しないでください。個別プロセスに閉じた"
        " subprocess(env={...}) (LEVEL 1) を使ってください。",
    ),
    (
        re.compile(r"\$PROFILE\b|Microsoft\.PowerShell_profile\.ps1", re.IGNORECASE),
        "PowerShell プロファイルの変更 (LEVEL 3) はユーザーの明示指示なしに"
        " 禁止されています。本当に必要なら先にユーザーへ確認してください。",
    ),
    (
        re.compile(r"~/\.bashrc|\.bash_profile"),
        ".bashrc / .bash_profile への変更 (LEVEL 3) はユーザーの明示指示なしに"
        " 禁止されています。本当に必要なら先にユーザーへ確認してください。",
    ),
]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command:
        return 0

    for pattern, reason in BLOCKED_PATTERNS:
        if pattern.search(command):
            print(f"Blocked: {reason} (skill windows-encoding 参照)", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
