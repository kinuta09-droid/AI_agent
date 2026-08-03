# -*- coding: utf-8 -*-
"""tests/test_*.py のスイープを並列で回すランナー (手元と CI で共用する唯一の実装)。

判定の作法は変えていない。**1ファイル1プロセスで実行し、終了コードだけで合否を決める。**
CLAUDE.md が `pytest tests/` を禁じている理由がそのままここの制約になる:
各ファイルの `sys.exit(1 if _FAIL else 0)` が判定の実体なので、複数ファイルを
1プロセスに畳むと最初の `sys.exit` で残り全部が消える。

なぜ並列化なのか (Guardian とは逆の結論):
  `plans/guardian-import-batching.md` の import 検査は「import できるか」を見るだけで
  1プロセスに畳めたので、バッチ化が並列化に4倍勝った (7s vs 30s)。
  スイープは上記のとおり畳めない = import の固定費 (`lib.lib_shared` で 3.78秒) を
  共有できないため、同じ固定費を複数コアに散らす並列化が正解になる。
  **共有ワーカープールは採らない** — テストは意図的にグローバル状態を壊す
  (sys.modules 差し替え / os.environ 書き換え / モジュール定数の再束縛) ので、
  1プロセス1ファイルだからこそそれが安全でいられる。

並列可否は宣言でしか決めない (skill `parallel-execution-safety` と同じ作法):
  「1回緑だった」は安全の証拠にならない。実際 `test_verify_fast_mode` ↔
  `test_hook_guards` の競合は8並列で FAIL 0 だったが、窓を人工的に開くと
  100% 赤くなる。判定は**共有する資源を列挙して**行い、結果をここに書く。
  どちらのリストにも無い名前は**直列レーンへ倒す** (付け忘れが安全側に出る)。
  分類漏れそのものは `tests/test_sweep_runner.py` が赤にする。

ThreadPoolExecutor を使わない理由:
  `tests/test_parallel_execute.py` の [15] が ThreadPoolExecutor の使用を
  3ファイルに限定している (エージェントの fan-out を `lib.concurrency` の
  資源上限の外で始めさせないため)。走査対象に `scripts/` が入っているのは意図的で、
  ここを例外にすると門が1つ緩む。そもそも親は CPU 仕事をせず子の終了を待つだけなので
  スレッドは要らない。**Popen を N 本まで起こしてポーリングする**方が、
  1本ごとの打ち切り (kill) を明示的に書けるぶん素直になる。
  子の出力は一時ファイルに逃がす — 8本同時に PIPE を張ると、
  親が読み出す前にパイプが詰まった子が固まる。

実行:
    py scripts/run_test_sweep.py                # 8並列
    py scripts/run_test_sweep.py --jobs 1       # 直列 (基準値の計測用)
    py scripts/run_test_sweep.py --list         # 分類だけ表示
"""
import argparse
import glob
import os
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

TESTS_DIR = os.path.join(_PROJECT_ROOT, "tests")

# 論理コア16の開発機で 8 -> 12 は12秒しか縮まない (固定費が CPU バウンドで
# 物理コア数に律速される)。隠れた共有状態を掘り起こすリスクに見合わないので 8 で止める。
DEFAULT_JOBS = 8

# 1本あたりの上限。超えたら FAIL として報告する (スイープ全体は止めない)。
# 最も遅い実測が 16.6秒 (`test_vault_semantic_smoke`) なので桁で余裕がある。
DEFAULT_TIMEOUT_SEC = 900

