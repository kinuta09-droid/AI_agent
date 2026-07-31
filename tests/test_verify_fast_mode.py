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
      [6] Briefing は --fast では実行されない (かつてフル174秒の主因だった部分。
      2026-07-31 のバッチ化でフルは約8秒になったが、コミット毎に1.3秒→8秒は
      6倍の後退なのでフックは --fast のまま)
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
            "重いセクションが実行されている (フックがフル実行コースに戻っている)",
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


def test_machine_locked_syspath_is_blocked():
    """`sys.path.insert(0, r'C:\\AI_agent')` を --fast が赤で止めることの回帰テスト。

    2026-07-30 の CI 初起動で、6つのテストがこの「下駄」のせいで
    `No module named 'tools'` / `'lib'` で落ちた。開発機ではリポジトリが
    実際に C:\\AI_agent にあるので緑になり、実機スイープは 69/0 を報告し続けていた。
    つまり **開発機の緑が CI の赤を隠していた** (Session 2 で CI 側の PYTHONPATH の
    下駄を外したのと同じ欠陥が、テストファイルの中に残っていた)。

    両方向を縛る:
      - 実行される sys.path.insert に絶対パスがあれば赤
      - その欠陥を説明する docstring/コメント/文字列リテラルは緑 (誤検知しない)
        ← 実際 tests/test_moc_auto_link.py 等が教訓としてこの文字列を含む
    """
    print("\n[3] sys.path へのルート直書きを --fast が止める")

    violating = os.path.join(_PROJECT_ROOT, "tests", "_tmp_machine_locked_probe.py")
    with open(violating, "w", encoding="utf-8") as f:
        f.write("# -*- coding: utf-8 -*-\n"
                "import sys\n"
                "sys.path.insert(0, r'C:\\AI_agent')\n")
    try:
        code, out, _ = _run_verify("--fast")
        check("ルート直書きを置くと --fast が赤 (exit 1)", code == 1,
              f"exit={code} — 検査が判定に反映されていない")
        check("エラー内容に該当ファイル名が出る", "_tmp_machine_locked_probe" in out,
              f"出力に無い: {out[-400:]!r}")
    finally:
        os.remove(violating)

    # 誤検知の方向: 同じ文字列を docstring / コメント / 文字列リテラルに持つが、
    # 実行される insert は __file__ 相対のファイル。これは緑でなければならない。
    documenting = os.path.join(_PROJECT_ROOT, "tests", "_tmp_documented_probe.py")
    with open(documenting, "w", encoding="utf-8") as f:
        f.write("# -*- coding: utf-8 -*-\n"
                "r'''以前は sys.path.insert(0, r\"C:\\AI_agent\") と直書きしていた。'''\n"
                "import sys, os\n"
                "# sys.path.insert(0, r'C:\\AI_agent')  <- 説明。実行されない\n"
                "MSG = r\"sys.path.insert(0, r'C:\\AI_agent')\"\n"
                "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n")
    try:
        code, out, _ = _run_verify("--fast")
        check("教訓を書いたコメント/文字列は誤検知しない (緑)", code == 0,
              f"exit={code} — ast ではなく文字列走査になっている疑い\n{out[-600:]}")
    finally:
        os.remove(documenting)

    code, _, _ = _run_verify("--fast")
    check("プローブ削除後は再び緑", code == 0, f"exit={code} — 一時ファイルが残っている")


