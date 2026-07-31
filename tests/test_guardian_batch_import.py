# -*- coding: utf-8 -*-
"""Guardian の import 検査をバッチ化しても検出力が落ちないことの回帰テスト

このテストが存在する理由 (2026-07-31):
  `verify_project.py` の [3] import整合性 / [4] TOOLSレジストリ / [6] Briefing は、
  「1モジュール = 1サブプロセス」で 35 プロセス (ユニーク27・8件は重複) を起動しており、
  フル実行 172秒 のほぼ全部がここだった。単価が 0.04秒〜7.37秒 とばらけるのは
  重いモジュールが ccxt / pandas / google.genai を引き連れてくるからで、
  **27回とも同じ重量級スタックをゼロからロードし直していた**のが原因。
  1プロセスにまとめれば `sys.modules` にキャッシュされて 7秒台で終わる
  (8並列は 30秒 = 並列化はバッチ化に4倍負ける)。

  だがこの高速化は **検出力と引き換えになりうる**:
    - 1件目が `sys.exit()` したらバッチごと死んで、以降が未検査のまま緑に見える
    - `except Exception` で受けると `SystemExit` を取り逃して黙る
    - 先に import したモジュールが `sys.path` を汚すと、後続が「本来 import
      できないのにできてしまう」
    - バッチがプロセスごと落ちると、届かなかった分が単に消える
  いずれも **「検査が空回りしていても "違反0件" と同じ出力になる」**
  というこのリポジトリの署名的欠陥そのものなので、5つの仕込みで固定する。

  [1] 関数単体 — バッチが各ケースで何を返すか
  [2] バッチが途中で死んだとき — 届かなかった分を単独プロセスで救う
  [3] e2e — 壊れたモジュールを混ぜたら run_verification が本当に exit 1 になるか
  [4] [6] がバッチ結果を読むだけになっているか (プロセスを増やしていないか)
  [5] 緑の常態が速いままか (172秒コースへの復帰検出)

実行: py tests/test_guardian_batch_import.py
"""
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import verify_project as v  # noqa: E402

# **`py` / `python3` と書かないこと。** このテストは子プロセスで
# verify_project を実走させるが、`py` (Windows の Python Launcher) は
# 「このテストを動かしている interpreter」とは限らない。
# CI では actions/setup-python の toolcache Python がテストを実行する一方
# `py` は別のシステム Python を指すので、pip install した依存が見えず
# `ModuleNotFoundError: No module named 'dotenv'` で落ちる (2026-07-31 の
# CI 初起動で実際に踏んだ)。開発機は `py tests/...` で走らせるので
# `py` == 同じ interpreter となり、**開発機だけ緑**になっていた。
# verify_project.py 自身は `PYTHON_EXE = sys.executable` で正しく、
# 下駄を履いていたのはこのテストだけだった。
LAUNCHER = sys.executable

# フル実行の上限。ローカル実測は 7.5秒 (基準 172.40秒) だが、CI の runner は
# 数倍遅いので余裕を持たせる。ここを超えるなら「1モジュール1プロセス」に
# 戻っている疑い — 60秒でも旧実装 (172秒) は確実に捕まる。
FULL_BUDGET_SEC = 60

_PASS = []
_FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASS.append(name)
        print(f"  ✅ {name}")
    else:
        _FAIL.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


