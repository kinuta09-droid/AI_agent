# -*- coding: utf-8 -*-
"""PreToolUse hook (Bash|PowerShell): `git commit` の前に verify_project.py --fast を強制実行する。

CLAUDE.md Coding Standards & Stability:
  「Guardian: Run `py verify_project.py` before any significant commit or bot restart.」
は今まで Claude の記憶力に依存する「お願い」でしかなく、実行を忘れても誰も気付けなかった。
このフックは `git commit` を含むシェルコマンドを検知したら verify_project.py を
実際に実行し、失敗していればコミット自体をブロックする。

なぜ --fast なのか (2026-07-30 判断ゲートA / 2026-07-31 に理由を更新):
  当時フル実行は実機 Windows で **174秒**。初版はこれを timeout=180 で待っており、
  余裕はわずか6秒しか無かった。Bot/Scheduler が常駐している通常状態では
  容易に超え、超えた瞬間に **全コミットが間欠的にブロックされる**。
  しかもメッセージが「実行できませんでした」なので、検証が失敗したのか
  時間が足りなかったのか読み手に区別できなかった。

  2026-07-31 に import 検査をバッチ化してフルは **約8秒** になった
  (172.40秒 -> 7.5秒)。timeout 逼迫という当初の理由は消えたが、
  **--fast のまま維持する**: コミット毎に 1.3秒 -> 8秒 は6倍の後退で、
  「気軽に細かくコミットする」という行動そのものを潰すため。
  --fast (実測 約1.3秒) は構文・エンコーディング・未定義名・Discord アンチパターン・
  例外握りつぶしを全て検査する。落ちるのは import 整合性 [3][4][6] だけで、
  そこは CI の guardian ジョブ (フル実行・ブロッキング) が担保する。

なぜ PowerShell も見るのか:
  初版は `tool_name != "Bash"` で始まっており、settings.json の matcher も
  `Bash` だけだった。主シェルである PowerShell ツールからの `git commit` は
  verify を一切通らずに素通りしていた (2026-07-30 実測)。

stdin: PreToolUse の JSON (tool_input.command)
exit 2 = ブロック (verify_project.py 失敗、または実行不能)
exit 0 = 許可
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stderr.reconfigure(encoding='utf-8')

# このフックが対象とするシェル系ツール。settings.json の matcher と同期させること
# (ズレの検出は tests/test_hook_guards.py が担当)。
SHELL_TOOLS = ("Bash", "PowerShell")

# 行頭 / `;` `&` `|` の後ろに現れる `git commit`。PowerShell も Bash も
# 複数コマンドを改行や `;` で連ねられるので MULTILINE で行頭も見る。
COMMIT_PATTERN = re.compile(r"(^|[;&|]\s*)git\s+commit\b", re.MULTILINE)

# --fast は実測 0.6 秒。20秒あればディスクが刺さっている等の異常時以外は必ず終わる。
VERIFY_TIMEOUT_SEC = 20


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if payload.get("tool_name") not in SHELL_TOOLS:
        return 0

    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not COMMIT_PATTERN.search(command):
        return 0

    # cwd が payload に無い / CLAUDE_PROJECT_DIR が環境に export されていない場合に
    # 「プロジェクトが分からないので素通し」にすると、ガードが無言で無効化される。
    # このスクリプトは <project>/.claude/hooks/ に置かれているので、自分の位置から
    # プロジェクトルートを決定的に求められる。最後の砦としてそれを使う。
    project_dir = (
        payload.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or str(Path(__file__).resolve().parents[2])
    )

    verify_script = Path(project_dir) / "verify_project.py"
    if not verify_script.exists():
        # 別リポジトリでの作業中など。ここは本当に検査対象外なので通す。
        return 0

    launcher = "py" if os.name == "nt" else "python3"
    try:
        result = subprocess.run(
            [launcher, str(verify_script), "--fast"],
            cwd=str(project_dir),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=VERIFY_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # 「検証が失敗した」ではなく「検証が終わらなかった」。混同させない。
        print(
            f"Blocked: verify_project.py --fast が {VERIFY_TIMEOUT_SEC} 秒で終わりませんでした"
            " (検証が失敗したのではなく、完了しなかった)。"
            " 手動で `py verify_project.py --fast` を実行し、原因を確認してください。",
            file=sys.stderr,
        )
        return 2
    except OSError as e:
        print(
            f"Blocked: verify_project.py を起動できませんでした ({type(e).__name__}: {e})。"
            f" `{launcher}` にパスが通っているか確認してください。",
            file=sys.stderr,
        )
        return 2

    if result.returncode != 0:
        tail = "\n".join((result.stdout or "").splitlines()[-40:])
        print(
            "Blocked: verify_project.py --fast が失敗しています。CLAUDE.md の"
            " Guardian 規約により、これを解消するまでコミットできません。\n\n"
            f"{tail}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
