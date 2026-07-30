# -*- coding: utf-8 -*-
"""PreToolUse hook (Edit|Write): CLAUDE.md の Encoding Safety Rules を機械的に強制する。

守る規約 (skill windows-encoding / CLAUDE.md Encoding Safety Rules と対応):
  - `.bat` / `.vbs` は ASCII のみ (cp932 が日本語コメントを化けさせ、
    `。` の末尾バイトが行末 CR を食う実障害があった)
  - `os.environ["PYTHONIOENCODING"] = ...` の代入は LEVEL 2 違反として禁止

stdin: PreToolUse の JSON (tool_name, tool_input.file_path,
       tool_input.content [Write] / tool_input.new_string [Edit])
exit 2 = ブロック (stderr が Claude に理由として渡る)
exit 0 = 許可
"""
import json
import re
import sys


def _changed_text(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or ""
    text = _changed_text(tool_name, tool_input)
    if not text:
        return 0

    lower_path = file_path.lower()

    if lower_path.endswith((".bat", ".vbs")):
        non_ascii = [ch for ch in text if ord(ch) > 127]
        if non_ascii:
            sample = "".join(non_ascii[:10])
            print(
                f"Blocked: {file_path} に非ASCII文字が含まれています ({sample!r} 等)。"
                " cmd.exe は .bat/.vbs を UTF-8 ではなく cp932 で読むため文字化けし、"
                " 特に「。」は行末CRを食って直後の行と連結する実障害があります"
                " (skill windows-encoding 参照)。日本語の説明は隣接する .md に書き、"
                " .bat/.vbs 本体は ASCII のみにしてください。",
                file=sys.stderr,
            )
            return 2

    if re.search(r"""os\.environ\s*\[\s*['"]PYTHONIOENCODING['"]\s*\]\s*=""", text):
        print(
            "Blocked: os.environ[\"PYTHONIOENCODING\"] への代入は LEVEL 2 違反です"
            " (全ての子プロセスを汚染します)。sys.stdout.reconfigure(encoding='utf-8')"
            " (LEVEL 0) か、subprocess 呼び出し時の env={**os.environ, ...} 注入"
            " (LEVEL 1、その呼び出しだけに限定) を使ってください (skill windows-encoding 参照)。",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