# ---------------------------------------------------------------------------
# プローブ (一時モジュール)
# ---------------------------------------------------------------------------
# tests/ は __init__.py を持つパッケージなので `tests._tmp_...` で import できる。
# `_tmp_` 始まりなので tests/test_*.py のスイープには拾われない。
PROBES = {
    "_tmp_gbi_ok": (
        "# -*- coding: utf-8 -*-\n"
        "def hello():\n"
        "    return 'ok'\n"
    ),
    "_tmp_gbi_exit": (
        "# -*- coding: utf-8 -*-\n"
        "'''import しただけで sys.exit(1) するモジュール。\n"
        "2026-07-30 に bot_discord.py で直したばかりの欠陥 (module scope の sys.exit)。\n"
        "except Exception で受けると SystemExit を取り逃すので、ここが緑に見えたら\n"
        "検査が黙っている。'''\n"
        "import sys\n"
        "sys.exit(1)\n"
    ),
    "_tmp_gbi_syspath": (
        "# -*- coding: utf-8 -*-\n"
        "'''import 時に sys.path を汚すモジュール。'''\n"
        "import os\n"
        "import sys\n"
        "POLLUTION = os.sep.join(['', 'tmp', 'guardian-probe-pollution'])\n"
        "sys.path.append(POLLUTION)\n"
    ),
    "_tmp_gbi_after": (
        "# -*- coding: utf-8 -*-\n"
        "'''sys.exit / クラッシュするプローブの「後続」。ここに到達できるかを見る。'''\n"
        "REACHED = True\n"
    ),
    "_tmp_gbi_crash": (
        "# -*- coding: utf-8 -*-\n"
        "'''import 中にプロセスごと落ちるモジュール (try/except で捕まえられない)。'''\n"
        "import os\n"
        "os._exit(3)\n"
    ),
    "_tmp_gbi_slow": (
        "# -*- coding: utf-8 -*-\n"
        "'''import が終わらないモジュール (バッチのタイムアウト経路の検証用)。'''\n"
        "import time\n"
        "time.sleep(120)\n"
    ),
    "_tmp_gbi_banner": (
        "# -*- coding: utf-8 -*-\n"
        "'''import 時に改行なしで stdout に書くモジュール (バナー出力の再現)。'''\n"
        "import sys\n"
        "sys.stdout.write('Loading tool...')\n"
    ),
}

MISSING_MODULE = "tests._tmp_gbi_does_not_exist_xyz"
POLLUTION_PATH = os.sep.join(["", "tmp", "guardian-probe-pollution"])


def _write_probes():
    for name, src in PROBES.items():
        with open(os.path.join(_PROJECT_ROOT, "tests", f"{name}.py"), "w", encoding="utf-8") as f:
            f.write(src)


def _remove_probes():
    for name in PROBES:
        path = os.path.join(_PROJECT_ROOT, "tests", f"{name}.py")
        if os.path.exists(path):
            os.remove(path)


def _spec(module, attrs=(), keys_of=()):
    return {"module": module, "attrs": list(attrs), "keys_of": list(keys_of)}


# ---------------------------------------------------------------------------
# [1] 関数単体
# ---------------------------------------------------------------------------
def test_batch_detects_each_failure_shape():
    print("\n[1] バッチが各ケースで何を返すか")
    results, note = v.run_import_batch([
        _spec("tests._tmp_gbi_ok", attrs=["hello", "no_such_function"]),
        _spec(MISSING_MODULE),
        _spec("tests._tmp_gbi_exit"),
        _spec("tests._tmp_gbi_syspath"),
        _spec("tests._tmp_gbi_after"),
    ])

    check("バッチが完走する (note なし)", note == "", f"note={note!r}")

    ok = results.get("tests._tmp_gbi_ok", {})
    check("正常なモジュールを誤検出しない", ok.get("ok") is True, f"{ok!r}")
    check("存在する関数は missing_attrs に入らない",
          "hello" not in (ok.get("missing_attrs") or []), f"{ok.get('missing_attrs')!r}")
    check("存在しない関数を missing_attrs で報告する",
          ok.get("missing_attrs") == ["no_such_function"], f"{ok.get('missing_attrs')!r}")

    miss = results.get(MISSING_MODULE, {})
    check("存在しないモジュールを検出する", miss.get("ok") is False, f"{miss!r}")
    check("理由が ModuleNotFoundError と分かる",
          "ModuleNotFoundError" in (miss.get("error") or ""), f"{miss.get('error')!r}")

    ex = results.get("tests._tmp_gbi_exit", {})
    check("import 時の sys.exit(1) を検出する", ex.get("ok") is False, f"{ex!r}")
    check("SystemExit として説明できる (except BaseException で受けている)",
          "SystemExit" in (ex.get("error") or ""), f"{ex.get('error')!r}")

    after = results.get("tests._tmp_gbi_after", {})
    check("sys.exit した後続モジュールにも到達する", after.get("ok") is True,
          "バッチが1件目の SystemExit で死んでいる = 以降が未検査のまま緑になる")

    pol = results.get("tests._tmp_gbi_syspath", {})
    check("import 時の sys.path 汚染を報告する",
          POLLUTION_PATH in (pol.get("syspath_added") or []),
          f"syspath_added={pol.get('syspath_added')!r}")
    check("汚染はモジュール毎に復元される (後続に漏れない)",
          POLLUTION_PATH not in (after.get("syspath_added") or []),
          f"後続に漏れている: {after.get('syspath_added')!r}")


