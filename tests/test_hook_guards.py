# -*- coding: utf-8 -*-
"""`.claude/hooks/` のガードが「主シェルでも効いている」ことの回帰テスト

このテストが存在する理由 (2026-07-30):
  3本の hook はどれも「対象ツール名」を自前で持ち、さらに
  `.claude/settings.json` の matcher が4つ目のコピーとして存在していた。
  初版はその全てが `Bash` だけを見ており、**このマシンの主シェルである
  PowerShell ツールからは全ての禁止事項が素通り**していた。
  Bash で exit 2 になるのと一字一句同じコマンドが PowerShell では通る、
  という状態が実測で確認されている
  (= `setx PYTHONIOENCODING utf-8` も `git commit` の無検証も塞げていなかった)。

  同じ値の重複が原因なので、**重複そのものを検査する**ことで再混入を止める:
  各 hook の SHELL_TOOLS と settings.json の matcher が一致しているか、
  そして実際に PowerShell の payload を食わせてブロックされるかを見る。

  もう一点、hook は stderr でしか Claude に理由を伝えられない。
  `sys.stderr.reconfigure(encoding='utf-8')` が無いと Windows では cp932 で
  書き出されて日本語の理由が全部化け、「何を直せばよいか」が読めなくなる
  (skill windows-encoding の LEVEL 0 に hook 自身が違反していた)。

実行: py tests/test_hook_guards.py
"""
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

HOOK_DIR = os.path.join(_PROJECT_ROOT, ".claude", "hooks")
SETTINGS = os.path.join(_PROJECT_ROOT, ".claude", "settings.json")
LAUNCHER = "py" if os.name == "nt" else "python3"

# シェル系 hook (tool_input.command を見るもの) と、その期待対象ツール
SHELL_HOOKS = ["guard_bash_encoding.py", "guard_git_commit.py"]
ALL_HOOKS = SHELL_HOOKS + ["guard_edit_encoding.py"]
EXPECTED_SHELL_TOOLS = {"Bash", "PowerShell"}

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _run_hook(hook: str, payload: dict):
    """hook を実プロセスとして起動し (returncode, stderr) を返す。

    import して関数を呼ぶのではなく実行形態を再現するのは、Claude Code が
    hook を `py <path>` で起動するため。stderr は UTF-8 で読む
    (化けていれば decode ではなく内容で分かるよう errors='replace')。
    """
    proc = subprocess.run(
        [LAUNCHER, os.path.join(HOOK_DIR, hook)],
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=_PROJECT_ROOT,
        timeout=60,
    )
    return proc.returncode, proc.stderr or ""


def _shell_payload(tool_name: str, command: str, cwd: str = _PROJECT_ROOT):
    return {"tool_name": tool_name, "tool_input": {"command": command}, "cwd": cwd}


# --- [1] 対象ツール名の宣言が3箇所で一致しているか -------------------------

def test_shell_tools_declared_consistently():
    print("\n[1] SHELL_TOOLS の宣言と settings.json の matcher")
    matcher_tools = None
    with open(SETTINGS, encoding="utf-8") as f:
        settings = json.load(f)
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        cmds = " ".join(h.get("command", "") for h in entry.get("hooks", []))
        if any(h in cmds for h in SHELL_HOOKS):
            matcher_tools = set(entry.get("matcher", "").split("|"))
    check(
        "settings.json の matcher がシェル系 hook を Bash|PowerShell で登録",
        matcher_tools == EXPECTED_SHELL_TOOLS,
        f"matcher={matcher_tools}",
    )

    for hook in SHELL_HOOKS:
        with open(os.path.join(HOOK_DIR, hook), encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"SHELL_TOOLS\s*=\s*\(([^)]*)\)", src)
        declared = set(re.findall(r"['\"]([A-Za-z]+)['\"]", m.group(1))) if m else set()
        check(
            f"{hook} の SHELL_TOOLS が matcher と一致",
            declared == EXPECTED_SHELL_TOOLS,
            f"declared={declared}",
        )
        check(
            f"{hook} が tool_name を SHELL_TOOLS で判定 (ハードコード禁止)",
            'tool_name") != "Bash"' not in src and "not in SHELL_TOOLS" in src,
            "tool_name の直書き比較が残っている",
        )


# --- [2] 実際に PowerShell payload でブロックされるか ----------------------

