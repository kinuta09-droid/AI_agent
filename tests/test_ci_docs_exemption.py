# -*- coding: utf-8 -*-
"""「docs だけの変更なら CI を skip してよい」という前提を縛る回帰テスト

このテストが存在する理由 (2026-07-31):
  `.github/workflows/tests.yml` に paths-ignore を入れて、markdown だけの変更で
  CI を走らせないようにした。実測では docs 3行の PR が 358 job秒
  (private + Windows なので12課金分) を消費し、しかも PR run と merge push run で
  2回走っていた。

  ただしこの skip は **前提の上に乗っている**:「追跡下の .md を読む Python が
  1本も無いので、docs 変更は判定結果を変えられない」。導入時点では実際に
  0件だったが、**将来この前提が崩れても skip は黙って続く**。そのとき
  「テストが読む .md」を編集した PR は、そのテストを一度も走らせずにマージされる。
  ログには一行も残らない — CLAUDE.md の所見2「無言の死」と同じ型になる。

  なので前提そのものを検査に変える:

    [1] 門が配線されているか — paths-ignore が下の宣言リストと一致し、
        push と pull_request の両方に付いていること。**宣言と一致**を見るのは、
        後から 'plans/**' のようなディレクトリ単位の除外が足されたときに
        赤くするため (そのディレクトリに .py が入った瞬間、コードの変更が
        skip されるようになる)。
    [2] 前提が生きているか — tests/ scripts/ verify_project.py の中に、
        リポジトリ追跡下の .md を名指ししている文字列リテラルが無いこと。
    [3] 検出器そのものの両方向 — 本物の参照を赤にし、コメント/docstring/
        追跡外の .md を緑にすること。

  [3] を書く理由は CLAUDE.md の check_machine_locked_syspath と同じ:
  **この欠陥を説明する文章自体が違反の形をしている**ので、grep 的な検査だと
  「教訓を書いた行」を違反として報告する。だから ast で書き、その ast 検査が
  本当に効いているかを逆方向でも確かめる。

実行: py tests/test_ci_docs_exemption.py
"""
import ast
import glob
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

WORKFLOW = os.path.join(_PROJECT_ROOT, ".github", "workflows", "tests.yml")

# --- 宣言 -------------------------------------------------------------------
# paths-ignore に書いてよいパターン。ここを増やすときは [2] の前提が
# その範囲でも成り立つかを確認してから増やすこと。
# ディレクトリ単位 ('docs/**' 等) は原則入れない — そのディレクトリに .py が
# 置かれた瞬間、コードの変更が skip されるようになる。
EXPECTED_PATHS_IGNORE = ["**.md"]

# path filter を付ける対象。workflow_dispatch には filter が効かない
# (= 手動なら常に全部走らせられる) ので、ここには入れない。
GATED_EVENTS = ("push", "pull_request")

_PASS = []
_FAIL = []


