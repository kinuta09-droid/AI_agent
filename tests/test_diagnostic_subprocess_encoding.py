# -*- coding: utf-8 -*-
"""診断を出力する層の subprocess 呼び出しが encoding を明示していることの静的検査。

このテストが存在する理由 (2026-07-30):
  `verify_project.py` の `check_import` が
  `subprocess.run(cmd, capture_output=True, text=True)` だった。
  `text=True` だけだと **親プロセスのロケール既定** で復号するため、
    - 開発機 (cp932) では日本語 traceback が偶然復号でき、何も起きない
    - GitHub Actions の runner (cp1252) では reader thread が UnicodeDecodeError で
      死に、`result.stderr` が空になる
  `returncode` は正しく返るので Guardian は「Import Error: bot_discord」とだけ
  出力し、**なぜ落ちたのかを一切説明できない**状態になった (CI 初起動で実際に発生)。

  つまりこれは「手元では絶対に再現しない」欠陥であり、動的テストでは捕まえられない。
  さらに悪いのは、これが **診断を出力する側** (Guardian・hook) で起きたこと —
  欠陥そのものより発見が遅れる。よって ast でソースを走査して縛る。

対象は「失敗を人間に説明する責務を持つ層」に限る (verify_project.py と
.claude/hooks/)。`AutoAudio/` 等の一回きりのスクリプトまで広げると、
本来の意図がぼやけて誰も直さなくなる。

実行: py tests/test_diagnostic_subprocess_encoding.py
"""
import ast
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 「失敗の理由を出力する責務」を持つファイル群
TARGETS = [
    os.path.join(_PROJECT_ROOT, "verify_project.py"),
    os.path.join(_PROJECT_ROOT, ".claude", "hooks", "guard_git_commit.py"),
    os.path.join(_PROJECT_ROOT, ".claude", "hooks", "guard_bash_encoding.py"),
    os.path.join(_PROJECT_ROOT, ".claude", "hooks", "guard_edit_encoding.py"),
]

_CAPTURING = {"text", "capture_output", "universal_newlines"}

_PASS = []
_FAIL = []


def check(name, cond, detail=""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def find_violations(path):
    """テキストモードで出力を取り込みつつ encoding を渡していない呼び出しを返す。

    Returns: [(lineno, 渡されているキーワードの一覧), ...]
    """
    with open(path, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), path)

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # subprocess.run / Popen / check_output
        if not (isinstance(func, ast.Attribute)
                and func.attr in ("run", "Popen", "check_output")
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        # 出力をテキストとして取り込む呼び出しだけが対象。
        # バイト列で受けるならデコードしないので encoding は不要。
        if not (kwargs & _CAPTURING):
            continue
        if "encoding" not in kwargs:
            out.append((node.lineno, sorted(kwargs)))
    return out


def test_no_locale_default_decoding():
    print("\n[1] 診断層の subprocess が encoding を明示している")
    for path in TARGETS:
        rel = os.path.relpath(path, _PROJECT_ROOT)
        if not os.path.exists(path):
            check(f"{rel} が存在する", False, "対象ファイルが見つからない (移動/改名?)")
            continue
        violations = find_violations(path)
        check(
            f"{rel}: ロケール既定で復号する呼び出しが無い",
            not violations,
            "encoding= が無い: " + ", ".join(f"L{ln} ({','.join(kw)})" for ln, kw in violations),
        )


def test_detector_actually_detects():
    """検査自身が機能していることの確認。

    「違反0件でした」は、検査が壊れていても同じ出力になる (このリポジトリで
    繰り返し出ている「表示と実態の食い違い」)。既知の違反形を渡して
    ちゃんと拾えることを確かめる。
    """
    print("\n[2] 検査自身が違反を拾えるか")
    probe = os.path.join(_PROJECT_ROOT, "tests", "_tmp_subproc_probe.py")
    cases = [
        ("text=True のみ", "import subprocess\nsubprocess.run(['x'], text=True)\n", True),
        ("capture_output のみ",
         "import subprocess\nsubprocess.run(['x'], capture_output=True)\n", True),
        ("universal_newlines のみ",
         "import subprocess\nsubprocess.Popen(['x'], universal_newlines=True)\n", True),
        ("encoding 明示",
         "import subprocess\nsubprocess.run(['x'], capture_output=True, "
         "encoding='utf-8', errors='replace')\n", False),
        ("バイト列で受ける (encoding 不要)",
         "import subprocess\nsubprocess.run(['x'], stdout=subprocess.PIPE)\n", False),
    ]
    try:
        for label, src, should_flag in cases:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("# -*- coding: utf-8 -*-\n" + src)
            got = bool(find_violations(probe))
            check(f"{'検出' if should_flag else '誤検知しない'}: {label}",
                  got == should_flag,
                  f"expected flag={should_flag}, got {got}")
    finally:
        if os.path.exists(probe):
            os.remove(probe)


if __name__ == "__main__":
    test_no_locale_default_decoding()
    test_detector_actually_detects()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
