# -*- coding: utf-8 -*-
"""
Antigravity Guardian (Stability Verifier)
プロジェクトの整合性を自動検証し、リファクタリング後の「破壊」を未然に防ぐ。
- 全ファイルの構文チェック (Compile Check)
- エンコーディングチェック (UTF-8)
- パッケージインポート整合性チェック (Dependency Check)

使い方:
    py verify_project.py            # フル検査 (リリース前・CI)
    py verify_project.py --fast     # 静的検査のみ (git commit フック用)

--fast は「サブプロセスを1つも起動しない検査」だけを走らせる。
実測 (2026-07-31 実機 Windows): フル 約10秒 / --fast 約1秒。
フルが 164.69秒 かかっていた頃の主因は [3][4][6] が
「1モジュール = 1サブプロセス」で ccxt/pandas/google.genai を含む重量級スタックを
27回ゼロからロードし直していたことで、これは1プロセスへのバッチ化で解消した
(run_import_batch のコメント参照)。
それでもフックは --fast のまま維持する: コミット毎に1秒→10秒は10倍の後退で、
「気軽に細かくコミットする」という行動を潰すため。
--fast が緑でもフルが緑とは限らないので、**--fast はフルの代用ではない**。
import 整合性は CI (.github/workflows/tests.yml の guardian ジョブ) と
リリース前の手動フル実行が担保する。
"""

import os
import re
import sys
import json
import subprocess
import importlib.util
import warnings
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# プロジェクトルートの設定
ROOT_DIR = Path(__file__).parent.absolute()
PYTHON_EXE = sys.executable

# Windows のドライブレター始まり (C:\... / D:/...) と POSIX の絶対パス。
# check_machine_locked_syspath で使う。
_ABS_PATH_RE = re.compile(r'^(?:[A-Za-z]:[\\/]|/)')

