# -*- coding: utf-8 -*-
"""
Feature Screening — 売買ルールを書く「前」に、特徴量単体の予測力を測る

なぜこれが要るか (2026-08-17):
  暗号資産系ジョブ11件を停止した根拠 (agent_scheduler.py の SCHEDULE 内コメント) は
  「4年通算 -0.00R、ランダムエントリーとの差は ±2pt で符号が反転」だった。
  真の失敗は数字そのものではなく手順 — ルールを先に書き、後からバックテストで
  良い数字を探した。11通り試して最良を選ぶと偶然だけで +0.13R かさ上げされ、
  計測値 +0.042R を上回っていた。

  そこでこのスクリプトは「複合ルールが儲かるか」を測らない。
  **個々の特徴量が前方リターンとどれだけ関係しているか**だけを測る。
  手仕舞い設計 (SL/TP) を一切混ぜないのが要点 — 前回はその幾何が勝率33.3%を
  決めていたのに、戦略の成果と取り違えた。

同じ間違いを構造的にできなくするための仕掛け:
  1. 合格基準をコード上部の定数で先に宣言する (事後に緩めない)
  2. 36件の結果を必ず全部出す (合格分の抜粋にしない)
  3. 帰無「最大値」分布を実測して並べる = 多重検定の補正を目に入る位置に置く
  4. 3期間で符号が揃うかだけを見る (最良期間を選ばない)
  5. 直近6ヶ月は読み込みすらしない (HOLDOUT_START)

実行: py scripts/eval_features.py --label v1
出力: logs/feature_eval/fe_YYYYMMDD_HHMM_<label>_report.md / _raw.csv
課金: ゼロ (Binance 公開API + ローカルキャッシュのみ)
"""
import os
import sys
import time
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

import numpy as np
import pandas as pd
import pandas_ta as ta_lib
from scipy.stats import spearmanr

from lib import bt_data
from lib.paths import LOGS_DIR

OUT_DIR = os.path.join(LOGS_DIR, "feature_eval")

# tools/tool_crypto.SCAN_SYMBOLS と同一。直接 import しないのは tool_crypto が
# lib_shared 経由で .env を必須にするため (backtest_crypto.DEFAULT_SYMBOLS と同じ前例)。
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "POL/USDT",
    "ATOM/USDT", "UNI/USDT", "LTC/USDT", "NEAR/USDT", "APT/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "FIL/USDT", "INJ/USDT",
]

# ---------------------------------------------------------------------------
# ホールドアウト: この日時以降のバーは読み込みすらしない。
# この定数を緩めてよいのは「ルールを確定させた後に1度だけ」。
# スクリーニング中に動かした時点で、アウトオブサンプル検証は失われる。
# ---------------------------------------------------------------------------
HOLDOUT_START = datetime.datetime(2026, 2, 17)

# 4h足での本数。126本 = 21日 = 旧 backtest の expiry_days に対応
HORIZONS = {"1d": 6, "5d": 30, "21d": 126}

# 「最良を選ぶため」ではなく「符号が揃うかを見るため」の分割
PERIODS = [
    ("2022H2-2023", datetime.datetime(2022, 7, 1), datetime.datetime(2024, 1, 1)),
    ("2024", datetime.datetime(2024, 1, 1), datetime.datetime(2025, 1, 1)),
    ("2025-2026", datetime.datetime(2025, 1, 1), HOLDOUT_START),
]

FEATURES = [
    "rsi14", "adx14", "di_diff", "atr_pct", "ema200_dist", "ema50_dist",
    "mom_24h", "mom_72h", "vol_z20", "range_pos20", "sweep", "fng",
]

# --- 合格基準 (実行前に確定。結果を見てから緩めない) ----------------------
ROUND_TRIP_COST_PCT = 0.4   # 往復コスト前提 (取引所手数料 + スリッページ)
GATE_IC = 0.03              # |IC中央値|
GATE_SIGN = 15              # 何銘柄で符号が揃えば可とするか (/有効銘柄数)
GATE_SPREAD_PCT = ROUND_TRIP_COST_PCT * 2   # top-bottom spread がコストの2倍以上
MIN_SAMPLES = 200           # この本数未満の銘柄はその期間の集計から外す
NULL_ITERS = 200            # 帰無分布の反復回数