# --- 並列不可の宣言 ---------------------------------------------------------
# 「リポジトリ内の共有パスを書き換える」か「その最中に読む」ものだけを挙げる。
# 値は理由。理由が書けないなら分類できていないということなので、書けるまで直列に置く。
SERIAL_TESTS = {
    "test_verify_fast_mode.py":
        "tests/ と scripts/ に**わざと壊したファイルを置いた状態で** verify_project を"
        "走らせる。窓は約1秒だが、その間に verify を走らせる側は必ず赤くなる",
    "test_hook_guards.py":
        "guard_git_commit 経由で本物の verify --fast を走らせる。上と窓が重なると"
        "自分は無実なのに赤くなる (実証済み: 窓を開けたまま実行すると 100% exit 1)",
    "test_diagnostic_subprocess_encoding.py":
        "tests/_tmp_subproc_probe.py を書く。中身は意図的に規約違反の subprocess 呼び出し"
        "なので、tests/ を走査する検査が将来1つ増えただけで上の2本と同じ競合になる",
    "test_guardian_batch_import.py":
        "競合の**両側**を持つ。(a) tests/_tmp_gbi_*.py を7本置く — うち "
        "_tmp_gbi_exit は import 時に sys.exit(1)、_tmp_gbi_crash は os._exit(3) する。"
        "(b) その状態で verify_project.py を**フルで**実走させる (import 整合性まで)。"
        "つまり上の3本にとっての加害者であり、同時に被害者でもある",
    "test_ci_docs_exemption.py":
        "test_diagnostic_subprocess_encoding のコメントが予告した «tests/ を走査する"
        "検査» が実際に増えた側。tests/*.py と scripts/*.py を glob して ast で読むので、"
        "上の3本が一時ファイルを置いている窓に重なると (a) 消えた直後のパスを開いて"
        "FileNotFoundError、(b) わざと壊したファイルを走査対象として拾う、"
        "のどちらかで自分は無実なのに赤くなる",
}

# 共有する可変資源を持たないことを確認済みのもの。書き込み先は tempfile.mkdtemp か、
# 自分専用の固定名 (`logs/_test_skill_observations.jsonl` 等) のどちらかで、
# 他のテストと衝突しない。
PARALLEL_TESTS = frozenset({
    "test_activity_corpus.py",
    "test_answer_grounding.py",
    "test_audio_automation.py",
    "test_backtest_engine.py",
    "test_bitnet_fixes.py",
    "test_bitnet_new_features.py",
    "test_bitnet_practical.py",
    "test_bitnet_preprocessing.py",
    "test_bitnet_router_gate.py",
    "test_briefing_truncation.py",
    "test_bt_data.py",
    "test_button_views.py",
    "test_client_review.py",
    "test_concurrency.py",
    "test_cost_guard.py",
    "test_create_note.py",
    "test_crypto_stats.py",
    "test_crypto_sweep.py",
    "test_crypto_watcher.py",
    "test_curate_hooks.py",
    "test_curate_integration.py",
    "test_curate_note_save.py",
    "test_docling_client.py",
    "test_document_sidecar.py",
    "test_document_tool.py",
    "test_engagement_log.py",
    "test_error_logging.py",
    "test_eval_agent.py",
    "test_eval_retrieval.py",
    "test_file_lock.py",
    "test_folder_create.py",
    "test_future_search_pipeline.py",
    "test_gmail_error_reporting.py",
    "test_gmail_summary.py",
    "test_grounding.py",
    "test_gws_mcp.py",
    "test_hybrid_search.py",
    "test_import_modularized.py",
    "test_keyword_hits.py",
    "test_learning_db_rotation.py",
    "test_mcp_create.py",
    "test_moc_auto_link.py",
    "test_multiquery_rerank.py",
    "test_news_risk.py",
    "test_paper_digest.py",
    "test_paper_radar.py",
    "test_paper_radar_live.py",
    "test_parallel_execute.py",
    "test_pid_guard.py",
    "test_prediction_audit.py",
    "test_quant_core.py",
    "test_rerank_backend.py",
    "test_result_icons.py",
    "test_retriever_loop.py",
    "test_scan_crypto.py",
    "test_scheduler_setup.py",
    "test_search.py",
    "test_security_fixes.py",
    "test_service_control.py",
    "test_session_context.py",
    "test_shadow_log.py",
    "test_structured_engine.py",
    "test_sweep_runner.py",
    "test_template_integration.py",
    "test_tool_args_guard.py",
    "test_tool_registry.py",
    "test_user_memory.py",
    "test_vault_chunking.py",
    "test_vault_cosine_search.py",
    "test_vault_index_mtime.py",
    "test_vault_links.py",
    "test_vault_semantic_smoke.py",
    "test_vault_watcher.py",
    "test_verifier.py",
    "test_watchdog.py",
    "test_yt.py",
})


