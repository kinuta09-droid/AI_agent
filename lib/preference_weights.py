# -*- coding: utf-8 -*-
"""
Preference Weights — パーソナライズ Phase 2 (シグナル→重み更新)

lib/engagement_log.py が集めた素のイベントログ (impression/click/reject/open/query)
を集計し、Curator と Paper Radar が参照できる「測定ベースの重み」を導出する。

設計方針 (YAGNI):
  - k-meansクラスタや埋め込みは使わない。Phase1ログの集計 (CTR・キーワード別
    加重和・半減期減衰) だけで「実際に開いたジャンル」を十分に反映できる
  - サンプルが少ないうちは無調整側 (呼び出し元にとって既定値と同じ) にフォールバック
    する。少数サンプルでの過学習を防ぐガード
  - 半減期30日の指数減衰 (tool_paper_radar.py の recency と同じ式) で
    古いシグナルを自然に減衰させる

公開 API:
  keyword_affinity(keywords, days=90)   → {keyword: weight}   (既定1.0、範囲[0.5, 2.0])
  branch_affinity(days=90, min_impressions=20) → {branch: ctr} (データ不足なら {})
  engagement_context_block(days=30)     → Curator プロンプト注入用テキスト ("" もあり)
"""
import math
import datetime

from lib.engagement_log import read_events, EVENT_WEIGHTS, TS_FORMAT, LOG_PATH as _DEFAULT_LOG_PATH

HALF_LIFE_DAYS = 30.0
_TANH_SCALE = 6.0  # 強いシグナル1〜2件で重みが飽和する程度のスケール


def _age_days(ts: str, now: datetime.datetime) -> float:
    try:
        dt = datetime.datetime.strptime(ts, TS_FORMAT)
    except (ValueError, TypeError):
        return 0.0
    return max((now - dt).total_seconds() / 86400.0, 0.0)


def _decay(age_days: float) -> float:
    return math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)


def keyword_affinity(keywords: list, days: int = 90,
                     now: datetime.datetime = None,
                     log_path: str = _DEFAULT_LOG_PATH) -> dict:
    """キーワードごとに、そのキーワードに言及したイベントの重みを
    半減期減衰付きで合算し、[0.5, 2.0] にクランプした重みを返す。

    呼び出し側は `.get(kw, 1.0)` で使うこと。ここに無いキーワード = 今まで通り
    均等加点 (1.0)。
    """
    if not keywords:
        return {}
    now = now or datetime.datetime.now()
    events = read_events(days=days, log_path=log_path)

    net = {kw: 0.0 for kw in keywords}
    for e in events:
        item = str(e.get("item", "")).lower()
        w = EVENT_WEIGHTS.get(e.get("event", ""), 0)
        if not w:
            continue
        decay = _decay(_age_days(e.get("ts", ""), now))
        for kw in keywords:
            if kw and kw.lower() in item:
                net[kw] += w * decay

    return {
        kw: max(0.5, min(2.0, 1.0 + math.tanh(score / _TANH_SCALE)))
        for kw, score in net.items() if score
    }


def branch_affinity(days: int = 90, min_impressions: int = 20,
                    now: datetime.datetime = None,
                    log_path: str = _DEFAULT_LOG_PATH) -> dict:
    """Curator/Setup の branch (consistent/bridge/entropy 等) 別CTRを返す。

    総 impression が min_impressions 未満なら {} (データ不足 = 無調整)。
    """
    now = now or datetime.datetime.now()
    events = read_events(days=days, log_path=log_path)

    impressions, clicks = {}, {}
    total_impressions = 0
    for e in events:
        if e.get("source") not in ("curate", "setup"):
            continue
        branch = e.get("meta", {}).get("branch", "")
        if not branch:
            continue
        decay = _decay(_age_days(e.get("ts", ""), now))
        if e.get("event") == "impression":
            impressions[branch] = impressions.get(branch, 0.0) + decay
            total_impressions += 1
        elif e.get("event") == "click":
            clicks[branch] = clicks.get(branch, 0.0) + decay

    if total_impressions < min_impressions:
        return {}

    branches = set(impressions) | set(clicks)
    # ラプラス平滑化 (+1/+2): サンプルの薄い branch が 0% や 100% に張り付くのを防ぐ
    return {
        b: round((clicks.get(b, 0.0) + 1) / (impressions.get(b, 0.0) + 2), 3)
        for b in branches
    }


def engagement_context_block(days: int = 30, top_n: int = 5,
                             log_path: str = _DEFAULT_LOG_PATH) -> str:
    """Curator のプロンプトに注入する短いテキストブロック。データが無ければ ""。"""
    events = read_events(days=days, log_path=log_path)
    if not events:
        return ""

    opens, clicks = {}, {}
    for e in events:
        item = str(e.get("item", "")).strip()
        if not item:
            continue
        if e.get("event") == "open":
            name = item.replace("\\", "/").rsplit("/", 1)[-1]
            name = name.rsplit(".", 1)[0]
            opens[name] = opens.get(name, 0) + 1
        elif e.get("event") == "click" and e.get("source") in ("curate", "setup"):
            clicks[item] = clicks.get(item, 0) + 1

    lines = []
    if opens:
        top_opens = sorted(opens.items(), key=lambda x: -x[1])[:top_n]
        lines.append("- よく開いたノート: " + ", ".join(n for n, _ in top_opens))
    if clicks:
        top_clicks = sorted(clicks.items(), key=lambda x: -x[1])[:top_n]
        lines.append("- よくクリックしたトピック: " + ", ".join(n for n, _ in top_clicks))

    branch_aff = branch_affinity(days=max(days, 90), log_path=log_path)
    if branch_aff:
        ranked = sorted(branch_aff.items(), key=lambda x: -x[1])
        lines.append("- スロット傾向 (実測CTR): " +
                    " > ".join(f"{b}({ctr})" for b, ctr in ranked))

    return "\n".join(lines)


if __name__ == "__main__":
    print(engagement_context_block() or "(データなし)")