def test_syspath_report_ignores_repo_root():
    """`__file__` 相対でリポジトリルートを挿入する正当なコードを警告しない。

    実際 tools/tool_user_memory.py・tool_xtrend.py・tool_vault_index.py・
    tool_activity_index.py・tool_browser_use.py・lib/engagement_log.py の6ファイルが
    これをやっている。全部を違反にすると誤検知だらけになって検査ごと外される。
    """
    print("\n[1b] sys.path 報告の線引き")
    noisy = v.report_syspath_pollution({
        "mod_root": {"syspath_added": [_PROJECT_ROOT]},
        "mod_root_slashy": {"syspath_added": [_PROJECT_ROOT + os.sep]},
        "mod_noisy": {"syspath_added": [POLLUTION_PATH]},
        "mod_clean": {"syspath_added": []},
    })
    reported = {m for m, _ in noisy}
    check("リポジトリルートの挿入は警告しない", "mod_root" not in reported, f"{noisy!r}")
    check("末尾セパレータ付きのルートも警告しない", "mod_root_slashy" not in reported, f"{noisy!r}")
    check("ルート以外の追加は警告する", "mod_noisy" in reported, f"{noisy!r}")


# ---------------------------------------------------------------------------
# [2] バッチが途中で死んだとき
# ---------------------------------------------------------------------------
def test_batch_crash_falls_back_to_isolated():
    """import 中にプロセスごと落ちても、届かなかった分を単独プロセスで救う。

    バッチ化の一番の危険はここ: 1件が os._exit / セグフォで落ちると、以降の
    モジュールは results に「存在しない」だけになり、素朴な実装では
    **未検査なのに緑**になる。届いた分だけを信用し、残りは単独で再検査する。
    """
    print("\n[2] バッチがプロセスごと落ちたとき")
    v._ISOLATED_CACHE.clear()
    results, note = v.run_import_batch([
        _spec("tests._tmp_gbi_ok"),
        _spec("tests._tmp_gbi_crash"),
        _spec("tests._tmp_gbi_after"),
    ])

    check("落ちる前の結果は届いている", results.get("tests._tmp_gbi_ok", {}).get("ok") is True,
          f"{results!r}")
    check("完走しなかったことを検出する (note が空でない)", note != "",
          "バッチが途中で死んだのに正常終了扱いになっている")
    check("落ちた以降のモジュールは results に無い",
          "tests._tmp_gbi_after" not in results, f"{sorted(results)!r}")

    ok, reason = v.module_verdict(results, "tests._tmp_gbi_after")
    check("未到達でも単独再検査で緑に救われる", ok is True, f"reason={reason!r}")

    ok2, reason2 = v.module_verdict(results, "tests._tmp_gbi_crash")
    check("落とした本人は赤のまま", ok2 is False, "プロセスを殺すモジュールが緑になっている")
    check("未到達だったことが理由に書かれる", "単独" in reason2, f"reason={reason2!r}")


def test_batch_timeout_is_reported():
    """バッチが時間内に終わらなかったとき、届いた分を残しつつ note を立てる。

    タイムアウトを「例外が出た = 全件不明」として捨てると、以降が単独再検査に
    落ちるだけで済むはずのところが全滅する。逆に note を立て忘れると
    **未完走なのに正常終了扱い**になる。
    """
    print("\n[2c] バッチがタイムアウトしたとき")
    original = v._BATCH_TIMEOUT_SEC
    # 3秒。子の起動 + 軽量モジュール1件の import は 0.2秒程度なので、
    # 遅い CI runner でも「1件目が届いてから」タイムアウトする余裕がある。
    v._BATCH_TIMEOUT_SEC = 3
    try:
        results, note = v.run_import_batch([
            _spec("tests._tmp_gbi_ok"),
            _spec("tests._tmp_gbi_slow"),
            _spec("tests._tmp_gbi_after"),
        ])
    finally:
        v._BATCH_TIMEOUT_SEC = original

    check("タイムアウトを検出する (note が空でない)", note != "", "正常終了扱いになっている")
    check("タイムアウトだと分かる文言", "秒" in note, f"note={note!r}")
    check("タイムアウト前の結果は届いている",
          results.get("tests._tmp_gbi_ok", {}).get("ok") is True, f"{sorted(results)!r}")