class Result:
    """1ファイル分の実行結果。出力はここに溜めてから一括で印字する。

    並列で実行しても**ファイル名と終了コードの対応が読める**ようにするため、
    子の stdout/stderr は逐次垂れ流さずバッファする。
    """

    def __init__(self, name, lane, code, seconds, output):
        self.name = name
        self.lane = lane
        self.code = code
        self.seconds = seconds
        self.output = output

    @property
    def ok(self):
        return self.code == 0


class _Job:
    """起動済みの子プロセス1本。出力は一時ファイルに逃がしてある。"""

    def __init__(self, path, lane, timeout):
        self.name = os.path.basename(path)
        self.lane = lane
        self.timeout = timeout
        self.started = time.monotonic()
        fd, self.out_path = tempfile.mkstemp(prefix="sweep_", suffix=".log")
        self._out = os.fdopen(fd, "wb")
        # PYTHONIOENCODING は **子プロセスにだけ** 渡す (CLAUDE.md の LEVEL 1)。
        # os.environ に代入すると自分の子孫全部に伝播するので絶対にやらない。
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(
            [sys.executable, path],
            cwd=_PROJECT_ROOT,
            stdout=self._out,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.timed_out = False

    @property
    def elapsed(self):
        return time.monotonic() - self.started

    def kill_tree(self):
        """打ち切る。Windows では孫まで落とす。

        MCP サーバや llama-cli を起こすテストがあるので、直接の子だけ殺すと
        孫が残って次のスイープに紛れ込む。Windows は taskkill /T が唯一の手。
        POSIX 側は**直接の子しか落とさない** (プロセスグループを作っていないため)。
        このリポジトリの実行環境は Windows なので、そこまではやらない。
        """
        self.timed_out = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           capture_output=True, encoding="utf-8", errors="replace")
        else:
            self.proc.kill()

    def finish(self):
        try:
            # 打ち切った直後に呼ばれる経路があるので、待ちには上限を置く。
            # taskkill が効かなかったときにスイープごと固まらせない。
            code = self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            code = 124
        self._out.close()
        try:
            with open(self.out_path, encoding="utf-8", errors="replace") as f:
                output = f.read()
        except OSError as e:
            output = f"[runner] 出力を読み戻せなかった: {e}"
        finally:
            try:
                os.remove(self.out_path)
            except OSError:
                pass
        if self.timed_out:
            # 打ち切りは「終了コードが 0 以外」に必ず落とす。時間切れを握りつぶすと
            # ハングしたテストが緑のまま素通りする。
            code = 124
            output += f"\n[runner] {self.timeout} 秒で打ち切った (タイムアウト)"
        return Result(self.name, self.lane, code, self.elapsed, output)


def classify(paths, extra_serial=(), lane_default="serial"):
    """(parallel, serial, unclassified) に振り分ける。

    宣言が無い名前は既定で直列レーンへ落とす。**付け忘れが安全側に出る**ことが
    ここでの非対称性 (誤って並列に入れるとデータ競合が黙って起きる)。
    """
    parallel, serial, unclassified = [], [], []
    for path in paths:
        name = os.path.basename(path)
        if name in extra_serial or name in SERIAL_TESTS:
            serial.append(path)
        elif name in PARALLEL_TESTS:
            parallel.append(path)
        else:
            unclassified.append(name)
            (parallel if lane_default == "parallel" else serial).append(path)
    return parallel, serial, unclassified