def check(name, cond, detail=""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------------------------
# ワークフローの読み取り (PyYAML を使わない)
# ---------------------------------------------------------------------------
def parse_on_block(text):
    """tests.yml の `on:` ブロックを {イベント名: [paths-ignore のパターン]} にする。

    PyYAML を使わない理由: requirements.txt の推移的依存に入っておらず、CI では
    import できない (確認済み)。テスト専用の依存を本番の requirements.txt に
    足すのは順序が逆なので、必要な範囲だけ自前で読む。
    **手書きの読み取りは黙って壊れる**ので、[3] で両方向のテストを付けてある。

    paths-ignore を持たないイベントは空リストで現れる (「イベントは在るが
    filter が無い」と「イベント自体が無い」を [1] で区別するため)。
    """
    events = {}
    in_on = False
    current = None
    collecting = False
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_on = line.startswith("on:")
            current, collecting = None, False
            continue
        if not in_on:
            continue
        if indent == 2:
            current = line.strip().rstrip(":").strip()
            events.setdefault(current, [])
            collecting = False
        elif indent == 4:
            collecting = line.strip().startswith("paths-ignore:")
        elif indent >= 6 and collecting and line.strip().startswith("- ") and current:
            events[current].append(line.strip()[2:].strip().strip("'\""))
    return events


# ---------------------------------------------------------------------------
# 追跡下の .md を名指ししている文字列リテラルの検出
# ---------------------------------------------------------------------------
def _docstring_constants(tree):
    """docstring として使われている Constant ノードの id 集合。

    docstring は「読んでいる」ではなく「説明している」なので除外する。
    コメントはそもそも ast に現れないため何もしなくてよい。
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def markdown_refs(source, tracked_relpaths):
    """source が名指ししている「追跡下の .md」を [(行, リテラル), ...] で返す。

    判定はリテラル1つ単位。`"CLAUDE" + ".md"` のように実行時に組み立てたものは
    検出しない — これは取りこぼしではなく、**パスとしてそこに書かれていない**
    ことの反映 (このファイル自身が [3] でその書き方を使っている)。
    f-string の断片は個別の Constant として現れるので拾える。
    """
    tracked_relpaths = {p.replace("\\", "/") for p in tracked_relpaths}
    tracked_basenames = {p.rsplit("/", 1)[-1] for p in tracked_relpaths}

    tree = ast.parse(source)
    skip = _docstring_constants(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        literal = node.value
        if not literal.lower().endswith(".md"):
            continue
        norm = literal.replace("\\", "/").lstrip("./")
        if norm in tracked_relpaths or norm.rsplit("/", 1)[-1] in tracked_basenames:
            hits.append((getattr(node, "lineno", 0), literal))
    return hits


def tracked_markdown():
    """git 追跡下の *.md の相対パス一覧。git が無ければ None (呼び出し側で SKIP)。"""
    try:
        proc = subprocess.run(["git", "ls-files", "*.md"], cwd=_PROJECT_ROOT,
                              capture_output=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip().replace("\\", "/") for ln in proc.stdout.splitlines() if ln.strip()]


def _scan_targets():
    """判定結果に効きうる Python。tests/ と、そこから呼ばれる scripts/・Guardian。"""
    paths = sorted(glob.glob(os.path.join(_PROJECT_ROOT, "tests", "*.py")))
    paths += sorted(glob.glob(os.path.join(_PROJECT_ROOT, "scripts", "*.py")))
    paths.append(os.path.join(_PROJECT_ROOT, "verify_project.py"))
    return [p for p in paths if os.path.isfile(p)]


# ---------------------------------------------------------------------------
# [1] 門が配線されているか
# ---------------------------------------------------------------------------
def test_workflow_declares_the_exemption():
    print("\n[1] tests.yml の paths-ignore が宣言と一致しているか")
    with open(WORKFLOW, encoding="utf-8") as f:
        events = parse_on_block(f.read())

    for event in GATED_EVENTS:
        check(f"{event} に paths-ignore がある", events.get(event),
              f"on.{event} = {events.get(event)!r}")
        check(f"{event} の除外が宣言どおり",
              events.get(event) == EXPECTED_PATHS_IGNORE,
              f"期待 {EXPECTED_PATHS_IGNORE} / 実際 {events.get(event)!r} — "
              "増やすなら EXPECTED_PATHS_IGNORE と [2] の前提を先に確認すること")

    check("workflow_dispatch が残っている (手動なら全部走らせられる)",
          "workflow_dispatch" in events, f"{sorted(events)}")
    check("workflow_dispatch に path filter を付けていない",
          not events.get("workflow_dispatch"),
          f"{events.get('workflow_dispatch')!r} — filter は効かないので誤解のもと")

    # ディレクトリ単位の除外を弾く。'plans/**' のような書き方は、そこに .py が
    # 置かれた瞬間にコードの変更を skip させる。
    bad = [p for p in EXPECTED_PATHS_IGNORE if not p.endswith(".md")]
    check("宣言リストが .md 以外を含んでいない", not bad, f"{bad}")


# ---------------------------------------------------------------------------
# [2] 前提が生きているか
# ---------------------------------------------------------------------------
def test_no_python_reads_a_tracked_markdown():
    print("\n[2] 追跡下の .md を読む Python が無いか (前提の実体)")
    tracked = tracked_markdown()
    if tracked is None:
        print("  ⏭️ git が使えないので SKIP")
        return
    check("追跡下の .md を列挙できた", len(tracked) > 0, f"{len(tracked)} 件")

    violations = []
    for path in _scan_targets():
        with open(path, encoding="utf-8") as f:
            source = f.read()
        try:
            hits = markdown_refs(source, tracked)
        except SyntaxError as e:
            violations.append(f"{os.path.relpath(path, _PROJECT_ROOT)}: 構文エラー {e}")
            continue
        for lineno, literal in hits:
            violations.append(
                f"{os.path.relpath(path, _PROJECT_ROOT)}:{lineno} -> {literal!r}")

    check(f"違反0件 ({len(_scan_targets())} ファイルを走査)", not violations,
          "docs 変更が判定結果を変えうる = paths-ignore の前提が崩れている。"
          "tests.yml の除外を外すか、この参照をやめること:\n    "
          + "\n    ".join(violations))


# ---------------------------------------------------------------------------
# [3] 検出器そのものの両方向
# ---------------------------------------------------------------------------
# リテラルで 'CLAUDE.md' と書くと **このファイル自身が [2] の違反**になるので、
# 実行時に組み立てる。検出器がリテラル単位で見ていることの実証も兼ねている。
_SAMPLE = "CLAUDE" + ".md"
_TRACKED = [_SAMPLE, "plans/test-sweep-speedup" + ".md"]


def test_detector_direction():
    print("\n[3] 検出器が本物だけを赤にするか (両方向)")

    should_hit = {
        "open() の引数": 'open("%s", encoding="utf-8")' % _SAMPLE,
        "os.path.join の末尾": 'p = os.path.join(ROOT, "plans", "test-sweep-speedup.md")',
        "f-string の断片": 'p = f"{root}/%s"' % _SAMPLE,
        "Windows 区切り": 'p = "plans\\\\test-sweep-speedup.md"',
        "相対の ./ 付き": 'p = "./%s"' % _SAMPLE,
    }
    for name, src in should_hit.items():
        check(f"検出する: {name}", markdown_refs(src, _TRACKED), src)

    should_pass = {
        "コメントの中":
            '# %s の教訓に従って書いた\nx = 1' % _SAMPLE,
        "モジュール docstring":
            '"""%s を参照。"""\nx = 1' % _SAMPLE,
        "関数 docstring":
            'def f():\n    """%s に書いてある。"""\n    return 1' % _SAMPLE,
        "追跡外の .md (tmpdir の fixture)":
            'open(os.path.join(tmp, "note.md"), "w")',
        "実行時に組み立てたもの":
            'p = "CLAU" + "DE.md"',
        "拡張子が違う":
            'open("CLAUDE.txt")',
    }
    for name, src in should_pass.items():
        hits = markdown_refs(src, _TRACKED)
        check(f"誤検知しない: {name}", not hits, f"{src!r} -> {hits}")


def test_workflow_parser_direction():
    print("\n[4] ワークフロー読み取りの両方向")

    good = (
        "name: Tests\n"
        "on:\n"
        "  push:\n"
        "    branches: [master, main]\n"
        "    paths-ignore:\n"
        "      - '**.md'\n"
        "  pull_request:\n"
        "    paths-ignore:\n"
        "      - '**.md'\n"
        "  workflow_dispatch:\n"
        "jobs:\n"
        "  guardian:\n"
        "    steps:\n"
        "      - run: echo paths-ignore\n"
    )
    got = parse_on_block(good)
    check("push/pull_request の除外を読める",
          got.get("push") == ["**.md"] and got.get("pull_request") == ["**.md"], f"{got}")
    check("workflow_dispatch を空で拾う", got.get("workflow_dispatch") == [], f"{got}")
    check("jobs: 以降を on: と混ぜない", "guardian" not in got, f"{sorted(got)}")

    missing = (
        "on:\n"
        "  push:\n"
        "    branches: [master, main]\n"
        "  pull_request:\n"
    )
    got = parse_on_block(missing)
    check("除外が無いイベントは空リストになる",
          got == {"push": [], "pull_request": []}, f"{got}")

    commented = (
        "on:\n"
        "  push:\n"
        "    # paths-ignore:\n"
        "    #   - '**.md'\n"
        "    branches: [master]\n"
    )
    check("コメントアウトされた除外を «在る» と読まない",
          parse_on_block(commented) == {"push": []}, f"{parse_on_block(commented)}")

    directory = (
        "on:\n"
        "  push:\n"
        "    paths-ignore:\n"
        "      - '**.md'\n"
        "      - 'plans/**'\n"
    )
    check("複数パターンを取りこぼさない (宣言との差分で赤にできる)",
          parse_on_block(directory).get("push") == ["**.md", "plans/**"],
          f"{parse_on_block(directory)}")


if __name__ == "__main__":
    test_workflow_declares_the_exemption()
    test_no_python_reads_a_tracked_markdown()
    test_detector_direction()
    test_workflow_parser_direction()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  - {f}")
    sys.exit(1 if _FAIL else 0)
