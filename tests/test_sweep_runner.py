# -*- coding: utf-8 -*-
"""`scripts/run_test_sweep.py` (並列スイープランナー) の回帰テスト

このテストが存在する理由 (2026-07-31):
  スイープを並列化すると、**判定そのものを壊しても速くなる**。
  「速くなった」は「まだ正しく判定している」の証拠にならないので、
  次の2方向を縛る。

    - 分類が網羅されているか (静的): tests/test_*.py が
      SERIAL_TESTS / PARALLEL_TESTS の**ちょうど一方**に載っていること。
      「判断されていない」状態を作らせない (tests/test_tool_registry.py と同じ作法)。
      片方に載せ忘れた新しいテストは直列レーンへ落ちるだけで実害は無いが、
      *誤って並列に入れた*ときはデータ競合が黙って起きるので、
      分類漏れは必ず人間の目に触れさせる。
    - 判定が終了コードまで配線されているか (動的): 一時ディレクトリに
      合格/失敗/遅い のダミーを置いて実際にランナーを走らせ、
      失敗が FAIL として名指しされ、全体が exit 1 になることを確認する。
      あわせて **直列レーンが並列レーンと時間的に重なっていない**ことを
      実測の区間で確認する (重なったら隔離は名前だけの飾りになる)。
    - `--filter` がファイル名の部分文字列一致で対象を絞り込み、一致が
      無いときは (`--list` 経由でも) 無言で全件/0件扱いに倒さず exit 1
      で明示的に知らせるか (2026-08-03)。

実行: py tests/test_sweep_runner.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts import run_test_sweep as rts  # noqa: E402

RUNNER = os.path.join(_PROJECT_ROOT, "scripts", "run_test_sweep.py")
LAUNCHER = sys.executable

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
# [1] 分類の網羅 (静的)
# ---------------------------------------------------------------------------
def test_every_test_file_is_classified():
    print("\n[1] 並列可否がすべてのテストについて宣言されているか")
    import glob

    names = {os.path.basename(p)
             for p in glob.glob(os.path.join(_PROJECT_ROOT, "tests", "test_*.py"))}

    both = sorted(set(rts.SERIAL_TESTS) & set(rts.PARALLEL_TESTS))
    check("両方のリストに載っている名前は無い", not both, f"{both}")

    missing = sorted(names - set(rts.SERIAL_TESTS) - set(rts.PARALLEL_TESTS))
    check(f"未分類が0件 ({len(names)} 本を走査)", not missing,
          "SERIAL_TESTS か PARALLEL_TESTS に分類すること: " + ", ".join(missing))

    stale = sorted((set(rts.SERIAL_TESTS) | set(rts.PARALLEL_TESTS)) - names)
    check("実在しない名前がリストに残っていない", not stale, f"{stale}")

    empty = sorted(n for n, why in rts.SERIAL_TESTS.items() if not why.strip())
    check("直列レーンの各件に理由が書かれている", not empty, f"{empty}")


# ---------------------------------------------------------------------------
# ダミーのテスト群を置いた一時ディレクトリでランナーを実走させる
# ---------------------------------------------------------------------------
_PROBE = """# -*- coding: utf-8 -*-
import json, os, sys, time
_dir = os.environ["SWEEP_PROBE_LOG"]
_start = time.time()
time.sleep({sleep})
_rec = os.path.join(_dir, {name!r} + ".interval.json")
with open(_rec, "w", encoding="utf-8") as f:
    json.dump({{"name": {name!r}, "start": _start, "end": time.time()}}, f)