def test_encoding_guard_blocks_both_shells():
    print("\n[2] guard_bash_encoding — 両シェルで LEVEL 4/3 を拒否")
    cases = [
        ("setx PYTHONIOENCODING utf-8", "LEVEL 4 setx"),
        ("[Environment]::SetEnvironmentVariable('PYTHONIOENCODING','utf-8','User')", "LEVEL 4 PowerShell版"),
        ("[Environment]::SetEnvironmentVariable('X','1','Machine')", "LEVEL 4 Machine スコープ"),
        # スコープは enum でも書ける。文字列形だけを見る正規表現は素通りさせた (実証済み)
        ("[Environment]::SetEnvironmentVariable('X','1',[EnvironmentVariableTarget]::Machine)",
         "LEVEL 4 enum スコープ (Machine)"),
        ("[Environment]::SetEnvironmentVariable('X','1',[EnvironmentVariableTarget]::User)",
         "LEVEL 4 enum スコープ (User)"),
        (r"reg add HKCU\Environment /v PYTHONIOENCODING /d utf-8", "LEVEL 4 reg add"),
        ("notepad $PROFILE", "LEVEL 3 PowerShell プロファイル"),
        ("echo x >> ~/.bashrc", "LEVEL 3 .bashrc"),
    ]
    for command, label in cases:
        for tool in sorted(EXPECTED_SHELL_TOOLS):
            code, _ = _run_hook("guard_bash_encoding.py", _shell_payload(tool, command))
            check(f"{tool}: {label} をブロック", code == 2, f"exit={code}")


def test_encoding_guard_allows_scoped_alternatives():
    print("\n[3] guard_bash_encoding — 正しいスコープ (LEVEL 0/1) は通す")
    allowed = [
        ("$env:PYTHONIOENCODING = 'utf-8'", "LEVEL 1 プロセス限定 env"),
        ("[Environment]::SetEnvironmentVariable('X','1','Process')", "Process スコープは永続化しない"),
        ("[Environment]::SetEnvironmentVariable('X','1',[EnvironmentVariableTarget]::Process)",
         "Process スコープ (enum) も永続化しない"),
        ("[Environment]::SetEnvironmentVariable('X','1')", "2引数形は既定で Process スコープ"),
        ("py -c \"import sys; sys.stdout.reconfigure(encoding='utf-8')\"", "LEVEL 0"),
        ("git status", "無関係なコマンド"),
    ]
    for command, label in allowed:
        for tool in sorted(EXPECTED_SHELL_TOOLS):
            code, _ = _run_hook("guard_bash_encoding.py", _shell_payload(tool, command))
            check(f"{tool}: {label} を許可", code == 0, f"exit={code}")


def test_commit_guard_sees_powershell():
    print("\n[4] guard_git_commit — PowerShell からの commit も検査対象")
    import tempfile

    # 本物のリポジトリでは verify が緑なので exit 0 になるが、それだけでは
    # 「hook が verify を走らせて緑だった」のか「hook が素通りした」のか区別できない
    # (どちらも exit 0)。verify_project.py が必ず失敗する偽リポジトリを cwd に
    # 渡し、exit 2 になることで **hook が実際に verify を起動した** ことを示す。
    with tempfile.TemporaryDirectory() as fake_repo:
        with open(os.path.join(fake_repo, "verify_project.py"), "w", encoding="utf-8") as f:
            f.write("import sys\nprint('STUB FAILURE')\nsys.exit(1)\n")
        for tool in sorted(EXPECTED_SHELL_TOOLS):
            code, err = _run_hook(
                "guard_git_commit.py",
                _shell_payload(tool, "git commit -m 'x'", cwd=fake_repo),
            )
            check(
                f"{tool}: git commit で verify が起動し、失敗ならブロック (exit 2)",
                code == 2 and "STUB FAILURE" in err,
                f"exit={code} stderr={err[:80]!r}",
            )
            # 同じ偽リポジトリでも commit でなければ verify を起動しない
            code, _ = _run_hook(
                "guard_git_commit.py",
                _shell_payload(tool, "git status", cwd=fake_repo),
            )
            check(f"{tool}: commit 以外では verify を起動しない", code == 0, f"exit={code}")

    # 実リポジトリでは緑なので通る
    for tool in sorted(EXPECTED_SHELL_TOOLS):
        code, _ = _run_hook("guard_git_commit.py", _shell_payload(tool, "git commit -m 'x'"))
        check(f"{tool}: 実リポジトリは verify 緑なので許可 (exit 0)", code == 0, f"exit={code}")

    # 対象外ツールは素通りすること (過剰ブロックの回帰)
    code, _ = _run_hook("guard_git_commit.py", {"tool_name": "Read", "tool_input": {"command": "git commit"}})
    check("対象外ツール (Read) は素通り", code == 0, f"exit={code}")

    # 改行区切りの2コマンド目も検知できること (初版は ^ が MULTILINE でなく素通りだった)
    with open(os.path.join(HOOK_DIR, "guard_git_commit.py"), encoding="utf-8") as f:
        src = f.read()
    check(
        "COMMIT_PATTERN が MULTILINE (改行区切りの git commit を見逃さない)",
        "re.MULTILINE" in src,
        "行頭マッチが文字列先頭限定になっている",
    )


