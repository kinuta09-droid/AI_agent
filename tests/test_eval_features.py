# -*- coding: utf-8 -*-
"""scripts/eval_features.py (特徴量スクリーニング) の回帰テスト

このテストが存在する理由:
  このハーネスは「特徴量に予測力が無い」と結論を出すために使う。
  結線がずれていると、本当は予測力のある特徴量まで一律にゼロと出て、
  しかも**それらしい表が出るので誰も気付けない** (Known Bug Patterns A の無言の死)。
  なので合成データで「出るべきときに出る」「出ないべきときに出ない」の
  両方向を固定する。

  [1] 既知相関を注入 → 検出でき、合格基準を通る
  [2] 純ノイズ → 検出せず、合格基準で落ちる
  [3] 前方リターンそのものを特徴量にする → IC ≈ 1.0 (結線確認)
  [4] ホールドアウトが実際に切られている
  [5] 二値特徴量で spread が潰れない (sweep 用の分岐)

実行: py tests/test_eval_features.py
課金: ゼロ (ネットワーク・実データを一切使わない)
"""
import os
import sys
import datetime

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(_PROJECT_ROOT, "scripts"))
import eval_features as ef

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{(' — ' + detail) if detail else ''}")


def _panel(kind: str, n_sym: int = 20, n: int = 2000, seed: int = 42) -> pd.DataFrame:
    """合成パネル。fwd_* を先に作り、そこから特徴量 x を逆算する。

    kind:
      "signal"   x = fwd + ノイズ (実務的に見つかってほしい強さ)
      "noise"    x は fwd と無関係
      "leak"     x = fwd そのもの (結線確認用。IC は 1.0 になるはず)
      "binary"   x ∈ {0,1}。x=1 のとき fwd が高い (sweep 型の検査)
    """
    rng = np.random.default_rng(seed)
    t0 = datetime.datetime(2022, 7, 1)
    frames = []
    for s in range(n_sym):
        ts = [t0 + datetime.timedelta(hours=4 * i) for i in range(n)]
        fwd = rng.normal(0, 0.05, n)
        if kind == "signal":
            x = fwd + rng.normal(0, 0.05, n)
        elif kind == "leak":
            x = fwd.copy()
        elif kind == "binary":
            x = (rng.random(n) < 0.1).astype(float)
            fwd = fwd + x * 0.03
        else:
            x = rng.normal(0, 1, n)
        d = {"symbol": f"S{s}", "timestamp": ts, "x": x}
        for hname in ef.HORIZONS:
            d[f"fwd_{hname}"] = fwd
        frames.append(pd.DataFrame(d))
    return pd.concat(frames, ignore_index=True)


print("=" * 60)
print("[1] 既知相関を注入 → 検出でき、合格基準を通る")
r = ef.evaluate(_panel("signal"), "x", "5d")
check("[1a] IC が基準を超える", r["ic_med"] >= ef.GATE_IC, f"IC={r['ic_med']:.3f}")
check("[1b] 20銘柄すべてで符号一致", r["sign_hits"] == 20,
      f"{r['sign_hits']}/{r['n_sym']}")
check("[1c] spread がコスト2倍を超える", abs(r["spread_pct"]) >= ef.GATE_SPREAD_PCT,
      f"{r['spread_pct']:.2f}%")
check("[1d] gate を通る", ef.gate(r, [1, 1, 1], null_p95=0.02) == [],
      str(ef.gate(r, [1, 1, 1], null_p95=0.02)))

print("\n[2] 純ノイズ → 検出せず、合格基準で落ちる")
r = ef.evaluate(_panel("noise"), "x", "5d")
check("[2a] IC がほぼゼロ", abs(r["ic_med"]) < ef.GATE_IC, f"IC={r['ic_med']:.4f}")
check("[2b] 符号一致が偶然の範囲", r["sign_hits"] < ef.GATE_SIGN,
      f"{r['sign_hits']}/{r['n_sym']}")
check("[2c] gate で落ちる", ef.gate(r, [1, 1, 1], null_p95=0.02) != [])

print("\n[3] 結線確認: 特徴量 = 前方リターンそのもの → IC ≈ 1.0")
r = ef.evaluate(_panel("leak"), "x", "5d")
check("[3a] IC が 1.0", r["ic_med"] > 0.999, f"IC={r['ic_med']:.6f}")
check("[3b] spread が正で大きい", r["spread_pct"] > 5, f"{r['spread_pct']:.2f}%")

print("\n[4] ホールドアウトが実際に切られている")
n = 500
ts = [ef.HOLDOUT_START - datetime.timedelta(hours=4 * (n - i)) for i in range(n)]
ts += [ef.HOLDOUT_START + datetime.timedelta(hours=4 * i) for i in range(n)]
cut = ef._cut_holdout(pd.DataFrame({"timestamp": ts, "v": range(2 * n)}))
check("[4a] HOLDOUT_START 以降の行が残らない",
      len(cut) > 0 and cut["timestamp"].max() < ef.HOLDOUT_START,
      f"max={cut['timestamp'].max()}")
check("[4b] 手前の行は落とさない", len(cut) == n, f"{len(cut)}行")
check("[4c] load_panel は必ず _cut_holdout を通す",
      "_cut_holdout(panel)" in open(
          os.path.join(_PROJECT_ROOT, "scripts", "eval_features.py"),
          encoding="utf-8").read())

print("\n[5] 二値特徴量でバケットが潰れない (sweep 型)")
r = ef.evaluate(_panel("binary"), "x", "5d")
check("[5a] spread が NaN でも 0 でもない",
      not np.isnan(r["spread_pct"]) and abs(r["spread_pct"]) > 1,
      f"{r['spread_pct']:.2f}%")
check("[5b] 20銘柄すべてで符号一致", r["sign_hits"] == 20,
      f"{r['sign_hits']}/{r['n_sym']}")

print("\n[6] 帰無最大分布が「多重検定の分だけ」大きくなる")
p = _panel("noise", n_sym=8, n=1200)
n1 = ef.null_max_ic(p, n_tests=1, iters=40, seed=1)
n36 = ef.null_max_ic(p, n_tests=36, iters=40, seed=1)
check("[6a] 36検定の帰無最大 > 1検定の帰無最大", n36["p95"] > n1["p95"],
      f"1件={n1['p95']:.4f} / 36件={n36['p95']:.4f}")
check("[6b] 帰無最大が0でない (分布が退化していない)", n36["p95"] > 0)

print("\n[7] build_features に先読みが無い (末尾 h 本の fwd は NaN)")
rng = np.random.default_rng(0)
m = 400
raw = pd.DataFrame({
    "timestamp": [datetime.datetime(2023, 1, 1) + datetime.timedelta(hours=4 * i)
                  for i in range(m)],
    "close": 100 + np.cumsum(rng.normal(0, 1, m)),
})
raw["open"] = raw["close"]
raw["high"] = raw["close"] + 1
raw["low"] = raw["close"] - 1
raw["volume"] = rng.random(m) * 1000
f = ef.build_features(raw, {})
for hname, h in ef.HORIZONS.items():
    check(f"[7-{hname}] 末尾{h}本の fwd_{hname} が NaN",
          f[f"fwd_{hname}"].tail(h).isna().all())
check("[7z] 全特徴量が列として存在する",
      all(c in f.columns for c in ef.FEATURES),
      str([c for c in ef.FEATURES if c not in f.columns]))

print("\n" + "=" * 60)
print(f"PASS: {len(_PASS)} / FAIL: {len(_FAIL)}")
for x in _FAIL:
    print(f"  ❌ {x}")
sys.exit(1 if _FAIL else 0)
