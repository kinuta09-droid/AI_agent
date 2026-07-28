# -*- coding: utf-8 -*-
"""
Phase C0: lib/concurrency のユニットテスト
(設定の読み取り / Ollama 同時実行上限 / observe のログ書き込み直列化)

実行: py tests/test_concurrency.py
"""
import os
import sys
import json
import time
import threading

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.concurrency as conc


class _EnvPatch:
    """os.environ を一時的に差し替える。テスト終了時に必ず元へ戻す。

    CLAUDE.md のスコープ階層では LEVEL 2 (プロセス環境の書き換え) は禁止だが、
    ここはテストプロセス内で完結し、子プロセスを起こさないので許容範囲。
    本体コードでは絶対にやらないこと。
    """

    def __init__(self, **kv):
        self.kv = kv
        self.saved = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        conc._reset_for_tests()
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        conc._reset_for_tests()
        return False


# ---------------------------------------------------------
# [1] 設定の読み取り
# ---------------------------------------------------------

def test_env_defaults_and_garbage():
    with _EnvPatch(AGENT_FANOUT_WORKERS=None, AGENT_PARALLEL=None):
        assert conc.parallel_enabled() is True, "未設定なら fan-out は有効"
        assert conc.fanout_workers() == conc.DEFAULT_FANOUT_WORKERS

    with _EnvPatch(AGENT_FANOUT_WORKERS="abc"):
        assert conc.fanout_workers() == conc.DEFAULT_FANOUT_WORKERS, "パース不能→既定"

    with _EnvPatch(AGENT_FANOUT_WORKERS="  "):
        assert conc.fanout_workers() == conc.DEFAULT_FANOUT_WORKERS, "空白→既定"

    with _EnvPatch(AGENT_FANOUT_WORKERS="0"):
        assert conc.fanout_workers() == 1, "0 は『無制限』ではなく下限1に丸める"

    with _EnvPatch(AGENT_FANOUT_WORKERS="-5"):
        assert conc.fanout_workers() == 1, "負値も下限1"

    with _EnvPatch(AGENT_FANOUT_WORKERS="4"):
        assert conc.fanout_workers() == 4
    print("✅ [1] env: 既定/不正値/下限")


def test_kill_switch_vocabulary():
    for raw in ("0", "false", "FALSE", "no", "off", " Off "):
        with _EnvPatch(AGENT_PARALLEL=raw):
            assert conc.parallel_enabled() is False, f"{raw!r} は OFF のはず"
    for raw in ("1", "true", "yes", "on"):
        with _EnvPatch(AGENT_PARALLEL=raw):
            assert conc.parallel_enabled() is True, f"{raw!r} は ON のはず"
    print("✅ [2] AGENT_PARALLEL のキルスイッチ語彙")


def test_settings_read_at_call_time():
    """import 時ではなく呼び出し時に読むこと (.env 差し替え → 再起動で効かせる)。"""
    with _EnvPatch(AGENT_FANOUT_WORKERS="2"):
        assert conc.fanout_workers() == 2
        os.environ["AGENT_FANOUT_WORKERS"] = "5"
        assert conc.fanout_workers() == 5, "呼び出しの度に読み直すこと"
    print("✅ [3] 設定は呼び出し時に読む")


# ---------------------------------------------------------
# [4] Ollama 同時実行上限
# ---------------------------------------------------------

def _measure_max_concurrency(n_threads: int, hold: float = 0.05) -> int:
    """ollama_slot() を n_threads から同時に取りに行き、観測された最大同時数を返す。"""
    lock = threading.Lock()
    live = 0
    peak = 0
    barrier = threading.Barrier(n_threads)

    def worker():
        nonlocal live, peak
        barrier.wait()
        with conc.ollama_slot():
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(hold)
            with lock:
                live -= 1

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return peak


def test_ollama_slot_enforces_limit():
    with _EnvPatch(OLLAMA_MAX_CONCURRENCY=None):
        peak = _measure_max_concurrency(8)
        assert peak == 1, f"既定は同時1本のはずだが {peak} 本同時に走った"

    with _EnvPatch(OLLAMA_MAX_CONCURRENCY="3"):
        peak = _measure_max_concurrency(8)
        assert peak <= 3, f"上限3を超えた: {peak}"
        assert peak >= 2, f"上限3なのに直列化されている: {peak} (セマフォが常に1?)"
    print("✅ [4] ollama_slot が同時実行数を上限で抑える")


def test_ollama_slot_releases_on_exception():
    """例外が出てもスロットを返すこと。返し損ねると2回目以降が永久に詰まる。"""
    with _EnvPatch(OLLAMA_MAX_CONCURRENCY="1"):
        for _ in range(3):
            try:
                with conc.ollama_slot():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        # ここで詰まっていれば join がタイムアウトする
        done = threading.Event()

        def worker():
            with conc.ollama_slot():
                done.set()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=3)
        assert done.is_set(), "例外経路でスロットが解放されていない (デッドロック)"
    print("✅ [5] 例外が出てもスロットを解放する")


def test_both_ollama_call_sites_take_the_slot():
    """埋め込みとリランクの**両方**が ollama_slot を通ること。

    片方だけ囲うのは囲っていないのと大差ない: 奪い合っているのはこの2モデルなので、
    どちらかが素通りすれば VRAM の追い出し合いはそのまま起きる。
    ast で実際の呼び出し位置を見る (import しただけで満足しない)。
    """
    import ast

    targets = {
        os.path.join(_PROJECT_ROOT, "tools", "tool_vault_index.py"): "_call_ollama_embed",
        os.path.join(_PROJECT_ROOT, "tools", "tool_vault_hybrid.py"): "_call_ollama_llm",
    }
    for path, func_name in targets.items():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == func_name)
        # requests.post を含む with 文が ollama_slot() を掴んでいること
        guarded = False
        for node in ast.walk(func):
            if not isinstance(node, ast.With):
                continue
            uses_slot = any(
                isinstance(i.context_expr, ast.Call)
                and isinstance(i.context_expr.func, ast.Name)
                and i.context_expr.func.id == "ollama_slot"
                for i in node.items
            )
            if not uses_slot:
                continue
            posts = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "post"]
            if posts:
                guarded = True
        assert guarded, f"{func_name} の requests.post が ollama_slot() で囲われていない"
    print("✅ [6] 埋め込み/リランクの両方が ollama_slot を通る")


# ---------------------------------------------------------
# [7] observe() のログ書き込み
# ---------------------------------------------------------

def test_observe_log_lines_not_interleaved(tmp_dir=None):
    """並列 observe() で JSONL の行が壊れないこと。"""
    import tools.tool_skill_observer as observer

    original = observer.LOG_PATH
    tmp_path = os.path.join(_PROJECT_ROOT, "logs", "_test_skill_observations.jsonl")
    observer.LOG_PATH = tmp_path
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    try:
        N = 24

        def tool(payload=""):
            time.sleep(0.005)
            return "ok"

        barrier = threading.Barrier(N)

        def worker(i):
            barrier.wait()
            observer.observe("tool_dummy", tool, "test", payload="x" * 200 + str(i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(tmp_path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        assert len(lines) == N, f"{N} 行のはずが {len(lines)} 行"
        for ln in lines:
            json.loads(ln)  # 行が混ざっていればここで落ちる
    finally:
        observer.LOG_PATH = original
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    print("✅ [7] 並列 observe でも JSONL が壊れない")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