def test_banner_output_does_not_swallow_a_record():
    """import 時に改行なしで stdout へ書くモジュールがあっても記録を落とさない。

    ワーカーが JSON を書く前に改行を出さないと、モジュールのバナー出力に
    JSON が連結されて行頭が `{` でなくなり、親がその1件を黙って捨てる。
    捨てられた1件は「未到達」として単独再検査に落ちるので **赤にはならず遅くなるだけ** —
    つまり誰も気付かないまま高速化が空洞化する。
    """
    print("\n[2d] バナー出力を挟んでも記録を落とさない")
    results, note = v.run_import_batch([
        _spec("tests._tmp_gbi_banner"),
        _spec("tests._tmp_gbi_ok"),
    ])
    check("バッチが完走する", note == "", f"note={note!r}")
    check("バナーを出したモジュール自身の記録が届く",
          results.get("tests._tmp_gbi_banner", {}).get("ok") is True, f"{sorted(results)!r}")
    check("後続の記録も届く",
          results.get("tests._tmp_gbi_ok", {}).get("ok") is True, f"{sorted(results)!r}")


def test_isolated_recheck_always_gives_a_reason():
    """単独再検査が「理由なしの赤」を返さないこと。

    os._exit() で死ぬモジュールは traceback を残さないので stderr が空になる。
    それを素通しすると「Import Error: <mod>」だけが出て理由が一行も無い —
    診断層そのものの無言の死 (cp1252 で stderr が消えたのと同じ結末)。
    """
    print("\n[2e] 単独再検査は必ず理由を返す")
    v._ISOLATED_CACHE.clear()
    ok, reason = v.check_import("tests._tmp_gbi_crash")
    check("os._exit するモジュールは赤", ok is False, "緑になっている")
    check("理由が空でない", bool(reason.strip()), "理由が一行も無い")
    check("終了コードが理由に含まれる", "3" in reason, f"reason={reason!r}")


def test_registry_rejects_malformed_function_path():
    """TOOLS の function が 'module.func' 形でないときに黙って飛ばさない。

    飛ばすとそのツールだけが永久に未検査になる (無言の死)。
    """
    print("\n[2f] TOOLS の function の形")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        good = os.path.join(d, "main_ok.py")
        with open(good, "w", encoding="utf-8") as f:
            f.write("# -*- coding: utf-8 -*-\n"
                    "TOOLS = [{'name': 'a', 'function': 'lib.paths.PROJECT_ROOT'},\n"
                    "         {'name': 'b', 'function': 'nodots'},\n"
                    "         {'name': 'c'}]\n")
        errors = []
        entries = v.load_tools_registry(errors, main_path=good)
        check("正しい形だけが検査対象になる", entries == [("a", "lib.paths.PROJECT_ROOT")],
              f"{entries!r}")
        check("'module.func' 形でない function をエラーにする", len(errors) == 1, f"{errors!r}")
        check("該当ツール名が出る", "[b]" in (errors[0] if errors else ""), f"{errors!r}")

        no_tools = os.path.join(d, "main_no_tools.py")
        with open(no_tools, "w", encoding="utf-8") as f:
            f.write("# -*- coding: utf-8 -*-\nX = 1\n")
        errors = []
        v.load_tools_registry(errors, main_path=no_tools)
        check("TOOLS が無ければエラー", len(errors) == 1, f"{errors!r}")

        errors = []
        v.load_tools_registry(errors, main_path=os.path.join(d, "does_not_exist.py"))
        check("読めなければエラー", len(errors) == 1, f"{errors!r}")


def test_module_verdict_reports_order_dependency():
    """バッチで赤・単独で緑 = 順序依存の副作用。これは偽陽性ではないので赤のまま。"""
    print("\n[2b] 順序依存 (バッチ赤 / 単独緑) の扱い")
    v._ISOLATED_CACHE.clear()
    results = {"tests._tmp_gbi_ok": {"module": "tests._tmp_gbi_ok", "ok": False,
                                     "error": "RuntimeError: 先行モジュールが壊した"}}
    ok, reason = v.module_verdict(results, "tests._tmp_gbi_ok")
    check("単独で通っても赤のままにする", ok is False,
          "バッチ化が隠した順序依存を「偽陽性」として捨てている")
    check("バッチ内 traceback を理由に残す", "RuntimeError" in reason, f"{reason!r}")