# ---------------------------------------------------------------------------
# 特徴量の構築 (全ベクトル化・時点 t で確定済みの情報のみ)
# ---------------------------------------------------------------------------

def build_features(df: pd.DataFrame, fng_history: dict) -> pd.DataFrame:
    """4h足 OHLCV → 特徴量 + 前方リターンの DataFrame。

    df: bt_data.get_ohlcv() の戻り (timestamp/open/high/low/close/volume, 昇順)
    先読み禁止: すべての特徴量はバー t の終値時点で確定している値だけを使う。
    """
    close, high, low, vol = df['close'], df['high'], df['low'], df['volume']
    adx = ta_lib.adx(high, low, close, length=14)
    atr = ta_lib.atr(high, low, close, length=14)

    out = pd.DataFrame({'timestamp': df['timestamp'].values})
    out['rsi14'] = ta_lib.rsi(close, length=14).values
    out['adx14'] = adx['ADX_14'].values
    out['di_diff'] = (adx['DMP_14'] - adx['DMN_14']).values
    out['atr_pct'] = (atr / close * 100).values
    out['ema200_dist'] = (close / ta_lib.ema(close, length=200) - 1).values
    out['ema50_dist'] = (close / ta_lib.ema(close, length=50) - 1).values
    out['mom_24h'] = close.pct_change(6).values
    out['mom_72h'] = close.pct_change(18).values

    vol_sd = vol.rolling(20).std()
    out['vol_z20'] = ((vol - vol.rolling(20).mean()) / vol_sd).where(vol_sd > 0).values

    roll_min, roll_max = low.rolling(20).min(), high.rolling(20).max()
    rng = roll_max - roll_min
    out['range_pos20'] = ((close - roll_min) / rng).where(rng > 0).values

    # 旧 _detect_liquidity_sweep のベクトル版: 直近20本の安値をヒゲで割り込み終値で戻す
    prev_min = low.shift(1).rolling(20).min()
    out['sweep'] = ((low < prev_min) & (close > prev_min)).astype(float).values

    # F&G は「前日」UTC の公表値。当日値はその日のうちに公表される = 先読みになる
    # (旧 backtest_crypto.run_symbol はバー開始日の当日値を使っていた)
    prev_day = (df['timestamp'] - pd.Timedelta(days=1)).dt.strftime('%Y-%m-%d')
    out['fng'] = prev_day.map(fng_history).astype(float).values

    for name, h in HORIZONS.items():
        out[f'fwd_{name}'] = (close.shift(-h) / close - 1).values

    return out.replace([np.inf, -np.inf], np.nan)


def _cut_holdout(df: pd.DataFrame) -> pd.DataFrame:
    """HOLDOUT_START 以降のバーを落とす。load_panel から必ず通す唯一の関門。"""
    return df[df['timestamp'] < HOLDOUT_START].reset_index(drop=True)


def load_panel(symbols: list, days: int = 1700) -> pd.DataFrame:
    """全銘柄の特徴量パネル (symbol 列付きの縦持ち) を作る。"""
    fng = bt_data.get_fng_history()
    since_ms = int((datetime.datetime.now()
                    - datetime.timedelta(days=days)).timestamp() * 1000)
    frames = []
    for sym in symbols:
        raw = bt_data.get_ohlcv(sym, "4h", since_ms)
        if len(raw) < MIN_SAMPLES:
            print(f"  ⚠️ {sym}: データ不足 ({len(raw)}本) — 除外")
            continue
        f = build_features(raw, fng)
        f.insert(0, 'symbol', sym)
        frames.append(f)
        print(f"  ✅ {sym}: {len(f)}本")
    panel = pd.concat(frames, ignore_index=True)
    return _cut_holdout(panel)


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------