def test_machine_locked_syspath_covers_indirection():
    """検査が「リテラル直書き」以外の形も見ていることの回帰テスト。

    初版は `sys.path.insert(0, <文字列定数>)` しか見ておらず、実測で7形のうち
    6形が素通りした (2026-07-30 の /grill 指摘)。Session 2 の C1 と同じ罠なので、
    素通りしていた形を全部ここに固定して、検査を狭められないようにする。

    [3] は「門に配線されているか」を --fast の終了コードで見る e2e、
    ここは「どの書き方まで見えているか」を関数単体で網羅する — 役割が違う。
    """
    print("\n[4] ルート直書きの各種書き方を網羅しているか")
    from verify_project import check_machine_locked_syspath as chk

    probe = os.path.join(_PROJECT_ROOT, "tests", "_tmp_syspath_form_probe.py")
    forms = {
        "直リテラル": "import sys\nsys.path.insert(0, r'C:\\AI_agent')\n",
        "変数経由": "import sys\nROOT = r'C:\\AI_agent'\nsys.path.insert(0, ROOT)\n",
        "sys.path +=": "import sys\nsys.path += [r'C:\\AI_agent']\n",
        "sys.path 代入": "import sys\nsys.path = [r'C:\\AI_agent'] + sys.path\n",
        "extend": "import sys\nsys.path.extend([r'C:\\AI_agent'])\n",
        "os.path.join 経由": "import sys, os\nsys.path.insert(0, os.path.join(r'C:\\AI_agent', 'x'))\n",
        "from sys import path": "from sys import path\npath.insert(0, r'C:\\AI_agent')\n",
        "import sys as 別名": "import sys as s\ns.path.insert(0, r'C:\\AI_agent')\n",
        "POSIX 絶対パス": "import sys\nsys.path.append('/opt/thing')\n",
    }
    try:
        for label, src in forms.items():
            with open(probe, "w", encoding="utf-8") as f:
                f.write("# -*- coding: utf-8 -*-\n" + src)
            check(f"検出できる: {label}", bool(chk(probe)), "素通りしている")

        # 逆方向: 正しい書き方と、教訓を文字列として持つだけのファイルは緑
        clean = {
            "__file__ 相対": (
                "import sys, os\n"
                "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"),
            "教訓を文字列で持つだけ": (
                "import sys, os\n"
                "MSG = r\"sys.path.insert(0, r'C:\\AI_agent')\"\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"),
            "絶対パス定数を sys.path に使わない": (
                "import sys, os\n"
                "VAULT = r'D:\\マイドライブ\\Obsidian'\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"),
        }
        for label, src in clean.items():
            with open(probe, "w", encoding="utf-8") as f:
                f.write("# -*- coding: utf-8 -*-\n" + src)
            got = chk(probe)
            check(f"誤検知しない: {label}", got == [], f"誤検知: {got}")
    finally:
        if os.path.exists(probe):
            os.remove(probe)


def test_hardcoded_data_path_is_blocked():
    """実行時データパスへのルート直書きを --fast が止めることの回帰テスト。

    [3][4] が塞いだのは **sys.path** の直書きだけで、2026-07-30 時点では
    `learning_db.json` / コストガードのカウンタ / `.env` / `token.json` /
    OHLCVキャッシュ といった **実行時に読み書きするパス** の直書きが9箇所
    残っていた。sys.path 版は ImportError で落ちるので気付けるが、データパス版は
    **落ちずに間違ったファイルを読み書きする** ぶん症状が見えにくい
    (学習DBの統計もコスト上限も静かに嘘になる)。

    プローブは scripts/ に置く: 検査は tests/ を対象外にしている
    (テストは期待値として絶対パスを書く場所なので) ため、tests/ に置くと
    「配線されているのに緑」になり、テストの意味が消える。
    """
    print("\n[5] 実行時データパスへのルート直書きを --fast が止める")

    violating = os.path.join(_PROJECT_ROOT, "scripts", "_tmp_datapath_probe.py")
    with open(violating, "w", encoding="utf-8") as f:
        f.write("# -*- coding: utf-8 -*-\n"
                "import os\n"
                "LEARNING_DB_PATH = os.path.join(r'C:\\AI_agent', 'learning_db.json')\n")
    try:
        code, out, _ = _run_verify("--fast")
        check("データパス直書きを置くと --fast が赤 (exit 1)", code == 1,
              f"exit={code} — 検査が判定に反映されていない")
        check("エラー内容に該当ファイル名が出る", "_tmp_datapath_probe" in out,
              f"出力に無い: {out[-400:]!r}")
    finally:
        os.remove(violating)

    code, _, _ = _run_verify("--fast")
    check("プローブ削除後は再び緑", code == 0, f"exit={code} — 一時ファイルが残っている")