# ---------------------------------------------------------------------------
# [3] e2e — 本当に exit 1 になるか
# ---------------------------------------------------------------------------
def _e2e(extra_modules=(), registry=None):
    """run_verification() を子プロセスで実走させ、(exit code, stdout) を返す。

    CRITICAL_MODULES / load_tools_registry だけを差し替える。verify_project 側に
    テスト専用の抜け道を作らずに「壊れたら赤くなるか」を通しで見るため。
    cwd=リポジトリルートなので sys.path をいじる必要はない。

    **CRITICAL_MODULES は本物の27件に足すのではなく置き換える。** ここで見たいのは
    「赤が終了コードまで配線されているか」であって、実在27モジュールが import
    できるかではない (それは [7] のフル実行が本物で見る)。

    **プローブを先頭・実在モジュール (`lib.paths`) を末尾**に置くこと。逆にすると
    「後続の実在モジュールも検査される」の assert が何も証明しなくなる
    (プローブが最後なので後続が存在しない = 常に緑)。

    e2e 1本の下限は約6秒で、内訳は [2] の全ソース走査 (1.2秒) と
    [6] が要求する tools.tool_briefing_manager -> google.genai (3.5秒)。
    **CRITICAL_MODULES をいくら削ってもここは消えない**ので、
    赤いケースは1本にまとめる (実測: 4本 32秒 -> 2本 12秒)。
    """
    code = (
        "import sys\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "import verify_project as v\n"
        f"v.CRITICAL_MODULES = {list(extra_modules)!r} + ['lib.paths']\n"
    )
    if registry is not None:
        code += f"v.load_tools_registry = lambda errors: {registry!r}\n"
    code += "sys.exit(0 if v.run_verification() else 1)\n"
    proc = subprocess.run([LAUNCHER, "-c", code], cwd=_PROJECT_ROOT,
                          capture_output=True, encoding="utf-8", errors="replace",
                          timeout=600)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def test_e2e_every_failure_shape_turns_red():
    """壊れたものを混ぜたら run_verification() が本当に exit 1 になり、理由が読めること。

    3つの壊し方を1回の実走にまとめてある。終了コードは共有になるが、**判別は
    出力内容の assert が担う** (どれか1つしか検出されていなければ、その行の
    assert が落ちる)。e2e 1本あたり約6秒の固定費 ([2] の全走査 + genai) を
    3回払う理由が無い。
    """
    print("\n[3] e2e: 壊れたものを混ぜたら赤くなるか")
    code, out = _e2e(
        [MISSING_MODULE, "tests._tmp_gbi_exit"],
        registry=[("probe_tool", "lib.pid_guard.no_such_function")],
    )
    check("exit 1 になる", code == 1, f"exit={code}")

    check("存在しないモジュール: 名前が出る", MISSING_MODULE in out, out[-800:])
    check("存在しないモジュール: 理由 (ModuleNotFoundError) が読める",
          "ModuleNotFoundError" in out, out[-800:])

    check("import 時 sys.exit: 名前が出る", "_tmp_gbi_exit" in out, out[-800:])
    # bare `sys.exit(1)` は単独プロセスでは traceback を残さない (stderr が空・
    # 終了コードだけ)。単独再検査の結果でバッチの traceback を上書きすると
    # 「SystemExit だった」という一番効く情報が消える。両方出ること。
    check("import 時 sys.exit: SystemExit として説明される", "SystemExit" in out, out[-1500:])
    check("import 時 sys.exit: 単独再検査の結果も併記される",
          "単独プロセスでの再検査" in out, out[-1500:])

    check("TOOLS の存在しない関数名が出る", "no_such_function" in out, out[-1500:])

    # プローブは CRITICAL_MODULES の先頭にあるので、`lib.paths` は「後続」。
    # 1件目の失敗で検査が止まっていればここが落ちる。
    check("先行が失敗しても後続の実在モジュールが検査される", "✅ lib.paths" in out,
          out[-1500:])