def _bucket_spread(x: np.ndarray, y: np.ndarray) -> float:
    """上位20%バケット − 下位20%バケットの平均前方リターン (%)。

    往復コスト (ROUND_TRIP_COST_PCT) と直接比較できる単位にするのが目的。
    二値/離散でバケットが潰れる場合 (sweep 等) は「閾値超え vs それ以外」で分ける。
    """
    q20, q80 = np.quantile(x, [0.2, 0.8])
    if q20 == q80:
        top, bot = y[x > q20], y[x <= q20]
    else:
        top, bot = y[x >= q80], y[x <= q20]
    if len(top) == 0 or len(bot) == 0:
        return float('nan')
    return (top.mean() - bot.mean()) * 100


def evaluate(panel: pd.DataFrame, feat: str, hname: str,
             lo=None, hi=None) -> dict:
    """(特徴量, ホライズン) 1組の予測力を測る。

    銘柄ごとに Spearman IC を出してから中央値を取る (プールしない)。
    同時刻の銘柄間相関 — みんな BTC と一緒に動く — でサンプル数を水増ししないため。
    """
    h = HORIZONS[hname]
    fwd = f'fwd_{hname}'
    sub = panel
    if lo is not None:
        sub = sub[(sub['timestamp'] >= lo) & (sub['timestamp'] < hi)]

    ics, spreads, bases, ns = [], [], [], []
    off_ics = []   # 位相オフセット別 (重複窓を除いた部分標本) の IC
    step = max(1, h // 30)   # オフセット数の上限を30に抑える

    for _, g in sub.groupby('symbol', sort=True):
        g = g[[feat, fwd]].dropna()
        if len(g) < MIN_SAMPLES:
            continue
        x, y = g[feat].to_numpy(float), g[fwd].to_numpy(float)
        if np.ptp(x) == 0:
            continue   # 期間中ずっと同じ値 (sweep が0本など) は測れない
        ic = spearmanr(x, y).statistic
        if np.isnan(ic):
            continue
        ics.append(ic)
        ns.append(len(g))
        spreads.append(_bucket_spread(x, y))
        bases.append(y.mean() * 100)

        # 位相オフセット: x[off::h] は互いに素な部分標本で、内部の前方リターンが
        # 重複しない。h本おきに取ると隣接バーの 125/126 重複が消える。
        per_sym = []
        for off in range(0, h, step):
            xs, ys = x[off::h], y[off::h]
            if len(xs) < 30 or np.ptp(xs) == 0:
                continue
            v = spearmanr(xs, ys).statistic
            if not np.isnan(v):
                per_sym.append(v)
        if per_sym:
            off_ics.append(np.mean(per_sym))

    if not ics:
        return {"feature": feat, "horizon": hname, "n_sym": 0, "ic_med": float('nan'),
                "off_sd": float('nan'), "sign_hits": 0, "spread_pct": float('nan'),
                "base_pct": float('nan'), "n_bars": 0}

    ic_med = float(np.median(ics))
    return {
        "feature": feat,
        "horizon": hname,
        "n_sym": len(ics),
        "ic_med": ic_med,
        # 位相をずらしたときのブレ。|ic_med| がこれを下回るなら、その数字は
        # 重複窓が作った自分自身のノイズの中にいる
        "off_sd": float(np.std(off_ics)) if len(off_ics) > 1 else float('nan'),
        "sign_hits": int(sum(np.sign(v) == np.sign(ic_med) for v in ics)),
        "spread_pct": float(np.nanmedian(spreads)),
        "base_pct": float(np.nanmedian(bases)),
        "n_bars": int(np.median(ns)),
    }


# ---------------------------------------------------------------------------
# 帰無分布 (多重検定の補正)
# ---------------------------------------------------------------------------

def _block_perm(n: int, block: int, rng) -> np.ndarray:
    """長さ block の連続ブロック単位で並べ替えたインデックス。

    一様シャッフルだと特徴量の自己相関が壊れ、帰無分布が楽観側にずれる。
    ブロック単位なら「局所的な滑らかさは本物と同程度、真の関係だけゼロ」に近づく。
    """
    blocks = [np.arange(i, min(i + block, n)) for i in range(0, n, block)]
    rng.shuffle(blocks)
    return np.concatenate(blocks)


def null_max_ic(panel: pd.DataFrame, n_tests: int, iters: int = NULL_ITERS,
                seed: int = 0) -> dict:
    """「n_tests 回の検定の最大 |IC中央値|」の帰無分布 → {p50, p95, p99}。

    実測値がこの95%点を超えられないなら、その特徴量は 36 回引いた籤の当たりと
    区別できない。前回「11通り試して最良を選ぶと偶然だけで +0.13R」の再発防止。

    帰無特徴量には fwd_ret の順位をブロック並べ替えしたものを使う
    (現実の特徴量と同程度の自己相関を持ち、真の関係だけがゼロの系列)。
    """
    rng = np.random.default_rng(seed)
    ranks = {}   # (symbol, hname) -> 前方リターンの順位 (float配列)
    for hname, h in HORIZONS.items():
        for sym, g in panel.groupby('symbol', sort=True):
            y = g[f'fwd_{hname}'].dropna().to_numpy(float)
            if len(y) >= MIN_SAMPLES:
                ranks.setdefault(hname, []).append((h, pd.Series(y).rank().to_numpy()))

    hnames = list(ranks.keys())
    maxima = []
    for _ in range(iters):
        stats = []
        while len(stats) < n_tests:
            for hname in hnames:
                per_sym = []
                for h, ry in ranks[hname]:
                    idx = _block_perm(len(ry), h, rng)
                    per_sym.append(np.corrcoef(ry, ry[idx])[0, 1])
                stats.append(abs(float(np.median(per_sym))))
                if len(stats) >= n_tests:
                    break
        maxima.append(max(stats))
    return {"p50": float(np.percentile(maxima, 50)),
            "p95": float(np.percentile(maxima, 95)),
            "p99": float(np.percentile(maxima, 99)),
            "iters": iters, "n_tests": n_tests}


# ---------------------------------------------------------------------------
# 合格判定
# ---------------------------------------------------------------------------

def gate(row: dict, period_signs: list, null_p95: float) -> list:
    """合格基準を満たさない理由のリスト (空なら合格)。

    基準は定数で先に宣言してある。結果を見てから緩めるとこの仕組みの意味が消える。
    """
    fails = []
    if not (abs(row["ic_med"]) >= GATE_IC):
        fails.append(f"IC {row['ic_med']:.3f} < {GATE_IC}")
    if row["sign_hits"] < min(GATE_SIGN, row["n_sym"]):
        fails.append(f"符号一致 {row['sign_hits']}/{row['n_sym']} < {GATE_SIGN}")
    valid = [s for s in period_signs if s != 0]
    if len(valid) < len(PERIODS) or len(set(valid)) != 1:
        fails.append("期間で符号が揃わない")
    if not (abs(row["spread_pct"]) >= GATE_SPREAD_PCT):
        fails.append(f"spread {row['spread_pct']:.2f}% < {GATE_SPREAD_PCT}%")
    if not (abs(row["ic_med"]) > null_p95):
        fails.append(f"帰無最大95%点 {null_p95:.3f} を超えない")
    return fails


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------

def _fmt(v, digits=3):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.{digits}f}"