def test_commit_guard_uses_fast_mode():
    print("\n[5] guard_git_commit — --fast を使い timeout と失敗を区別する")
    import ast

    with open(os.path.join(HOOK_DIR, "guard_git_commit.py"), encoding="utf-8") as f:
        src = f.read()
    # フルはバッチ化で 172秒 -> 約8秒 になったが、コミット毎に 1.3秒 -> 8秒 は
    # 6倍の後退なのでフックは --fast のまま (2026-07-31)。
    check("verify_project.py を --fast で呼ぶ", '"--fast"' in src,
          "フル実行はコミット毎に払うには重い (--fast 約1.3秒 に対し約8秒)")

    # 文字列一致で "timeout=180" を探すと、なぜ180が駄目だったかを説明した
    # docstring 自身に当たってしまう。実際に subprocess.run へ渡している値を ast で取る。
    tree = ast.parse(src)
    timeouts = [
        kw.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "run"
        for kw in node.keywords
        if kw.arg == "timeout"
    ]
    check("subprocess.run に timeout が指定されている", len(timeouts) == 1, f"{len(timeouts)}件")
    effective = None
    if len(timeouts) == 1:
        val = timeouts[0]
        if isinstance(val, ast.Constant):
            effective = val.value
        elif isinstance(val, ast.Name):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Assign)
                        and any(getattr(t, "id", None) == val.id for t in node.targets)
                        and isinstance(node.value, ast.Constant)):
                    effective = node.value.value
    check(
        "timeout が実測時間に張り付いていない",
        isinstance(effective, (int, float)) and effective <= 60,
        f"実効 timeout={effective} — 実測に余裕が無いと間欠的に全コミットが封鎖される",
    )
    check(
        "TimeoutExpired と OSError を別メッセージで扱う",
        "except subprocess.TimeoutExpired" in src and "except OSError" in src,
        "検証失敗と実行不能が同じ文言になっている",
    )


# --- [6] stderr が UTF-8 で書かれるか ------------------------------------

def test_all_hooks_reconfigure_stderr():
    print("\n[6] 全 hook が stderr を UTF-8 に reconfigure")
    for hook in ALL_HOOKS:
        with open(os.path.join(HOOK_DIR, hook), encoding="utf-8") as f:
            src = f.read()
        check(
            f"{hook}: sys.stderr.reconfigure(encoding='utf-8')",
            "sys.stderr.reconfigure(encoding='utf-8')" in src,
            "ブロック理由が cp932 で化けて読めなくなる",
        )


# --- [8]/[9] stdin の cp932 誤デコードで検出がバイパスされないこと -----------
#
# 2026-08-17、~/.claude/hooks/guard_ja_style.py (グローバル、git管理外) で
# 「sys.stdin.reconfigure が無いと Windows 既定コンソール(cp932)下で
# json.load(sys.stdin) が UTF-8 ペイロードを誤デコードし、検出ロジックが
# 無言でバイパスされる」という実バグが見つかった。この3 hook も同型の
# json.load(sys.stdin) を持つため、同じ穴が無いかここで機械的に検査する。
#
# 重要: subprocess.run(input=str, encoding=...) の「パイプ書き込み」経由では
# このバグは再現しない (実測済み)。実際に stdin= へファイルハンドルを渡す
# (シェルの `< payload.json` と同じ経路) で初めて誤デコードが起きる。


def _run_hook_file_stdin(hook: str, payload_path: str):
    """実ファイルリダイレクト (`py hook.py < payload.json`) を再現する。

    _run_hook() の subprocess.run(input=str, encoding='utf-8') は親プロセスが
    バイト列を書き込むパイプであり、子プロセス側の sys.stdin デコードには
    影響しない――はずが実測では cp932 誤デコードを再現しなかった
    (Python 3.13 の pipe stdin は別経路でエンコーディングが決まる模様)。
    stdin= に実ファイルハンドルを渡すことで初めて guard_ja_style.py 事故と
    同じ経路 (OS のコンソールコードページ cp932 でのデコード) を再現できる。
    """
    with open(payload_path, "rb") as fh:
        proc = subprocess.run(
            [LAUNCHER, os.path.join(HOOK_DIR, hook)],
            stdin=fh,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=_PROJECT_ROOT,
            timeout=60,
        )
    return proc.returncode, proc.stderr or ""