def test_hardcoded_data_path_forms_and_false_positives():
    """どの書き方まで見えているか / 何を誤検知してはいけないかを関数単体で固定する。

    誤検知の方向が特に重要: この検査を「絶対パス文字列を全部赤にする」形に広げると、
    (a) 欠陥を説明する docstring、(b) `scripts/search_dev_logs.py` の
    「移行の痕跡を探す検索語リスト」に入っている `C:\\obsidian`、
    (c) 唯一の宣言場所である `lib/paths.py` 自身 — の3つが巻き添えになる。
    だから「パスを組み立てている文脈」に限定してある。その線引きをここで縛る。
    """
    print("\n[6] データパス検査の網羅と誤検知")
    from verify_project import check_hardcoded_abs_paths as chk

    probe = os.path.join(_PROJECT_ROOT, "scripts", "_tmp_datapath_form_probe.py")
    rel = os.path.join("scripts", "_tmp_datapath_form_probe.py")

    detected = {
        "os.path.join の第1引数": "import os\nP = os.path.join(r'C:\\AI_agent', 'learning_db.json')\n",
        "load_dotenv の引数": "from dotenv import load_dotenv\nload_dotenv(r'C:\\AI_agent\\.env')\n",
        "open の引数": "f = open(r'C:\\AI_agent\\learning_db.json')\n",
        "_PATH への代入": "TOKEN_PATH = r'C:\\AI_agent\\token.json'\n",
        "_DIR への代入": "OHLCV_CACHE_DIR = r'C:\\AI_agent\\logs\\ohlcv_cache'\n",
        "_ROOT への代入": "VAULT_ROOT = r'D:\\マイドライブ\\Obsidian'\n",
        "キーワード引数": "import os\nos.makedirs(name=r'C:\\AI_agent\\logs')\n",
        "POSIX 絶対パス": "import os\nP = os.path.join('/opt/arms', 'db.json')\n",
    }
    clean = {
        "__file__ 相対": (
            "import os\n"
            "ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
            "LEARNING_DB_PATH = os.path.join(ROOT, 'learning_db.json')\n"),
        "教訓を docstring に書いただけ": (
            "r'''以前は os.path.join(r\"C:\\AI_agent\", \"learning_db.json\") だった。'''\n"
            "import os\n"),
        "コメントに書いただけ": "import os\n# LEARNING_DB_PATH = r'C:\\AI_agent\\learning_db.json'\n",
        "検索語リスト (search_dev_logs 型)": (
            "DEFAULT_KEYWORDS = ['マイドライブ', 'C:\\\\obsidian', 'C:/obsidian', '移行']\n"),
        "相対パスの組み立て": "import os\nP = os.path.join('logs', 'x.json')\n",
    }
    try:
        for label, src in detected.items():
            with open(probe, "w", encoding="utf-8") as f:
                f.write("# -*- coding: utf-8 -*-\n" + src)
            check(f"検出できる: {label}", bool(chk(probe, rel)), "素通りしている")

        for label, src in clean.items():
            with open(probe, "w", encoding="utf-8") as f:
                f.write("# -*- coding: utf-8 -*-\n" + src)
            got = chk(probe, rel)
            check(f"誤検知しない: {label}", got == [], f"誤検知: {got}")

        # 唯一の宣言場所 lib/paths.py は allowlist されている (ここが赤いと
        # 「宣言する場所が無い」状態になり、検査ごと外される方向に力が働く)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("# -*- coding: utf-8 -*-\nVAULT_ROOT = r'D:\\マイドライブ\\Obsidian'\n")
        check("lib/paths.py は許可されている", chk(probe, "lib/paths.py") == [],
              "唯一の宣言場所まで赤くしている")
        check("tests/ は対象外", chk(probe, "tests/test_x.py") == [],
              "テストの期待値まで赤くしている")
    finally:
        if os.path.exists(probe):
            os.remove(probe)


def test_paths_module_is_the_single_source():
    """lib/paths.py が実際に「リポジトリルートを __file__ から算出」していること。

    検査 (verify) と実体 (lib/paths.py) は別物なので、検査だけ通って実体が
    直書きに戻っている、という食い違いがありうる。ここで実体側を縛る。
    """
    print("\n[7] lib/paths.py の解決先")
    from lib import paths

    check("PROJECT_ROOT がリポジトリルート",
          os.path.normcase(paths.PROJECT_ROOT) == os.path.normcase(_PROJECT_ROOT),
          f"{paths.PROJECT_ROOT} != {_PROJECT_ROOT}")
    check("ENV_PATH は PROJECT_ROOT/.env",
          paths.ENV_PATH == os.path.join(_PROJECT_ROOT, ".env"), paths.ENV_PATH)
    check("LOGS_DIR は PROJECT_ROOT/logs",
          paths.LOGS_DIR == os.path.join(_PROJECT_ROOT, "logs"), paths.LOGS_DIR)

    # lib_shared のデータパスが paths 経由になっていること (直書きへの復帰検出)。
    # lib_shared は API キーが要るので import せず、ast で式の形だけ見る。
    import ast
    src = open(os.path.join(_PROJECT_ROOT, "lib", "lib_shared.py"), encoding="utf-8").read()
    wanted = {"LEARNING_DB_PATH", "LEARNING_DB_ARCHIVE_PATH"}
    found = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in wanted:
                    found[t.id] = ast.unparse(node.value)
    for name in sorted(wanted):
        expr = found.get(name, "")
        check(f"{name} が PROJECT_ROOT 起点", "PROJECT_ROOT" in expr,
              f"式が {expr!r} — 直書きに戻っている疑い")


if __name__ == "__main__":
    test_fast_skips_only_the_expensive_sections()
    test_fast_actually_fails_on_broken_source()
    test_machine_locked_syspath_is_blocked()
    test_machine_locked_syspath_covers_indirection()
    test_hardcoded_data_path_is_blocked()
    test_hardcoded_data_path_forms_and_false_positives()
    test_paths_module_is_the_single_source()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
