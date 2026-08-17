# -*- coding: utf-8 -*-
"""PreToolUse hook (Bash|PowerShell): エンコーディング設定のスコープ逸脱を機械的に禁止する。

守る規約 (.claude/rules/encoding-safety.md の NEVER 節と対応):
  - LEVEL 4: `setx PYTHONIOENCODING ...` / レジストリ経由の環境変数永続化は絶対禁止
  - LEVEL 3: PowerShell プロファイル ($PROFILE) / .bashrc への追記は
             ユーザーの明示指示なしに禁止
  - `chcp 65001` の永続変更 (Command Processor の Autorun) も禁止

なぜ PowerShell も見るのか (2026-07-30):
  初版は `tool_name != "Bash"` で始まっており、settings.json の matcher も
  `Bash` だけだった。**このマシンの主シェルは PowerShell** なので、
  Bash ツールで exit 2 になるのと完全に同一の文字列が PowerShell ツールでは
  素通りすることを実測で確認した。つまり禁止したはずの LEVEL 4 が
  塞がっていなかった。判定対象は SHELL_TOOLS に一元化し、
  tests/test_hook_guards.py が settings.json の matcher との一致を検査する。

stdin: PreToolUse の JSON (tool_input.command)
exit 2 = ブロック
exit 0 = 許可
"""
import json
import re
import sys

sys.stdin.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# このフックが対象とするシェル系ツール。settings.json の matcher と同期させること
# (ズレの検出は tests/test_hook_guards.py が担当)。
SHELL_TOOLS = ("Bash", "PowerShell")

BLOCKED_PATTERNS = [
    (
        re.compile(r"\bsetx\s+PYTHONIOENCODING\b", re.IGNORECASE),
        "setx PYTHONIOENCODING はレジストリへの永続書き込み (LEVEL 4) です。"
        " 絶対に実行しないでください。個別プロセスに閉じた"
        " subprocess(env={...}) (LEVEL 1) を使ってください。",
    ),
    (
        # PowerShell 版の LEVEL 4。第3引数が User/Machine のときだけレジストリに
        # 永続化される (2引数形と 'Process' は LEVEL 1 相当なので通す)。
        # スコープは文字列でも enum でも書けるため両方を見る:
        #   SetEnvironmentVariable('X','1','Machine')
        #   SetEnvironmentVariable('X','1',[EnvironmentVariableTarget]::Machine)
        # 前者だけを見る正規表現は後者を素通りさせる (2026-07-30 の /grill で実証)。
        re.compile(
            r"SetEnvironmentVariable\s*\([^)]*(?:['\"]|::)\s*(?:User|Machine)\b",
            re.IGNORECASE,
        ),
        "[Environment]::SetEnvironmentVariable(..., 'User'/'Machine') は"
        " レジストリへの永続書き込み (LEVEL 4) で、setx と同じものです。"
        " そのプロセスだけに効かせたいなら $env:NAME = '...' (LEVEL 1) か"
        " subprocess(env={...}) を使ってください。",
    ),
    (
        # reg add / Set-ItemProperty で環境変数・Command Processor Autorun を書く経路。
        # `chcp 65001` の永続化はここ (Autorun) を通る。
        re.compile(
            r"(?:\breg\s+add\b|\bSet-ItemProperty\b|\bNew-ItemProperty\b)"
            r"[^\n]*(?:\\Environment\b|:\\Environment\b|Command\s+Processor)",
            re.IGNORECASE,
        ),
        "レジストリの Environment / Command Processor を書き換える操作 (LEVEL 4) です。"
        " 環境変数や chcp をシステム全体に永続化しないでください"
        " (2026-02-27 の汚染事故と同型)。",
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

    if payload.get("tool_name") not in SHELL_TOOLS:
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