print({name!r} + " done")
sys.exit({code})
"""


def _make_probes(tmp, specs):
    """specs = [(name, sleep, exit_code), ...] を tmp に書き出す。"""
    for name, sleep, code in specs:
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(_PROBE.format(sleep=sleep, name=name, code=code))


def _run_runner(tmp, log, *args, timeout=300):
    # log はプローブごとの区間記録を書き込むディレクトリ。1プローブ=1ファイルにして
    # 複数プロセスが同じファイルへ同時追記する競合を構造的に無くす — Windows の CRT は
    # O_APPEND を「毎回 EOF へ seek してから write」でエミュレートしており POSIX と違って
    # プロセス間で原子的ではないため、複数プローブがほぼ同時に終わる並列レーンでは
    # 後勝ちで1行がそっくり消えることがある (2026-08-03: CI で 6本中5本しか記録されず
    # flaky FAIL、ローカルでは常に緑という形で顕在化)。親プロセス側で先に作っておけば
    # 子プロセス側での mkdir 競合も無い。
    os.makedirs(log, exist_ok=True)
    env = {**os.environ, "SWEEP_PROBE_LOG": log, "PYTHONIOENCODING": "utf-8"}
    started = time.monotonic()
    proc = subprocess.run(
        [LAUNCHER, RUNNER, "--tests-dir", tmp, *args],
        cwd=_PROJECT_ROOT, capture_output=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), time.monotonic() - started


def _intervals(log):
    out = {}
    if not os.path.isdir(log):
        return out
    for fn in os.listdir(log):
        if fn.endswith(".interval.json"):
            with open(os.path.join(log, fn), encoding="utf-8") as f:
                rec = json.load(f)
            out[rec["name"]] = (rec["start"], rec["end"])
    return out


def test_failure_reaches_the_exit_code_and_lanes_do_not_overlap():
    """並列で速くなっても、判定と隔離が生きていること。

    1回の実走で3つ見る (ランナーの起動固定費を3回払う理由が無い):
      - 失敗した1本が名指しで FAIL になり、全体が exit 1 になる
      - 並列レーンが本当に並列 (wall が所要時間の総和より明確に小さい)
      - 直列レーンの区間が、どの並列レーンの区間とも重ならない
    """
    print("\n[2] 失敗が終了コードに届くか / レーンが重なっていないか")
    tmp = tempfile.mkdtemp(prefix="sweep_probe_")
    log = os.path.join(tmp, "intervals")
    try:
        _make_probes(tmp, [
            ("test_probe_p1.py", 2.0, 0),
            ("test_probe_p2.py", 2.0, 0),
            ("test_probe_p3.py", 2.0, 0),
            ("test_probe_p4.py", 2.0, 0),
            ("test_probe_bad.py", 0.2, 1),
            ("test_probe_serial.py", 1.0, 0),
        ])
        code, out, wall = _run_runner(
            tmp, log, "--jobs", "4", "--lane-default", "parallel",
            "--serial", "test_probe_serial.py",
        )

        check("失敗が1本あると exit 1", code == 1, f"exit={code}\n{out[-800:]}")
        check("失敗したファイルが名指しされる", "test_probe_bad.py" in out.split("Failed files:")[-1],
              out[-800:])
        check("成功した5本は Failed files に出ない",
              "test_probe_p1.py" not in out.split("Failed files:")[-1], out[-800:])

        iv = _intervals(log)
        check("6本すべての区間が記録された", len(iv) == 6, f"{sorted(iv)}")

        # 「並列になっているか」を **wall clock のしきい値で見ない**。
        # 初版は `wall < 7.0` だった (逐次なら 9.2秒 / 理想の並列なら 4秒) が、
        # CI runner ではプロセス起動の固定費だけで 3秒近く乗るため 7.0 に届き、
        # **同じコミットが緑と赤を行き来する** 状態になっていた
        # (2026-07-31: master の push は赤・PR の再実行は緑)。
        # 「開発機の速さ」を暗黙の前提にした判定はこのリポジトリが繰り返し
        # 潰してきた下駄そのもの。区間ログを既に持っているので、
        # **実際に重なった本数 (peak concurrency)** で判定する。
        # 逐次に戻れば peak は必ず 1 になるので、しきい値 >= 2 で厳密に捕まる。
        par = [iv[n] for n in ("test_probe_p1.py", "test_probe_p2.py",
                               "test_probe_p3.py", "test_probe_p4.py") if n in iv]
        events = sorted([(a, 1) for a, _ in par] + [(b, -1) for _, b in par])
        peak, cur = 0, 0
        for _t, delta in events:
            cur += delta
            peak = max(peak, cur)
        check(f"並列レーンが実際に並列 (同時実行 peak={peak} / wall {wall:.1f}s)",
              peak >= 2,
              f"peak={peak} — 逐次実行に戻っている (並列なら必ず 2 以上になる)")
        s = iv.get("test_probe_serial.py")
        overlaps = [n for n, (a, b) in iv.items()
                    if s and n != "test_probe_serial.py" and a < s[1] and s[0] < b]
        check("直列レーンがどの並列テストとも重ならない", not overlaps,
              f"重なった相手: {overlaps}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_all_green_exits_zero():
    """逆方向。「常に赤いだけ」を防ぐ。"""
    print("\n[3] 全部緑なら exit 0")
    tmp = tempfile.mkdtemp(prefix="sweep_probe_")
    log = os.path.join(tmp, "intervals")
    try:
        _make_probes(tmp, [("test_probe_ok1.py", 0.1, 0), ("test_probe_ok2.py", 0.1, 0)])
        code, out, _ = _run_runner(tmp, log, "--jobs", "2", "--lane-default", "parallel")
        check("exit 0 になる", code == 0, f"exit={code}\n{out[-800:]}")
        check("PASS 2 / FAIL 0 と報告する", "PASS: 2  FAIL: 0" in out, out[-500:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_filter_selects_a_subset_by_substring():
    """`--filter` がファイル名の部分文字列一致で対象を絞り込むか。

    ジャンルごとの分類リストを別途持たず、既存のファイル名だけで絞れるようにした
    (2026-08-03)。「一致するものが無い」も無言で全件実行に倒さず、明示的に
    exit 1 で知らせることを合わせて確認する。
    """
    print("\n[5] --filter が部分文字列でファイルを絞り込むか")
    tmp = tempfile.mkdtemp(prefix="sweep_probe_")
    log = os.path.join(tmp, "intervals")
    try:
        _make_probes(tmp, [
            ("test_probe_vault_a.py", 0.1, 0),
            ("test_probe_vault_b.py", 0.1, 0),
            ("test_probe_crypto_a.py", 0.1, 0),
        ])
        code, out, _ = _run_runner(
            tmp, log, "--jobs", "4", "--lane-default", "parallel", "--filter", "vault")
        check("絞り込んだ2本だけが対象になる", "対象 2 本" in out, out[:300])
        check("絞り込み対象外は走らない",
              "test_probe_crypto_a.py" not in out, out[:500])
        check("exit 0 になる", code == 0, f"exit={code}\n{out[-500:]}")

        code2, out2, _ = _run_runner(
            tmp, log, "--jobs", "4", "--lane-default", "parallel", "--filter", "no_such_genre")
        check("一致が無いときは exit 1 で明示的に知らせる", code2 == 1, f"exit={code2}\n{out2}")

        code3, out3, _ = _run_runner(tmp, log, "--list", "--filter", "no_such_genre")
        check("--list 経由でも一致無しは exit 1 になる (0/0/0 の無言成功に倒さない)",
              code3 == 1, f"exit={code3}\n{out3}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hung_test_is_failed_not_ignored():
    """止まったテストを「時間切れだから無かったこと」にしない。

    タイムアウトを握りつぶすと、ハングしたテストがスイープを緑のまま素通りする
    (このリポジトリで繰り返し出ている「表示と実態の食い違い」)。
    """
    print("\n[4] 時間内に終わらなかった1本を FAIL にするか")
    tmp = tempfile.mkdtemp(prefix="sweep_probe_")
    log = os.path.join(tmp, "intervals")
    try:
        _make_probes(tmp, [("test_probe_hang.py", 30, 0), ("test_probe_fast.py", 0.1, 0)])
        code, out, wall = _run_runner(
            tmp, log, "--jobs", "2", "--lane-default", "parallel", "--timeout", "3")
        check("exit 1 になる", code == 1, f"exit={code}\n{out[-800:]}")
        check("ハングした本人が名指しされる",
              "test_probe_hang.py" in out.split("Failed files:")[-1], out[-800:])
        check("打ち切ったことが出力に残る", "タイムアウト" in out, out[-800:])
        check(f"打ち切りが効いている (wall {wall:.1f}s < 30s)", wall < 25, f"{wall:.1f}s")
        check("巻き添えにしない (同時に走った1本は緑)",
              "test_probe_fast.py" not in out.split("Failed files:")[-1], out[-800:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_every_test_file_is_classified()
    test_failure_reaches_the_exit_code_and_lanes_do_not_overlap()
    test_all_green_exits_zero()
    test_filter_selects_a_subset_by_substring()
    test_hung_test_is_failed_not_ignored()

    print("\n" + "=" * 55)
    print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
    for f in _FAIL:
        print(f"  - {f}")
    sys.exit(1 if _FAIL else 0)
