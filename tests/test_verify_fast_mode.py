# -*- coding: utf-8 -*-
"""`verify_project.py --fast` が「速いが空ではない」ことの回帰テスト

このテストが存在する理由 (2026-07-30):
  `.claude/hooks/guard_git_commit.py` は `git commit` のたびに --fast を呼び、
  失敗すればコミットをブロックする。つまり **--fast が実際に何を検査しているかが、
  commit ゲートの実効性そのもの**。
  --fast が無言で no-op に近づいても (例: セクションを飛ばしすぎる、
  例外を飲んで緑を返す) 誰も気付けず、フックは「見せかけの門」になる。

  そこで2方向から縛る:
    - **飛ばしてよいもの**: [3] import整合性 / [4] TOOLSレジストリ /
      [6] Briefing は --fast では実行されない (これらがフル174秒の主因)
    - **飛ばしてはいけないもの**: [1] 構造 / [2] 構文・エンコーディング・未定義名 /
      [5] Discord アンチパターン・例外握りつぶし は --fast でも実行される
  さらに、壊れたファイルを置いたら --fast が実際に赤くなることを確認する
  (「静的検査が走っている」と印字するだけで判定していない、を防ぐ)。

実行: py tests/test_verify_fast_mode.py
"""
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

LAUNCHER = "py" if os.name == "nt" else "python3"
VERIFY = os.path.join(_PROJECT_ROOT, "verify_project.py")

# guard_git_commit.py の VERIFY_TIMEOUT_SEC と同じ意図の上限。
# ここを超えるなら --fast が重い検査を巻き込んでいる。
FAST_BUDGET_SEC = 20

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _run_verify(*args, cwd=_PROJECT_ROOT):
    started = time.monotonic()
    proc = subprocess.run(
        [LAUNCHER, VERIFY, *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    return proc.returncode, proc.stdout or "", time.monotonic() - started


def test_fast_skips_only_the_expensive_sections():
    print("\n[1] --fast が飛ばすもの / 飛ばさないもの")
    code, out, elapsed = _run_verify("--fast")

    check("--fast が緑 (exit 0)", code == 0, f"exit={code}\n{out[-600:]}")
    check(
        f"--fast が {FAST_BUDGET_SEC} 秒以内 (実測 {elapsed:.2f}s)",
        elapsed < FAST_BUDGET_SEC,
        f"{elapsed:.2f}s — 重い検査を巻き込んでいる",
    )

    for marker, label in [("[1]", "パッケージ構造"), ("[2]", "構文・エンコーディング"),
                          ("[5]", "Discordアンチパターン/例外握りつぶし")]:
        check(f"--fast でも {marker} {label} を実行", marker in out, "セクションが出力に無い")

    for marker, label in [("[3]", "import整合性"), ("[4]", "TOOLSレジストリ"),
                          ("[6]", "Briefingプロバイダー")]:
        check(
            f"--fast は {marker} {label} を飛ばす",
            f"\n{marker} " not in out,
            "重いセクションが実行されている (フックが174秒コースに戻っている)",
        )

    check(
        "--fast の緑には「import整合性は未検査」と明示される",
        "--fast" in out and "未検査" in out,
        "フルと同じ緑に見えてしまう",
    )
    # 走ったファイル数が0なら [2] は空回りしている
    check(
        "[2] が実際にソースを走査している",
        "source files scanned" in out and " - Total 0 source files" not in out,
        f"走査件数が読めない: {out[:200]!r}",
    )


def test_fast_actually_fails_on_broken_source():
    print("\n[2] --fast は静的検査で実際に赤くなる")
    # 構文エラーを持つファイルを一時的に置く。verify_project.py は
    # tests/ も走査対象に含むので、ここに置けば [2] に拾われる。
    broken = os.path.join(_PROJECT_ROOT, "tests", "_tmp_broken_syntax_probe.py")
    with open(broken, "w", encoding="utf-8") as f:
        f.write("def broken(:\n    pass\n")
    try:
        code, out, _ = _run_verify("--fast")
        check(
            "構文エラーを置くと --fast が赤 (exit 1)",
            code == 1,
            f"exit={code} — 静的検査が判定に反映されていない",
        )
        check(
            "エラー内容に該当ファイル名が出る",
            "_tmp_broken_syntax_probe" in out,
            f"出力に無い: {out[-400:]!r}",
        )
    finally:
        os.remove(broken)

    # 掃除できたことを確認 (残すと以降の全実行が赤になる)
    code, _, _ = _run_verify("--fast")
    check("プローブ削除後は再び緑", code == 0, f"exit={code} — 一時ファイルが残っている")


if __name__ == "__main__":
    test_fast_skips_only_the_expensive_sections()
    test_fast_actually_fails_on_broken_source()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