def test_all_hooks_reconfigure_stdin():
    print("\n[8] 全 hook が stdin を UTF-8 に reconfigure")
    for hook in ALL_HOOKS:
        with open(os.path.join(HOOK_DIR, hook), encoding="utf-8") as f:
            src = f.read()
        check(
            f"{hook}: sys.stdin.reconfigure(encoding='utf-8')",
            "sys.stdin.reconfigure(encoding='utf-8')" in src,
            "json.load(sys.stdin) が cp932 でペイロードを誤デコードし、"
            "検出ロジックが無言でバイパスされる (2026-08-17 known-bug-patterns Category A参照)",
        )


def test_stdin_decoding_survives_file_redirection():
    print("\n[9] cp932 誤デコードで検出がバイパスされないこと (実ファイルリダイレクト経由)")
    import tempfile

    # 日本語コメントの直後 (空白なし) に ASCII 禁止パターンが続くケース。
    # cp932 はマルチバイト文字の続きとして ASCII 1バイトを飲み込むことがあり、
    # 実測では「後os.environ」の「o」が直前の文字化けに吸収されて
    # 「s.environ[...]」に壊れ、正規表現が一致しなくなった (再現済み)。
    snippet = 'os' + '.environ["PYTHON' + 'IOENCODING"] = "utf-8"'
    dangerous_content = "日本語の設定コメント直後" + snippet
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "lib/foo.py", "new_string": dangerous_content},
    }
    with tempfile.TemporaryDirectory() as tmp:
        payload_path = os.path.join(tmp, "payload.json")
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        code, err = _run_hook_file_stdin("guard_edit_encoding.py", payload_path)
        check(
            "日本語直後の os.environ[...] がファイルリダイレクト経由でもブロックされる (exit 2)",
            code == 2,
            f"exit={code} stderr={err[:120]!r}",
        )

    # setx 検知側 (guard_bash_encoding.py) も同じ経路で確認。
    command = "echo 日本語のコメント; " + "set" + "x " + "PYTHONIO" + "ENCODING utf-8"
    payload = {"tool_name": "PowerShell", "tool_input": {"command": command}}
    with tempfile.TemporaryDirectory() as tmp:
        payload_path = os.path.join(tmp, "payload.json")
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        code, err = _run_hook_file_stdin("guard_bash_encoding.py", payload_path)
        check(
            "日本語直後の setx がファイルリダイレクト経由でもブロックされる (exit 2)",
            code == 2,
            f"exit={code} stderr={err[:120]!r}",
        )


def test_block_reason_is_readable_japanese():
    print("\n[7] ブロック理由の日本語が化けずに読める")
    code, err = _run_hook("guard_bash_encoding.py", _shell_payload("PowerShell", "setx PYTHONIOENCODING utf-8"))
    check("exit 2 でブロック", code == 2, f"exit={code}")
    check(
        "stderr にレジストリ (LEVEL 4) の説明が読める形で入っている",
        "レジストリ" in err and "LEVEL 4" in err,
        f"stderr={err[:120]!r}",
    )
    check("置換文字 (U+FFFD) が混ざっていない", "�" not in err, f"stderr={err[:120]!r}")

    code, err = _run_hook("guard_edit_encoding.py", {
        "tool_name": "Write",
        "tool_input": {"file_path": "scripts\\tmp_probe.bat", "content": "REM 日本語コメント\n"},
    })
    check("Edit/Write hook: .bat の非ASCII をブロック", code == 2, f"exit={code}")
    check(
        "Edit/Write hook: 理由が読める",
        "cp932" in err and "�" not in err,
        f"stderr={err[:120]!r}",
    )


if __name__ == "__main__":
    test_shell_tools_declared_consistently()
    test_encoding_guard_blocks_both_shells()
    test_encoding_guard_allows_scoped_alternatives()
    test_commit_guard_sees_powershell()
    test_commit_guard_uses_fast_mode()
    test_all_hooks_reconfigure_stderr()
    test_all_hooks_reconfigure_stdin()
    test_stdin_decoding_survives_file_redirection()
    test_block_reason_is_readable_japanese()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