def check_encoding(file_path):
    """ファイルがUTF-8で保存されているか確認する"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            f.read()
        return True
    except UnicodeDecodeError:
        return False

def check_syntax(file_path):
    """Pythonの構文チェックを行う。

    旧実装は `subprocess.run([py, "-m", "py_compile", file])` で、143ファイル =
    143プロセスを起動していた。Windows はプロセス生成が高価で、これだけで
    フル実行時間の相当部分を占めていた (2026-07-30 計測)。
    compile() はバイト列を渡せば PEP 263 の coding 宣言も BOM も py_compile と
    同じ規則で解釈するので、判定内容を変えずにインプロセス化できる
    (.pyc を書かなくなる点だけが差分で、検査には影響しない)。
    """
    try:
        with open(file_path, 'rb') as f:
            src = f.read()
        # compile() は SyntaxWarning を出すことがあるが、構文の可否は例外で判定する。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(src, str(file_path), "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"{type(e).__name__}: {e}"
    except (OSError, ValueError, UnicodeDecodeError) as e:
        # ValueError はソース中の NUL バイト等
        return False, f"{type(e).__name__}: {e}"
    except (RecursionError, MemoryError) as e:
        # サブプロセス版では病的に深いネストの1ファイルが子プロセスだけを殺したが、
        # インプロセス化した以上ここで捕まえないと Guardian 自体が落ちる。
        return False, f"{type(e).__name__}: コンパイルできないほど複雑なファイル ({e})"

def check_undefined_names(file_path):
    """定義もインポートもされていない名前への参照を検出する。

    構文チェックでは捕まらない NameError 予備軍を静的に洗い出す。
    2026-02-28 の commands/ 分割リファクタで bot_discord.py の
    _dispatch_curate が移動済みシンボル (CurateView 等) を参照したまま残り、
    /curate が5ヶ月間壊れていたのを検出できなかった反省から追加 (2026-07-25)。

    返り値: [(スコープ名, 未定義名), ...]
    """
    import builtins
    import symtable
    import warnings

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            src = f.read()
        # symtable はコンパイルを伴うため SyntaxWarning が出ることがある。
        # 構文の問題は check_syntax の担当なのでここでは黙らせる。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            top = symtable.symtable(src, str(file_path), "exec")
    except Exception:
        return []  # 構文エラーは check_syntax の担当

    module_names = {s.get_name() for s in top.get_symbols()}
    findings = []

    def walk(table, scope):
        for sym in table.get_symbols():
            name = sym.get_name()
            # ローカル/引数/クロージャ変数は対象外。モジュール定義済み・組込みも除外
            if not sym.is_referenced():
                continue
            if sym.is_local() or sym.is_parameter() or sym.is_free():
                continue
            if name in module_names or hasattr(builtins, name):
                continue
            if (scope, name) not in findings:
                findings.append((scope, name))
        for child in table.get_children():
            walk(child, f"{scope}.{child.get_name()}")

    walk(top, "<module>")
    return findings


def check_swallowed_exceptions(file_path):
    """例外を握りつぶしてユーザーに見せるだけの except 節を検出する。

    「捕まえた例外を f-string に埋め込んでいる (= ユーザーに見せている) のに、
    節内に print / logger / traceback / raise が一つも無い」ものを違反とする。
    この形はログに一行も残さないため、壊れていても誰も気付けない。
    実際 /curate は _dispatch_curate の except でこの形をしており、
    NameError のまま5ヶ月間発覚しなかった (2026-07-26 に全28箇所を lib.bot_errors
    経由へ置換)。discord.py は未捕捉例外を自前ロガーで出すので、
    この形の except は「無いほうがマシ」な状態になっている。

    例外変数を使っていない `except ImportError: pass` 等は対象外
    (意図的な握りつぶしであり、見せる情報も無い)。

    返り値: [(行番号, 節の先頭文の要約), ...]
    """
    import ast

    LOGGING_CALLS = {
        "print", "error", "warning", "exception", "critical",
        "format_exc", "print_exc", "log_command_error",
    }

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), str(file_path))
    except Exception:
        return []  # 構文エラーは check_syntax の担当

    def logs_something(handler):
        for node in ast.walk(handler):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in LOGGING_CALLS:
                    return True
            if isinstance(node, ast.Raise):
                return True
        return False

    def shows_exception(handler):
        """捕捉した例外変数を f-string に埋め込んでいるか"""
        if not handler.name:
            return False
        for node in ast.walk(handler):
            if isinstance(node, ast.FormattedValue):
                for inner in ast.walk(node.value):
                    if isinstance(inner, ast.Name) and inner.id == handler.name:
                        return True
        return False

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if shows_exception(node) and not logs_something(node):
            summary = ast.unparse(node.body[0]) if node.body else ""
            findings.append((node.lineno, summary[:80]))
    return findings


def check_machine_locked_syspath(file_path):
    """sys.path に絶対パスのリテラルを差し込んでいる箇所を検出する。

    `sys.path.insert(0, r'C:\\AI_agent')` はこのマシンでしか成立しない「下駄」で、
    リポジトリを別の場所に置いた環境 (CI の D:\\a\\AI_agent\\AI_agent) では
    ImportError になる。開発機だけが緑になるので、実機のテストスイープが
    「通っている」と言い続ける一方で CI だけが落ちる、という食い違いを生む。
    実際 2026-07-30 の CI 初起動で6ファイルがこれで落ちた
    (`No module named 'tools'` / `'lib'`)。正しい形は
    `sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`。

    文字列走査ではなく ast を使う理由: この欠陥を説明する docstring/コメント自体が
    `sys.path.insert(0, r'C:\\AI_agent')` という文字列を含むため、grep 的な検査だと
    「教訓を書いた行」を違反として報告してしまう。

    **リテラル直書きの形だけを見てはいけない** (2026-07-30 の /grill 指摘)。
    初版は `sys.path.insert(0, <文字列定数>)` だけを見ており、実測すると7形のうち
    6形が素通りしていた: 変数経由 (`ROOT = r'C:\\AI_agent'` -> `insert(0, ROOT)`)、
    `sys.path += [...]`、`sys.path = [...] + sys.path`、`extend([...])`、
    `os.path.join(r'C:\\AI_agent', ...)`、`from sys import path` -> `path.insert(...)`。
    Session 2 の C1 (「穴を塞いだ」と書いたコミットが半分しか塞いでいなかった)
    と同じ罠なので、**sys.path を書き換える箇所を列挙し、その式の部分木ごと**
    絶対パスを探す形にした。名前は「絶対パス文字列を代入された名前」を集めて解決する。

    Returns: [(lineno, 該当リテラル), ...]
    """
    import ast

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), str(file_path))
    except Exception:
        return []  # 構文エラーは check_syntax の担当

    def first_abs_literal(subtree):
        """部分木の中の最初の「絶対パスに見える文字列定数」を返す"""
        for n in ast.walk(subtree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and _ABS_PATH_RE.match(n.value)):
                return n.value
        return None

    sys_aliases = {"sys"}      # import sys / import sys as s
    path_aliases = set()       # from sys import path / as p
    abs_names = {}             # 絶対パス文字列を代入された名前 -> そのリテラル

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "sys":
                    sys_aliases.add(a.asname or "sys")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "sys":
                for a in node.names:
                    if a.name == "path":
                        path_aliases.add(a.asname or "path")
        elif isinstance(node, ast.Assign):
            lit = first_abs_literal(node.value)
            if lit:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        abs_names[t.id] = lit

    def is_sys_path(node):
        """node が sys.path (別名込み) を指しているか"""
        if isinstance(node, ast.Attribute) and node.attr == "path":
            return isinstance(node.value, ast.Name) and node.value.id in sys_aliases
        return isinstance(node, ast.Name) and node.id in path_aliases

    def scan(subtree, lineno, out):
        for n in ast.walk(subtree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and _ABS_PATH_RE.match(n.value)):
                out.append((lineno, n.value))
            elif isinstance(n, ast.Name) and n.id in abs_names:
                out.append((lineno, abs_names[n.id]))

    findings = []
    for node in ast.walk(tree):
        # sys.path.insert / append / extend (別名込み)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("insert", "append", "extend")
                and is_sys_path(node.func.value)):
            for arg in node.args:
                scan(arg, node.lineno, findings)
        # sys.path += [...]
        elif isinstance(node, ast.AugAssign) and is_sys_path(node.target):
            scan(node.value, node.lineno, findings)
        # sys.path = [...] + sys.path
        elif isinstance(node, ast.Assign) and any(is_sys_path(t) for t in node.targets):
            scan(node.value, node.lineno, findings)
    return findings


# 絶対パスの直書きを許す唯一の場所。ここが「リポジトリ外の実体」の宣言箇所。
_ABS_PATH_ALLOWED = {"lib/paths.py"}

# 据え置き中の既知の直書き。**新規追加禁止** — 増やすなら lib/paths.py へ寄せる。
_ABS_PATH_EXEMPT = {
    "tools/tool_lora_factory.py":
        "旧Vault (C:\\obsidian\\Obsidian\\...) を指したまま。現行 Vault は "
        "D ドライブなので移行漏れの可能性が高いが、書き込み先が変わる変更に "
        "なるため要判断。棚卸しの結論が出たら lib/paths.py へ寄せて本行を消す",
}

# パスを組み立てている、と見なす呼び出し
_PATHY_CALLS = {"join", "load_dotenv", "open", "exists", "isdir", "isfile", "makedirs"}
# パス定数だと見なす代入先の名前の語尾
_PATHY_SUFFIXES = ("_PATH", "_DIR", "_ROOT", "_FILE")


def check_hardcoded_abs_paths(file_path, rel_path):
    """実行時データパスに絶対パスを直書きしている箇所を検出する。

    check_machine_locked_syspath() の姉妹編。あちらは **sys.path** だけを見るので、
    2026-07-30 の修正後も `learning_db.json` / コストガードのカウンタ /
    OHLCVキャッシュ / token.json / .env といった **実行時に読み書きするパス** の
    ルート直書きが9箇所そのまま残っていた (検出対象外だったため誰も気付かない)。

    sys.path の直書きは ImportError で派手に落ちるが、データパスの直書きは
    **落ちずに間違ったファイルを読み書きする**。別の場所に置いたチェックアウトが
    本番の learning_db やコストガードのカウンタを更新してしまえば、学習DBの
    統計もコスト上限も静かに嘘になる。sys.path 版より症状が見えにくい。

    誤検知を避けるため「パスを組み立てている文脈」に限定する:
      - os.path.join() / load_dotenv() / open() 等の引数
      - 語尾が _PATH/_DIR/_ROOT/_FILE の名前への代入
    これで `DEFAULT_KEYWORDS = [..., "C:\\obsidian", ...]` のような
    「検索語としての絶対パス」(scripts/search_dev_logs.py) は素通りする。

    docstring/コメントは ast なので元々引っかからない (この関数自身の docstring が
    絶対パスを含んでいるのが何よりの理由 — check_machine_locked_syspath と同じ罠)。

    Returns: [(lineno, 該当リテラル), ...]
    """
    import ast

    norm = str(rel_path).replace("\\", "/")
    if norm in _ABS_PATH_ALLOWED or norm in _ABS_PATH_EXEMPT:
        return []
    # tests/ は「期待値としての絶対パス」を書く場所なので対象外
    if norm.startswith("tests/"):
        return []

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), str(file_path))
    except Exception:
        return []  # 構文エラーは check_syntax の担当

    findings = []

    def take(node):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and _ABS_PATH_RE.match(node.value)):
            findings.append((node.lineno, node.value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in _PATHY_CALLS:
                for arg in node.args:
                    take(arg)
                for kw in node.keywords:
                    take(kw.value)
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id.upper().endswith(_PATHY_SUFFIXES)
                   for t in node.targets):
                take(node.value)
    return findings


def check_import(module_name):
    """モジュール1件を単独のサブプロセスで import してみる (隔離再検査用)。

    通常経路はこれではなく run_import_batch() の一括 import。ここは
    **バッチが赤かった / 途中で死んだモジュールだけ** を単独で再実行し、
    他モジュールの影響を受けていない正確な traceback を得るために残してある。

    かつてこの関数は「現在のプロセスを汚さず」という理由で全モジュールに使われて
    いたが、その建前は [4] のレジストリ検査が `importlib.import_module()` で
    Guardian 自身のプロセスに import して hasattr を見ていた時点で既に破れていた
    (= 各モジュールはサブプロセスとインプロセスで二重に import されていた)。
    現在は [4] の hasattr 判定もバッチ側 (子プロセス) に寄せてあるので、
    Guardian 本体は検査対象モジュールを一切 import しない。
    """
    try:
        cmd = [PYTHON_EXE, "-c", f"import {module_name}; print('OK')"]
        # encoding/errors は必須 (LEVEL 1)。text=True だけだとロケール既定で復号するため、
        # 日本語を含む traceback が cp1252 ロケール (GitHub Actions の runner) で
        # UnicodeDecodeError になり、reader thread が死んで stderr が空になる。
        # 結果 Guardian は「Import Error: <mod>」だけを出し理由を説明できない。
        # 開発機 (cp932) では偶然復号できるので、CI で初めて顕在化した (2026-07-30)。
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding='utf-8', errors='replace',
                                cwd=str(ROOT_DIR))
        if result.returncode == 0:
            return True, ""
        # stderr が空でも「理由なし」で返さない。os._exit() やクラッシュで死んだ
        # モジュールは traceback を残さないので、これを素通しすると
        # 「Import Error: <mod>」だけが出て理由が一行も無い状態になる
        # (= 診断層そのものの無言の死。cp1252 の stderr 消失と同じ結末)。
        reason = (result.stderr or "").strip()
        if not reason:
            reason = (f"(stderr なし) 子プロセスが exit code {result.returncode} で終了。"
                      f"import 中に os._exit() / プロセスクラッシュが起きた可能性")
        return False, reason
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------
# [3][4][6] の一括 import (バッチ)
# ---------------------------------------------------------
# 旧実装は「1モジュール = 1サブプロセス」で 35 プロセス (ユニークは27。8件は
# [3] と [4] の重複) を起動しており、フル実行 164.69秒 のほぼ全部がここだった。
# 単価が 0.04秒 (lib.pid_guard) 〜 7.37秒 (bot_discord) とばらけるのは、重い
# モジュールが ccxt / pandas / google.genai を引き連れてくるからで、27回とも
# **同じ重量級スタックをゼロからロードし直していた**のが本当の原因。
#
# 実測:
#   旧実装のフル (35プロセス) ... 164.69秒 (master 241c8e3) / 172.40秒 (master d6a3f46)
#   ユニーク27件を8並列 ....... 30.03秒
#   ユニーク27件を1プロセス ... **6.98秒** (バッチ化後のフル実測 7.5秒)
# つまり並列化はバッチ化に4倍負ける。16コアあっても、各プロセスが同じ依存を
# 独立にロードする無駄は消えないため。**ここを取り違えて並列化に走らないこと。**
#
# バッチ化で検出力が落ちないことは細工モジュールで検証済み (5/5):
#   存在しないモジュール / import 時 sys.exit(1) / その後続モジュールへの到達 /
#   import 時の sys.path 汚染 / 正常モジュールを誤検出しない。
_BATCH_TIMEOUT_SEC = 300

# 子プロセスで走るバッチワーカー。`-c` で渡すのは sys.path[0] を cwd に
# したいため (一時ファイルにすると sys.path[0] がその一時ディレクトリになり、
# 「リポジトリからの import 解決」を検証したいという目的が崩れる)。
_BATCH_WORKER_SRC = r'''
import importlib
import json
import sys
import traceback

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
# stdin もロケール既定 (開発機は cp932) ではなく utf-8 で読む。
# 現状 spec は ASCII のモジュール名だけだが、ここを暗黙のロケールに任せると
# 「開発機では偶然通り CI で落ちる」型の欠陥を仕込むことになる。
sys.stdin.reconfigure(encoding='utf-8')


def emit(rec):
    # 1件ずつ flush する。バッチが途中で強制終了しても (os._exit / クラッシュ)
    # そこまでの結果は親に届き、親は「届かなかったモジュール」だけを
    # 単独プロセスで再検査できる。まとめて最後に出すと全部巻き添えになる。
    #
    # 先頭の改行は必須。import 時に改行なしで stdout へ書くモジュール
    # (「Loading...」等のバナー) があると、その続きに JSON が連結されて
    # 行頭が `{` でなくなり、親がその1件を黙って捨てる。
    sys.stdout.write("\n" + json.dumps(rec, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    spec = json.loads(sys.stdin.read())
    for entry in spec["modules"]:
        name = entry["module"]
        rec = {"module": name, "ok": True, "error": "",
               "missing_attrs": [], "syspath_added": [], "keys": {}}
        before = list(sys.path)
        mod = None
        try:
            mod = importlib.import_module(name)
        except BaseException:
            # Exception ではなく BaseException。import 時の sys.exit() は
            # SystemExit (Exception のサブクラスではない) なので、Exception で
            # 受けると取り逃して検査が黙る。2026-07-30 に bot_discord.py で
            # 直したばかりの欠陥が再発したとき、それが最悪の壊れ方になる。
            rec["ok"] = False
            rec["error"] = traceback.format_exc()
        # sys.path をモジュール毎にスナップショット & 復元する。
        # 復元しないと、先に import したモジュールが足したパスのおかげで
        # 後続が import できてしまい、個別実行より検出が甘くなる。
        rec["syspath_added"] = [p for p in sys.path if p not in before]
        sys.path[:] = before
        if mod is not None:
            # 属性の取得自体が例外を投げうる (PEP 562 のモジュール __getattr__)。
            # ここで素通しするとワーカーごと死に、隔離再検査は import しか見ないので
            # **属性検査だけが黙って消える**。赤として報告する。
            try:
                rec["missing_attrs"] = [a for a in entry.get("attrs", []) if not hasattr(mod, a)]
                for a in entry.get("keys_of", []):
                    rec["keys"][a] = list(getattr(mod, a, None) or {})
            except BaseException:
                rec["ok"] = False
                rec["error"] = "属性の検査中に例外:\n" + traceback.format_exc()
        emit(rec)
    emit({"__done__": True})


main()
'''

# バッチが赤かったモジュールの単独再検査結果のメモ ({module: (ok, reason)})。
# [3] と [4] の両方に出てくるモジュールで2回起動しないため。
_ISOLATED_CACHE = {}


def _as_text(value):
    """TimeoutExpired.stdout 等が bytes で返る実装差を吸収する"""
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value or ""


def run_import_batch(specs):
    """モジュール群を1つの子プロセスでまとめて import する。

    specs: [{"module": str, "attrs": [str, ...], "keys_of": [str, ...]}, ...]
    Returns: (results, note)
      results: {module: レコード} — **子プロセスから届いたものだけ**。
               途中で死んだ場合、以降のモジュールは results に入らない
               (呼び出し側が単独プロセスで再検査する)
      note:    バッチ自体の異常を説明する文字列。正常なら ""
    """
    payload = json.dumps({"modules": specs}, ensure_ascii=False)
    stdout, stderr, note = "", "", ""
    try:
        # encoding/errors は必須 (LEVEL 1)。text=True だけだとロケール既定で
        # 復号するため、日本語を含む traceback が cp1252 の CI runner で
        # UnicodeDecodeError になり **失敗の理由だけが消える** (2026-07-30 に実際に踏んだ)。
        cmd = [PYTHON_EXE, "-c", _BATCH_WORKER_SRC]
        proc = subprocess.run(cmd, input=payload, capture_output=True,
                              encoding='utf-8', errors='replace',
                              cwd=str(ROOT_DIR), timeout=_BATCH_TIMEOUT_SEC)
        stdout, stderr = _as_text(proc.stdout), _as_text(proc.stderr)
    except subprocess.TimeoutExpired as e:
        stdout, stderr = _as_text(e.stdout), _as_text(e.stderr)
        note = f"バッチが {_BATCH_TIMEOUT_SEC} 秒で終わりませんでした"
    except Exception as e:
        note = f"バッチを起動できませんでした ({type(e).__name__}: {e})"

    results, done = {}, False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("__done__"):
            done = True
        elif "module" in rec:
            results[rec["module"]] = rec

    if not done and not note:
        tail = stderr.strip()[-400:]
        note = ("バッチが完走しませんでした (import 中にプロセスごと落ちた可能性)"
                + (f"\n{tail}" if tail else ""))
    return results, note


def _isolated(module_name):
    """単独プロセスで再検査する (同一モジュールは1回だけ起動)"""
    if module_name not in _ISOLATED_CACHE:
        _ISOLATED_CACHE[module_name] = check_import(module_name)
    return _ISOLATED_CACHE[module_name]


def module_verdict(results, module_name):
    """バッチ結果から (ok, 理由) を出す。赤いときだけ単独プロセスで再検査する。

    緑の常態ではサブプロセスは1本 (バッチ) しか起動しないので約7秒。
    赤いときだけ「失敗件数 × 約5秒」を払って、他モジュールに汚されていない
    正確な traceback を得る。
    """
    rec = results.get(module_name)
    if rec is not None and rec.get("ok"):
        return True, ""

    ok, err = _isolated(module_name)
    if rec is None:
        # バッチが途中で死んで到達しなかった。単独で通るならモジュール自体は健全。
        if ok:
            return True, ""
        return False, f"(バッチ未到達のため単独プロセスで再検査)\n{err}"

    if ok:
        # 単独では通るのにバッチでは落ちた = 先行モジュールの副作用に依存/破壊されている。
        # これはバッチ化が作った偽陽性ではなく、実際の順序依存なので赤のままにする。
        return False, ("単独では import できるが、他モジュールと同一プロセスで"
                       "import すると失敗する (順序依存の副作用)\n"
                       "--- バッチ内の traceback ---\n" + rec.get("error", ""))

    # どちらも赤。**片方で上書きしない** — 単独プロセスは traceback を持たない
    # ことがある (bare `sys.exit(1)` は何も出力せず終了コードだけ返す) 一方、
    # バッチ側は必ず traceback.format_exc() を持っている。単独の結果で上書きすると
    # 「SystemExit だった」という一番効く情報が消える。両方あるなら両方出す。
    parts = []
    if rec.get("error", "").strip():
        parts.append("--- バッチ内の traceback ---\n" + rec["error"].strip())
    if err.strip():
        parts.append("--- 単独プロセスでの再検査 ---\n" + err.strip())
    return False, "\n".join(parts) or "理由を取得できませんでした"


def report_syspath_pollution(results):
    """import 時に sys.path へ足されたパスのうち、リポジトリルート以外を報告する。

    `__file__` 相対でリポジトリルートを挿入するのは正当な作法で、実際
    tools/tool_user_memory.py・tool_xtrend.py・tool_vault_index.py・
    tool_activity_index.py・tool_browser_use.py・lib/engagement_log.py の6ファイルが
    やっている。これらを違反にすると誤検知だらけになって検査ごと外されるので、
    **ルート以外の追加だけ**を、しかもエラーではなく警告として出す。
    """
    root = os.path.normcase(os.path.normpath(str(ROOT_DIR)))
    noisy = []
    for mod in sorted(results):
        for p in results[mod].get("syspath_added") or []:
            if not p or os.path.normcase(os.path.normpath(p)) == root:
                continue
            noisy.append((mod, p))
    for mod, p in noisy:
        print(f"  ⚠️  {mod} が import 時に sys.path へ {p!r} を追加")
    return noisy


def run_verification(fast=False):
    """プロジェクト整合性を検査する。

    fast=True では「サブプロセスを起動する検査」= [3] import 整合性 /
    [4] main.py TOOLS レジストリ / [6] Briefing プロバイダー を飛ばす。
    残る [1][2][5] は純粋な静的検査 (compile / symtable / ast / 文字列走査) で、
    構文・エンコーディング・未定義名・Discord アンチパターン・例外握りつぶしを
    カバーする。「commit を止めるべき壊れ方」の大半はここで捕まる。
    """
    print("🛡️  Antigravity Guardian - System Verification Start")
    if fast:
        print("⚡ FAST MODE: 静的検査のみ。import整合性 [3][4][6] は検査していません")
    errors = []

    # 1. パッケージ構造の確認
    required_pkgs = ['lib', 'tools', 'commands', 'tests']
    print("[1] Checking Package Structure...")
    for pkg in required_pkgs:
        init_file = ROOT_DIR / pkg / "__init__.py"
        if not init_file.exists():
            errors.append(f"Missing __init__.py in {pkg}/")
            print(f"  ❌ {pkg}/ (Missing __init__.py)")

    # 2. 指定ディレクトリの構文 & エンコーディングチェック (env 等は除外)
    print("\n[2] Checking Syntax & Encoding (Source Only)...")
    target_dirs = ['.', 'lib', 'tools', 'commands', 'tests', 'scripts']
    py_files = []
    
    for d in target_dirs:
        d_path = ROOT_DIR / d
        if d_path.is_dir():
            if d == '.':
                py_files.extend(list(d_path.glob("*.py")))
            else:
                py_files.extend(list(d_path.glob("**/*.py")))
    
    # 重複排除と正規化
    py_files = sorted(list(set(f for f in py_files if f.is_file())))
    
    count = 0
    for py_file in py_files:
        rel_path = py_file.relative_to(ROOT_DIR)
        # 除外キーワード
        if any(ex in str(rel_path) for ex in ['antigravity_env', '.gemini', '__pycache__', '.git']):
            continue

        # Encoding
        if not check_encoding(py_file):
            errors.append(f"Encoding Error: {rel_path} is not UTF-8")
            print(f"  ❌ {rel_path} (Not UTF-8)")
            continue
            
        # Syntax
        success, err_msg = check_syntax(py_file)
        if not success:
            errors.append(f"Syntax Error: {rel_path}\n{err_msg}")
            print(f"  ❌ {rel_path} (Syntax Error)")
        
        # 未定義名 (NameError 予備軍) — ファイル分割リファクタの取り残し検出
        for scope, name in check_undefined_names(py_file):
            errors.append(f"Undefined Name: {rel_path} — {scope} が '{name}' を参照")
            print(f"  ❌ {rel_path} ({scope}: '{name}' が未定義)")

        # sys.path へのルート直書き — 開発機だけ緑になる「下駄」の検出
        for lineno, literal in check_machine_locked_syspath(py_file):
            errors.append(
                f"Machine-locked sys.path: {rel_path}:{lineno} — {literal!r} を直書き。"
                f"__file__ からの相対で算出すること "
                f"(tests/ や scripts/ からなら dirname を2段)")
            print(f"  ❌ {rel_path}:{lineno} (sys.path にルート直書き: {literal!r})")

        # 実行時データパスへのルート直書き — 落ちずに別ファイルを読み書きする方の下駄
        for lineno, literal in check_hardcoded_abs_paths(py_file, rel_path):
            errors.append(
                f"Hardcoded absolute path: {rel_path}:{lineno} — {literal!r} を直書き。"
                f"リポジトリ配下なら lib/paths.py の PROJECT_ROOT/LOGS_DIR 経由、"
                f"リポジトリ外 (Vault/BitNet 等) なら lib/paths.py に "
                f"「.env で上書き可・既定値は現行値」の形で宣言して import すること")
            print(f"  ❌ {rel_path}:{lineno} (データパスにルート直書き: {literal!r})")

        count += 1

    print(f"  - Total {count} source files scanned.")

    batch_results = {}
    if not fast:
        # [3][4][6] は同じモジュール群を必要とするので、import は1回のバッチに集約し、
        # 各セクションはその結果を読むだけにする ([6] だけ別プロセスにすると
        # tool_briefing_manager を2回ロードすることになる)。
        batch_results = run_import_checks(errors)
    run_discord_checks(errors)
    if not fast:
        run_briefing_check(errors, batch_results)
        run_prod_optional_deps_check()

    # 判定
    print("\n" + "="*50)
    if not errors:
        print("✅ ALL SYSTEMS GO. Project is clean and stable.")
        if fast:
            print("   ⚡ ただし --fast。import整合性は未検査なので"
                  " リリース前に `py verify_project.py` (フル) を実行すること")
        return True
    print(f"❌ FOUND {len(errors)} ERROR(S):")
    for err in errors:
        print(f"\n--- ERROR ---\n{err}")
    return False


BRIEFING_MODULE = 'tools.tool_briefing_manager'
EXPECTED_PROVIDERS = ['Crypto', 'Obsidian', 'Gmail']

# [3] が import できることを要求する重要モジュール。
# 関数ローカルではなくモジュール定数にしてあるのは、
# tests/test_guardian_batch_import.py が「壊れたモジュールを混ぜたら
# 本当に exit 1 になるか」を差し替えで e2e 検証するため。
CRITICAL_MODULES = [
    'lib.lib_shared',
    'lib.lib_obsidian_utils',
    'lib.pid_guard',
    'tools.tool_calendar',
    'tools.tool_web',
    'tools.tool_briefing_manager',
    'tools.tool_gmail_manager',
    'commands.bot_commands_tools',
    'commands.bot_commands_curator',
    'bot_discord',
    'agent_hello',
    'main',
    'tools.tool_future_search',
    'tools.tool_skill_observer',
    'tools.tool_xtrend',
    'tools.tool_vault_index',
    'tools.tool_user_memory',
    'tools.tool_crypto_stats',
    'tools.tool_crypto_watcher',
    'engine_plan_execute',
]


def load_tools_registry(errors, main_path=None):
    """main.py の TOOLS を読み、[(name, func_path), ...] を返す。

    main.py 自体は module scope が load_dotenv とパス定義だけなので、ここで
    exec_module しても重量級の依存は引かない (重いのは TOOLS が指す先のモジュールで、
    そちらはバッチの子プロセスが import する)。

    main_path はテストが差し替えるためだけの引数 (既定は ROOT_DIR/main.py)。
    """
    main_path = main_path or (ROOT_DIR / "main.py")
    try:
        spec = importlib.util.spec_from_file_location("main_module", str(main_path))
        main_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main_mod)
    except Exception as e:
        errors.append(f"Failed to load main.py for registry verification: {e}")
        return []

    if not hasattr(main_mod, "TOOLS"):
        errors.append("main.py does not have a TOOLS list")
        return []

    entries = []
    for tool in main_mod.TOOLS:
        name = tool.get("name")
        func_path = tool.get("function")
        if not func_path:
            continue
        if "." not in func_path:
            # 黙って飛ばさない。「module.func」の形でなければレジストリの記述ミスで、
            # 飛ばすと該当ツールだけが永久に未検査になる (無言の死)。
            errors.append(f"Main Registry Error [{name}]: function が "
                          f"'module.func' の形ではありません: {func_path!r}")
            continue
        entries.append((name, func_path))
    return entries


def run_import_checks(errors):
    """[3] 重要モジュールの import 整合性 と [4] main.py TOOLS レジストリ。

    どちらもモジュール単位の import を要するので、[6] Briefing の分も含めて
    **重複を除いた27件を1つの子プロセスでまとめて import** する (run_import_batch)。
    赤かったものだけ単独プロセスで再検査する。--fast では丸ごと飛ばす
    (判断ゲートA / 2026-07-30)。

    Returns: バッチ結果 {module: レコード} — [6] が同じ結果を読むために返す。
    """
    _ISOLATED_CACHE.clear()
    registry = load_tools_registry(errors)
    critical_modules = list(CRITICAL_MODULES)

    # 重複除去: [3] の20件と [4] の15件はモジュールが8件重なっており、旧実装は
    # 同じモジュールを2回ずつ起動していた。ユニークな27件だけを1回 import する。
    # dict は挿入順を保つので、宣言順 = import 順 (再現性のため)。
    specs = {}
    for mod in critical_modules:
        specs.setdefault(mod, {"module": mod, "attrs": [], "keys_of": []})
    for _name, func_path in registry:
        module_name, func_name = func_path.rsplit(".", 1)
        entry = specs.setdefault(module_name, {"module": module_name, "attrs": [], "keys_of": []})
        if func_name not in entry["attrs"]:
            entry["attrs"].append(func_name)
    # [6] Briefing プロバイダーも同じバッチで取る (別プロセスにすると
    # tools.tool_briefing_manager を2回ロードすることになる)
    bm = specs.setdefault(BRIEFING_MODULE,
                          {"module": BRIEFING_MODULE, "attrs": [], "keys_of": []})
    if "_providers" not in bm["keys_of"]:
        bm["keys_of"].append("_providers")

    results, note = run_import_batch(list(specs.values()))
    if note:
        # バッチ自体の異常はエラーにしない: 到達しなかったモジュールは
        # module_verdict() が単独プロセスで再検査するので検出力は落ちない。
        # ただし黙って遅くなる/甘くなるのは避けたいので必ず出力する。
        print(f"  ⚠️  バッチ実行が正常終了しませんでした — {note}")

    print("\n[3] Checking Import Integrity...")
    for mod in critical_modules:
        ok, reason = module_verdict(results, mod)
        if ok:
            print(f"  ✅ {mod}")
        else:
            errors.append(f"Import Error: {mod}\n{reason}")
            print(f"  ❌ {mod}")

    # 4. main.py の TOOLS レジストリ検証
    print("\n[4] Verifying main.py TOOLS registry...")
    for name, func_path in registry:
        module_name, func_name = func_path.rsplit(".", 1)
        ok, reason = module_verdict(results, module_name)
        if not ok:
            errors.append(f"Main Registry Error [{name}]: Module {func_path} not found\n{reason}")
            print(f"  ❌ {name} ({func_path})")
            continue
        # 関数存在チェック。旧実装は Guardian 自身のプロセスに import して
        # hasattr を見ていたが、判定はバッチの子プロセスで済ませてある。
        if func_name in (results.get(module_name, {}).get("missing_attrs") or []):
            errors.append(f"Main Registry Error [{name}]: Function {func_name} not found in {module_name}")
            print(f"  ❌ {name} ({func_name} missing)")
        else:
            print(f"  ✅ {name}")

    report_syspath_pollution(results)
    return results


def run_briefing_check(errors, results):
    """[6] Briefing プロバイダー登録チェック。

    かつては専用のサブプロセスを1本起動していたが、必要な情報 (_providers のキー)
    は [3][4] のバッチが同じ子プロセス内で既に取っているので、ここではその結果を
    読むだけにする。--fast では [3][4] ごと飛ばすので results が空になり、
    このセクション自体呼ばれない。
    """
    print("\n[6] Checking Briefing Providers...")
    rec = results.get(BRIEFING_MODULE)
    if rec is None or not rec.get("ok"):
        # import 自体の失敗は [3] が既にエラーとして積んでいる。ここで二重に
        # 積むと件数が水増しされるので、事実だけ出して判定は [3] に委ねる。
        print(f"  ❌ {BRIEFING_MODULE} を import できないため未検査 ([3] のエラーを参照)")
        return
    registered = rec.get("keys", {}).get("_providers") or []
    missing = [p for p in EXPECTED_PROVIDERS if p not in registered]
    if missing:
        msg = f"Briefing providers missing: {','.join(missing)} (登録済み: {','.join(registered) or 'なし'})"
        errors.append(msg)
        print(f"  ❌ {msg}")
    else:
        print(f"  ✅ Registered: {','.join(registered)}")


# 本番 (scriptsrms_core.bat の `py -u main.py`) で使う任意依存。未導入でも
# 劣化フォールバックが黙って動くため、入れ忘れると誰も気付けない
# (2026-08-01 docling を antigravity_env にだけ入れて本番が毎回 HTML 抽出に落ちていた)。
PROD_OPTIONAL_DEPS = ("docling",)


def missing_prod_optional_deps(modules=PROD_OPTIONAL_DEPS):
    """本番と同じ `py` ランチャーで find_spec し、見つからないモジュール名を返す。

    `py` は activate 中の venv (VIRTUAL_ENV) を優先するので、それを外した env で
    呼ぶ — 外さないと antigravity_env を見て「入っている」と誤判定する。
    `py` が無い環境 (非Windows) では None。
    """
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    code = ("import importlib.util,sys;"
            "print(','.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))")
    try:
        r = subprocess.run(["py", "-c", code, *modules], env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [m for m in r.stdout.strip().split(",") if m]


def run_prod_optional_deps_check():
    """[7] 本番 Python の任意依存。警告のみ (CI には入っていないので error にしない)。"""
    print("\n[7] Checking Optional Deps on Production Python (`py`)...")
    missing = missing_prod_optional_deps()
    if missing is None:
        print("  ⏭️  `py` ランチャーを実行できないため未検査")
    elif missing:
        print(f"  ⚠️  未導入: {', '.join(missing)} — 劣化フォールバックで動き続けます。"
              f"本番で使うなら `py -m pip install` (venv を deactivate した状態で)")
    else:
        print(f"  ✅ {', '.join(PROD_OPTIONAL_DEPS)}")


def run_discord_checks(errors):
    """[5] Discord アンチパターン と bot 層の例外握りつぶし。

    どちらも純粋な文字列走査 / ast なのでサブプロセス不要 = --fast でも実行する。
    """
    # 5. Discord パターン禁止チェック
    print("\n[5] Checking Discord Anti-Patterns...")
    discord_files = [
        ROOT_DIR / 'bot_discord.py',
        ROOT_DIR / 'commands' / 'bot_commands_tools.py',
        ROOT_DIR / 'lib' / 'discord_response.py',
    ]
    for df in discord_files:
        if not df.exists():
            continue
        src = df.read_text(encoding='utf-8')
        rel = df.relative_to(ROOT_DIR)
        # io.StringIO を discord.File に渡すパターン（make_discord_file の定義行は除外）
        for i, line in enumerate(src.splitlines(), 1):
            if 'discord.File' in line and 'io.StringIO' in line and 'def make_discord_file' not in line:
                msg = f"Discord anti-pattern: {rel}:{i} — use make_discord_file() instead of discord.File(io.StringIO())"
                errors.append(msg)
                print(f"  ❌ {rel}:{i}")
        print(f"  ✅ {rel} (no StringIO pattern)")

    # 例外の握りつぶし (ログを残さず Discord に出すだけ) — bot 層のみが対象。
    # tools/ の `return f"Error: {e}"` は呼び出し元への戻り値契約なので対象外。
    bot_layer = (
        [ROOT_DIR / 'bot_discord.py', ROOT_DIR / 'lib' / 'discord_response.py']
        + sorted((ROOT_DIR / 'commands').glob('*.py'))
    )
    swallowed = 0
    for bf in bot_layer:
        if not bf.exists():
            continue
        rel = bf.relative_to(ROOT_DIR)
        for lineno, summary in check_swallowed_exceptions(bf):
            swallowed += 1
            errors.append(
                f"Swallowed Exception: {rel}:{lineno} — 例外をユーザーに見せるだけで"
                f"ログに残していない。lib.bot_errors.log_command_error() を使うこと\n"
                f"    {summary}"
            )
            print(f"  ❌ {rel}:{lineno} (例外がログに残らない)")
    if swallowed == 0:
        print(f"  ✅ bot層 {len(bot_layer)} ファイル (握りつぶした例外なし)")


if __name__ == "__main__":
    success = run_verification(fast="--fast" in sys.argv[1:])
    sys.exit(0 if success else 1)