def _run_lane(paths, lane, jobs, timeout):
    """同時に最大 jobs 本まで起こしながら paths を消化する。

    終わった順に印字する (待っている間の無音を避ける) が、**1本分の出力は
    まとめて出す**。並列で垂れ流すとファイル名と終了コードの対応が読めなくなり、
    「終了コードだけで判定する」という前提の検算ができなくなる。
    """
    results = []
    pending = list(paths)
    running = []
    while pending or running:
        while pending and len(running) < max(1, jobs):
            running.append(_Job(pending.pop(0), lane, timeout))
        time.sleep(0.05)
        for job in list(running):
            if job.proc.poll() is None:
                if job.elapsed > timeout:
                    job.kill_tree()
                continue
            running.remove(job)
            r = job.finish()
            results.append(r)
            _print_result(r)
    return results


def _print_result(r):
    print(f"\n=== {r.name} === exit={r.code} ({r.seconds:.1f}s, {r.lane})")
    if r.output:
        print(r.output.rstrip())


def sweep(tests_dir, jobs, timeout, extra_serial=(), lane_default="serial"):
    paths = sorted(glob.glob(os.path.join(tests_dir, "test_*.py")))
    if not paths:
        print(f"❌ {tests_dir} に test_*.py が1本も無い")
        return 1

    parallel, serial, unclassified = classify(paths, extra_serial, lane_default)
    if jobs <= 1:
        # 基準値の計測用。レーンを混ぜず、ファイル名順にそのまま流す。
        parallel, serial = [], paths

    if unclassified:
        lane = "並列" if lane_default == "parallel" else "直列"
        print(f"⚠️ 並列可否が未宣言のため{lane}レーンで実行: {', '.join(sorted(unclassified))}")
        print("   scripts/run_test_sweep.py の SERIAL_TESTS / PARALLEL_TESTS に分類すること")

    print(f"対象 {len(paths)} 本 (並列 {len(parallel)} / 直列 {len(serial)}, jobs={jobs})")
    started = time.monotonic()
    results = []

    results += _run_lane(parallel, "並列", jobs, timeout)
    # 直列レーンは並列レーンが**完全に終わってから**。重ねた瞬間に隔離の意味が消える。
    results += _run_lane(serial, "直列", 1, timeout)

    wall = time.monotonic() - started
    failed = [r for r in results if not r.ok]

    print("\n" + "=" * 60)
    print(f"PASS: {len(results) - len(failed)}  FAIL: {len(failed)}  "
          f"— wall {wall:.1f}s (jobs={jobs}, 直列 {len(serial)} 本)")

    slowest = sorted(results, key=lambda r: r.seconds, reverse=True)[:5]
    print("遅い順: " + " / ".join(f"{r.name} {r.seconds:.1f}s" for r in slowest))

    if failed:
        print("\nFailed files:")
        for r in sorted(failed, key=lambda r: r.name):
            print(f"  - {r.name} (exit={r.code})")
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="tests/test_*.py を並列スイープする")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"並列度 (既定 {DEFAULT_JOBS}。1 なら完全直列)")
    parser.add_argument("--tests-dir", default=TESTS_DIR)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC,
                        help="1本あたりの上限秒数")
    parser.add_argument("--serial", action="append", metavar="NAME",
                        help="このファイル名を直列レーンへ強制する (繰り返し可)")
    parser.add_argument("--lane-default", choices=("serial", "parallel"), default="serial",
                        help="どちらのリストにも無い名前の扱い (既定 serial)")
    parser.add_argument("--list", action="store_true", help="分類を表示して終了する")
    args = parser.parse_args(argv)

    if args.list:
        paths = sorted(glob.glob(os.path.join(args.tests_dir, "test_*.py")))
        parallel, serial, unclassified = classify(paths, args.serial or [], args.lane_default)
        print(f"並列 {len(parallel)} 本 / 直列 {len(serial)} 本 / 未宣言 {len(unclassified)} 本")
        for path in serial:
            name = os.path.basename(path)
            print(f"  [直列] {name}: {SERIAL_TESTS.get(name, '(未宣言)')}")
        if unclassified:
            print("  未宣言: " + ", ".join(sorted(unclassified)))
        return 0

    return sweep(args.tests_dir, args.jobs, args.timeout, args.serial or [],
                 args.lane_default)


if __name__ == "__main__":
    sys.exit(main())
