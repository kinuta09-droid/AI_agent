# -*- coding: utf-8 -*-
"""SessionStart hook: origin/<branch> がローカルの先を行っていないか確認する。

背景 (2026-08-29): ユーザーは複数の Claude セッション (ローカル + クラウド) を同時に
同じリポジトリで走らせることがある。ローカルセッションが origin の最新を知らないまま
数十分かけて機能を実装し、push直前の git fetch で初めて「同じ課題を別セッションが
既に PR としてマージ済み」と分かり、作業が丸ごと重複した実例がある
(memory: agent-feedback-category-parallel-session-collision)。
push直前ではなく**セッション開始時点**で気付ければ、着手前に方針を確認できる。

git操作に失敗しても (オフライン、gitリポジトリでない等) セッション開始を妨げないよう
常に exit 0 で終わる (このフックは情報提示のみで、何かをブロックする設計ではない)。
"""
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')

_TIMEOUT = 5


def _run(args):
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=_TIMEOUT,
    )


def main() -> int:
    try:
        _run(["git", "rev-parse", "--git-dir"]).check_returncode()
    except Exception:
        return 0  # gitリポジトリでない

    try:
        fetch = subprocess.run(["git", "fetch", "--quiet"], capture_output=True, timeout=15)
    except Exception:
        return 0  # オフライン等。セッション開始を妨げない
    if fetch.returncode != 0:
        # fetch失敗 (認証切れ等) を無視して進むと、古いorigin追跡データのまま
        # 「乖離なし」と誤判定しかねない。何も分からない状態なので黙って抜ける。
        return 0

    try:
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        if not branch or branch == "HEAD":
            return 0  # detached HEAD 等、対象ブランチが定まらない

        upstream = f"origin/{branch}"
        log = _run(["git", "log", "--oneline", f"HEAD..{upstream}"])
    except Exception:
        return 0

    commits = [line for line in (log.stdout or "").splitlines() if line.strip()]
    if commits:
        print(f"⚠️ {upstream} にローカル未取得のコミットが{len(commits)}件あります "
              "(別セッションが並行して作業した可能性):")
        for line in commits[:10]:
            print(f"  {line}")
        print(f"着手前に `git log HEAD..{upstream}` で内容を確認してください。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
