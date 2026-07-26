# -*- coding: utf-8 -*-
"""bot層の例外がログに残ることの回帰テスト

このテストが存在する理由:
  /curate は 2026-02-28 のリファクタ以降 NameError で壊れていたが、
  `except Exception as e: await followup.send(f"Error: {e}")` が例外を
  Discord に流すだけでログに一行も残さなかったため、5ヶ月間発覚しなかった。
  2026-07-26 に bot 層28箇所を lib.bot_errors.log_command_error() へ置換した。
  ここではその契約 (必ず stdout にトレースを出す) と、同じ形が
  再び混入していないことの両方を検証する。

Discord へは接続せず、偽 Interaction でコマンドを直接叩く。
実行: py tests/test_error_logging.py
"""
import os
import sys
import ast
import asyncio
import contextlib
import io

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lib.bot_errors import log_command_error, MAX_USER_MESSAGE

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


def _capture(fn):
    """fn() を実行し (戻り値, stdout) を返す。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        value = fn()
    return value, buf.getvalue()


# ---------------------------------------------------------
# 1. ヘルパー単体
# ---------------------------------------------------------

def test_helper_logs_traceback():
    print("\n[1] log_command_error がトレースを残す")

    def run():
        try:
            raise RuntimeError("何かが壊れました")
        except RuntimeError as e:
            return log_command_error("/dummy", e)

    msg, out = _capture(run)

    check("stdout に Traceback が出る", "Traceback" in out, out[:200])
    check("stdout に発生箇所が出る", "/dummy" in out, out[:200])
    check("stdout に例外本文が出る", "何かが壊れました" in out, out[:200])
    check("戻り値に型名が入る", msg.startswith("Error (RuntimeError):"), msg)
    check("戻り値に例外本文が入る", "何かが壊れました" in msg, msg)


def test_helper_prefix_and_truncation():
    print("\n[2] 前置き温存と長文の打ち切り")

    def run_prefix():
        try:
            raise ValueError("boom")
        except ValueError as e:
            return log_command_error("/xtrend", e, "❌ XTrend Error")

    msg, _ = _capture(run_prefix)
    check("呼び出し側の前置きが残る", msg.startswith("❌ XTrend Error (ValueError):"), msg)

    def run_long():
        try:
            raise RuntimeError("x" * 5000)
        except RuntimeError as e:
            return log_command_error("/dummy", e)

    long_msg, long_out = _capture(run_long)
    check("Discord 向けは打ち切られる",
          len(long_msg) < MAX_USER_MESSAGE + 100, f"len={len(long_msg)}")
    check("打ち切りの案内が付く", "logs/launcher.log" in long_msg, long_msg[-80:])
    check("ログ側は全文が残る", long_out.count("x") >= 5000, f"count={long_out.count('x')}")


def test_helper_handles_empty_message():
    print("\n[3] str(exc) が空でも情報が残る")

    class _Silent(Exception):
        pass

    def run():
        try:
            raise _Silent()
        except _Silent as e:
            return log_command_error("/dummy", e)

    msg, _ = _capture(run)
    # 型名を入れていない従来実装だと "Error: " だけになり何も分からなかった
    check("型名だけでも判別できる", "_Silent" in msg, msg)


# ---------------------------------------------------------
# 2. 実コマンド経路 (偽 Interaction)
# ---------------------------------------------------------

def test_command_path_logs_and_replies():
    print("\n[4] /setup の except 経路を実走")

    import commands.bot_commands_curator as bcc
    from tests.test_curate_hooks import FakeInteraction

    orig = bcc.read_main_context
    bcc.read_main_context = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("ネットワーク断")
    )
    try:
        interaction = FakeInteraction()
        _, out = _capture(
            lambda: asyncio.run(bcc.cmd_setup(interaction))
        )
    finally:
        bcc.read_main_context = orig

    replies = [c for k, c in interaction.sent if k == "followup" and c]
    check("Discord に応答が返る", bool(replies), str(interaction.sent))
    check("応答に例外の型名が入る",
          any("RuntimeError" in r for r in replies), str(replies))
    check("ログに Traceback が残る", "Traceback" in out, out[:200])
    check("ログに発生箇所が残る", "/setup" in out, out[:200])


# ---------------------------------------------------------
# 3. 静的スキャン (Guardian と同じ判定を独立に持つ)
# ---------------------------------------------------------

def test_no_silent_handlers_remain():
    print("\n[5] bot層に握りつぶしが残っていない")

    from verify_project import check_swallowed_exceptions

    targets = [os.path.join(_PROJECT_ROOT, "bot_discord.py")]
    commands_dir = os.path.join(_PROJECT_ROOT, "commands")
    targets += [os.path.join(commands_dir, f)
                for f in sorted(os.listdir(commands_dir)) if f.endswith(".py")]

    findings = []
    for path in targets:
        for lineno, summary in check_swallowed_exceptions(path):
            findings.append(f"{os.path.relpath(path, _PROJECT_ROOT)}:{lineno} {summary}")

    check(f"握りつぶし0件 ({len(targets)}ファイル走査)",
          not findings, " / ".join(findings[:3]))


def test_agent_failure_prefix_still_matches():
    """/agent の失敗判定がヘルパーの出力形式に追従しているか。

    _FAILURE_PREFIXES は前方一致で「この結果はセッション文脈に残さない」を
    決めている。log_command_error が型名を挟む形にしたとき、ここが "Error:" の
    ままだと失敗結果を成功扱いして次回の計画プロンプトに混ぜてしまう。
    形式を変えたら必ずここで落ちるように結び付けておく。
    """
    print("\n[7] /agent の失敗判定がヘルパー出力と噛み合う")

    from commands.bot_commands_agent import _FAILURE_PREFIXES

    def run():
        try:
            raise RuntimeError("boom")
        except RuntimeError as e:
            return log_command_error("/agent test", e)

    msg, _ = _capture(run)
    check("失敗と判定される", msg.startswith(_FAILURE_PREFIXES), f"{msg[:40]} vs {_FAILURE_PREFIXES}")
    check("正常結果は失敗と誤判定しない",
          not "Errorハンドリングを調査しました".startswith(_FAILURE_PREFIXES),
          str(_FAILURE_PREFIXES))


def test_detector_catches_the_bad_shape():
    """検出器そのものが機能しているか（0件を返すだけの飾りになっていないか）"""
    print("\n[6] 検出ルールが実際に違反を捕まえる")

    import tempfile
    from verify_project import check_swallowed_exceptions

    bad = (
        "async def f(interaction):\n"
        "    try:\n"
        "        g()\n"
        "    except Exception as e:\n"
        "        await interaction.followup.send(f'Error: {e}')\n"
    )
    good = (
        "async def f(interaction):\n"
        "    try:\n"
        "        g()\n"
        "    except Exception as e:\n"
        "        print(e)\n"
        "        await interaction.followup.send(f'Error: {e}')\n"
    )
    ignored = (
        "def f():\n"
        "    try:\n"
        "        import foo\n"
        "    except ImportError:\n"
        "        pass\n"
    )

    tmpdir = tempfile.mkdtemp(prefix="swallow_")
    results = {}
    for name, src in (("bad", bad), ("good", good), ("ignored", ignored)):
        p = os.path.join(tmpdir, f"{name}.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        results[name] = check_swallowed_exceptions(p)

    check("握りつぶしを検出する", len(results["bad"]) == 1, str(results["bad"]))
    check("ログ有りは見逃す", results["good"] == [], str(results["good"]))
    check("例外変数を使わない節は対象外", results["ignored"] == [], str(results["ignored"]))


if __name__ == "__main__":
    print("=" * 55)
    print(" bot層 例外ログ 回帰テスト")
    print("=" * 55)

    test_helper_logs_traceback()
    test_helper_prefix_and_truncation()
    test_helper_handles_empty_message()
    test_command_path_logs_and_replies()
    test_no_silent_handlers_remain()
    test_detector_catches_the_bad_shape()
    test_agent_failure_prefix_still_matches()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