def build_report(rows: list, null: dict, label: str, panel: pd.DataFrame,
                 corr_pairs: list) -> str:
    span = f"{panel['timestamp'].min():%Y-%m-%d} 〜 {panel['timestamp'].max():%Y-%m-%d}"
    passed = [r for r in rows if not r["fails"]]

    L = [f"# 特徴量スクリーニング — {label}", ""]
    L.append(f"- 生成: {datetime.datetime.now():%Y-%m-%d %H:%M}")
    L.append(f"- 銘柄: {panel['symbol'].nunique()} / データ範囲: {span}")
    L.append(f"- **ホールドアウト {HOLDOUT_START:%Y-%m-%d} 以降は読み込んでいない**")
    L.append(f"- 往復コスト前提: {ROUND_TRIP_COST_PCT}%")
    L.append("")
    L.append("測っているのは複合ルールの損益ではなく、**特徴量単体と前方リターンの関係**。")
    L.append("SL/TP のパス判定は一切使っていない (前回はその幾何が勝率を決めていた)。")
    L.append("")

    L.append("## 合格基準 (実行前に確定・事後変更禁止)")
    L.append(f"1. `|IC中央値| >= {GATE_IC}`")
    L.append(f"2. 符号一致 `>= {GATE_SIGN}` 銘柄")
    L.append(f"3. {len(PERIODS)}期間すべてで符号が同じ")
    L.append(f"4. `|spread| >= {GATE_SPREAD_PCT}%` (往復コスト{ROUND_TRIP_COST_PCT}%の2倍)")
    L.append("5. 帰無最大分布の95パーセンタイルを超える")
    L.append("")

    L.append("## 帰無分布 — 多重検定の補正")
    L.append(f"{null['n_tests']}検定・ブロックシャッフル{null['iters']}回で得た"
             f"**最大** `|IC中央値|` の分布:")
    L.append("")
    L.append(f"| p50 | **p95** | p99 |")
    L.append(f"|---|---|---|")
    L.append(f"| {null['p50']:.4f} | **{null['p95']:.4f}** | {null['p99']:.4f} |")
    L.append("")
    L.append(f"→ `|IC| <= {null['p95']:.4f}` は、{null['n_tests']}回引いた籤の"
             "当たりと区別できない。")
    L.append("")

    L.append(f"## 全{len(rows)}件 (合格分の抜粋ではない)")
    L.append("")
    L.append("| feature | h | 銘柄 | IC中央値 | 位相SD | 符号 | spread% | base% | 期間符号 | 判定 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: -abs(r["ic_med"] if not np.isnan(r["ic_med"]) else 0)):
        signs = "".join("+" if s > 0 else "-" if s < 0 else "·" for s in r["period_signs"])
        verdict = "✅ 合格" if not r["fails"] else "❌ " + " / ".join(r["fails"])
        L.append(f"| `{r['feature']}` | {r['horizon']} | {r['n_sym']} | "
                 f"{_fmt(r['ic_med'])} | ±{_fmt(r['off_sd'])} | "
                 f"{r['sign_hits']}/{r['n_sym']} | {_fmt(r['spread_pct'], 2)} | "
                 f"{_fmt(r['base_pct'], 2)} | {signs} | {verdict} |")
    L.append("")

    L.append("## 合格した特徴量")
    if passed:
        for r in passed:
            L.append(f"- `{r['feature']}` @ {r['horizon']}: IC {r['ic_med']:+.3f}, "
                     f"spread {r['spread_pct']:+.2f}% (base {r['base_pct']:+.2f}%)")
    else:
        L.append("**なし。** 12特徴量×3ホライズンのどれも、宣言した基準を満たさなかった。")
        L.append("")
        L.append("これは失敗ではなく結果 — 基準を緩めて何かを通すのではなく、"
                 "別の特徴量 (funding rate / OI / オンチェーン) を探す根拠になる。")
    L.append("")

    L.append("## 特徴量間の重複 (|Spearman r| >= 0.7)")
    if corr_pairs:
        for a, b, r in corr_pairs:
            L.append(f"- `{a}` ↔ `{b}`: {r:+.2f}")
        L.append("")
        L.append("→ ここに出たペアは実質同じものを測っている。両方が合格しても"
                 "「2つの証拠」ではない。")
    else:
        L.append("なし (|r| >= 0.7 のペアは検出されなかった)")
    L.append("")

    L.append("## 読み方")
    L.append(f"- **spread%** は往復コスト {ROUND_TRIP_COST_PCT}% と直接比較する単位。"
             "コストを下回るなら、統計的に有意でも実運用では損になる")
    L.append("- **base%** は同期間・同銘柄の無条件平均前方リターン。spread が base に"
             "近いなら、それは特徴量ではなく相場の上昇を測っている"
             " (前回2022H2-2023が黒字だった正体)")
    L.append("- **位相SD** は重複窓を除いた部分標本での IC のブレ。"
             "`|IC中央値| < 位相SD` なら、その数字は自分自身のノイズの中にいる")
    L.append("- **期間符号** は `2022H2-2023 / 2024 / 2025-2026` の順。"
             "揃わないものは、良い期間を選んだだけになる")
    return "\n".join(L)


# ---------------------------------------------------------------------------

def _corr_pairs(panel: pd.DataFrame, threshold: float = 0.7) -> list:
    """特徴量間の Spearman 相関のうち |r| >= threshold のペア。"""
    c = panel[FEATURES].corr(method="spearman")
    pairs = []
    for i, a in enumerate(FEATURES):
        for b in FEATURES[i + 1:]:
            r = c.loc[a, b]
            if pd.notna(r) and abs(r) >= threshold:
                pairs.append((a, b, float(r)))
    return sorted(pairs, key=lambda p: -abs(p[2]))


def run(symbols: list, label: str, days: int) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"📥 データ読み込み ({len(symbols)}銘柄・4h足)...")
    panel = load_panel(symbols, days)
    print(f"   → {len(panel):,}行 / {panel['timestamp'].min():%Y-%m-%d}"
          f" 〜 {panel['timestamp'].max():%Y-%m-%d}")

    print(f"📊 評価中 ({len(FEATURES)}特徴量 × {len(HORIZONS)}ホライズン)...")
    t0 = time.time()
    rows = []
    for feat in FEATURES:
        for hname in HORIZONS:
            r = evaluate(panel, feat, hname)
            signs = []
            for _, lo, hi in PERIODS:
                ic = evaluate(panel, feat, hname, lo, hi)["ic_med"]
                signs.append(0 if np.isnan(ic) else int(np.sign(ic)))
            r["period_signs"] = signs
            rows.append(r)
        print(f"   {feat} 完了 ({time.time()-t0:.0f}s)")

    print(f"🎲 帰無分布 ({NULL_ITERS}回 × {len(rows)}検定)...")
    null = null_max_ic(panel, n_tests=len(rows))
    for r in rows:
        r["fails"] = gate(r, r["period_signs"], null["p95"])

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    base = os.path.join(OUT_DIR, f"fe_{stamp}_{label}")
    pd.DataFrame([{k: v for k, v in r.items() if k != "period_signs"}
                  | {"period_signs": "".join(map(str, r["period_signs"])),
                     "fails": " / ".join(r["fails"])}
                  for r in rows]).to_csv(base + "_raw.csv", index=False,
                                         encoding="utf-8")
    report = build_report(rows, null, label, panel, _corr_pairs(panel))
    with open(base + "_report.md", "w", encoding="utf-8") as f:
        f.write(report)

    n_pass = sum(1 for r in rows if not r["fails"])
    print(f"\n{'✅' if n_pass else '⚠️'} 合格 {n_pass}/{len(rows)} 件")
    print(f"📄 {base}_report.md")
    return base + "_report.md"


def main():
    p = argparse.ArgumentParser(description="特徴量の単体予測力スクリーニング")
    p.add_argument("--symbols", default=",".join(SYMBOLS))
    p.add_argument("--days", type=int, default=1700,
                   help="読み込む日数 (既定=キャッシュ全期間相当)")
    p.add_argument("--label", default="v1")
    args = p.parse_args()
    run([s.strip() for s in args.symbols.split(",") if s.strip()],
        args.label, args.days)


if __name__ == "__main__":
    main()