# 逆方向 (「検査が常に赤いだけ」ではないこと) は [7] の
# test_full_run_stays_fast_and_green が担う。あちらは **本物の27モジュールと
# 本物の TOOLS 15件** を同じ missing_attrs 経路に通して緑を確認するので、
# スタブしたレジストリ1件で緑を見るより強い。
# 専用の e2e を別に持つと、6秒 (= [2] の全走査 + genai の固定費) を
# **直列レーンに**上乗せするだけになる (このファイルは共有資源の競合で
# scripts/run_test_sweep.py の SERIAL_TESTS に入っており、クリティカルパス上にある)。


# ---------------------------------------------------------------------------
# [4] [6] Briefing がバッチ結果を読むだけになっているか
# ---------------------------------------------------------------------------
def test_briefing_reads_batch_result_without_extra_process():
    print("\n[6] Briefing はバッチ結果を読むだけ")
    import ast

    errors = []
    v.run_briefing_check(errors, {
        v.BRIEFING_MODULE: {"ok": True, "keys": {"_providers": ["Crypto", "Obsidian",
                                                               "Schedule & Tasks", "Gmail"]}},
    })
    check("全プロバイダーが揃っていれば緑", errors == [], f"{errors!r}")

    errors = []
    v.run_briefing_check(errors, {
        v.BRIEFING_MODULE: {"ok": True, "keys": {"_providers": ["Crypto", "Obsidian"]}},
    })
    check("欠けていれば赤", len(errors) == 1, f"{errors!r}")
    check("欠けているプロバイダー名が出る", "Gmail" in (errors[0] if errors else ""), f"{errors!r}")

    errors = []
    v.run_briefing_check(errors, {})
    check("import 自体が失敗したケースでエラーを二重計上しない", errors == [],
          f"[3] が既に積んでいるのに [6] も積んでいる: {errors!r}")

    # プロセスを増やしていないこと (ast で subprocess 呼び出しの不在を見る)。
    # 実行時間だけで見ると「たまたま速かった」と区別できない。
    src = open(os.path.join(_PROJECT_ROOT, "verify_project.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "run_briefing_check")
    spawns = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
    check("run_briefing_check がサブプロセスを起動しない", not spawns,
          f"{len(spawns)} 件の subprocess 呼び出しが残っている")


# ---------------------------------------------------------------------------
# [5] 緑の常態が速いままか
# ---------------------------------------------------------------------------
def test_full_run_stays_fast_and_green():
    """172秒コース (1モジュール1プロセス) への復帰検出。

    「バッチ化した」と書いたコミットの後に誰かが `check_import()` をループで
    呼ぶ形に戻しても、出力は全く同じ ✅ の羅列になる — このリポジトリで
    繰り返し出ている「表示と実態の食い違い」。時間でしか区別できない。
    """
    print("\n[7] 緑の常態が速いまま")
    started = time.monotonic()
    proc = subprocess.run([LAUNCHER, os.path.join(_PROJECT_ROOT, "verify_project.py")],
                          cwd=_PROJECT_ROOT, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=600)
    elapsed = time.monotonic() - started
    check("フル実行が緑 (exit 0)", proc.returncode == 0,
          f"exit={proc.returncode}\n{(proc.stdout or '')[-800:]}")
    check(f"フル実行が {FULL_BUDGET_SEC} 秒以内 (実測 {elapsed:.2f}s / 基準 172.40s)",
          elapsed < FULL_BUDGET_SEC,
          f"{elapsed:.2f}s — 1モジュール1プロセスに戻っている疑い")
    for marker in ("[3]", "[4]", "[6]"):
        check(f"フルでは {marker} を実行する", f"\n{marker} " in (proc.stdout or ""),
              "セクションが消えている (速いのは検査していないから、になっていないか)")


if __name__ == "__main__":
    _write_probes()
    try:
        test_batch_detects_each_failure_shape()
        test_syspath_report_ignores_repo_root()
        test_batch_crash_falls_back_to_isolated()
        test_batch_timeout_is_reported()
        test_banner_output_does_not_swallow_a_record()
        test_isolated_recheck_always_gives_a_reason()
        test_registry_rejects_malformed_function_path()
        test_module_verdict_reports_order_dependency()
        test_e2e_every_failure_shape_turns_red()
        test_briefing_reads_batch_result_without_extra_process()
        test_full_run_stays_fast_and_green()
    finally:
        _remove_probes()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  ❌ {f}")
    sys.exit(1 if _FAIL else 0)
