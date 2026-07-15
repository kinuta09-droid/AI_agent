# -*- coding: utf-8 -*-
"""
Antigravity Briefing Manager
Provider Registry パターンで複数の情報ソースを集約し、
Gemini で要約したブリーフィングを生成する。
"""

import os
import json
import datetime
from typing import Callable

import ccxt

from lib.lib_shared import (
    client, MODEL_NAME, retry,
    VAULT_ROOT, WORK_DIR, fetch_fear_greed_index,
)


# ---------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------

_providers: dict[str, Callable[[], str]] = {}


def add_provider(name: str, fn: Callable[[], str]):
    """ブリーフィング情報ソースを追加する。重複時は上書き。

    Usage:
        add_provider("Gmail", get_gmail_summary)
    """
    _providers[name] = fn


# ---------------------------------------------------------
# Built-in Providers
# ---------------------------------------------------------

_exchange = ccxt.binance()


def get_crypto_summary() -> str:
    """直近12時間の BTC/ETH 価格変動 + Fear & Greed Index を返す。"""
    symbols = ["BTC/USDT", "ETH/USDT"]
    lines = []

    for symbol in symbols:
        try:
            ohlcv = _exchange.fetch_ohlcv(symbol, timeframe="1h", limit=12)
            if not ohlcv:
                lines.append(f"- {symbol}: データ取得失敗")
                continue

            price_12h_ago = ohlcv[0][1]   # open of oldest candle
            price_now = ohlcv[-1][4]      # close of latest candle
            change_pct = ((price_now - price_12h_ago) / price_12h_ago) * 100
            high = max(c[2] for c in ohlcv)
            low = min(c[3] for c in ohlcv)

            lines.append(
                f"- {symbol}: ${price_now:,.0f} ({change_pct:+.2f}% / 12h) "
                f"| High ${high:,.0f} / Low ${low:,.0f}"
            )
        except Exception as e:
            lines.append(f"- {symbol}: エラー ({e})")

    fng_value, fng_label = fetch_fear_greed_index()
    lines.append(f"- Fear & Greed Index: {fng_value} ({fng_label})")

    return "\n".join(lines)


def get_obsidian_updates() -> str:
    """24時間以内に更新された 30_Main 内のファイル上位3件の内容を返す。"""
    main_dir = os.path.join(VAULT_ROOT, "30_Main")
    if not os.path.isdir(main_dir):
        return "30_Main フォルダが見つかりません。"

    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=24)
    file_list = []

    for fname in os.listdir(main_dir):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(main_dir, fname)
        if not os.path.isfile(fpath):
            continue
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
        if mtime >= cutoff:
            file_list.append((fname, fpath, mtime))

    file_list.sort(key=lambda x: x[2], reverse=True)
    top_files = file_list[:3]

    if not top_files:
        return "直近24時間に更新されたファイルはありません。"

    sections = []
    for fname, fpath, mtime in top_files:
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read(500)
            sections.append(
                f"### {fname} (更新: {mtime.strftime('%H:%M')})\n{content}"
            )
        except Exception as e:
            sections.append(f"### {fname}\n読み込みエラー: {e}")

    return "\n\n".join(sections)


# ---------------------------------------------------------
# Register Default Providers
# ---------------------------------------------------------

add_provider("Crypto", get_crypto_summary)
add_provider("Obsidian", get_obsidian_updates)

# gws CLI が利用可能なら予定＋タスクのサマリーを Briefing に含める
try:
    from tools.tool_calendar import tool_daily_summary
    add_provider("Schedule & Tasks", tool_daily_summary)
except ImportError:
    pass  # gws / tool_calendar が利用不可の場合はスキップ

# Gmail (自己登録パターン: モジュール読込時に add_provider を実行する)
try:
    import tools.tool_gmail_manager  # noqa: F401 — side effect: registers "Gmail" provider
except ImportError:
    pass


# ---------------------------------------------------------
# X Trend Section (fixed — not filtered by Gemini)
# ---------------------------------------------------------

_XTREND_JSON = os.path.join(WORK_DIR, "40_Report", "XTrend", "latest_features.json")


def _build_xtrend_section(top_n: int = 3) -> str:
    """latest_features.json から上位N件を読み込み、Briefing 用 Markdown を返す。
    ファイルが存在しない・古い（2日以上前）場合は空文字を返す。
    """
    if not os.path.exists(_XTREND_JSON):
        return ""

    try:
        with open(_XTREND_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ""

    # 鮮度チェック: 2日以上古いデータは表示しない
    date_str = data.get("date", "")
    try:
        data_date = datetime.date.fromisoformat(date_str)
        if (datetime.date.today() - data_date).days > 2:
            return ""
    except ValueError:
        return ""

    features = data.get("features", [])[:top_n]
    if not features:
        return ""

    lines = [
        "",
        "---",
        "",
        f"## 🔥 X Trend Top {top_n} — ARMS導入候補 ({date_str})",
        "",
    ]
    for i, f in enumerate(features, 1):
        arms = f.get("arms_affinity", 0)
        diff = f.get("difficulty", 0)
        name = f.get("feature_name", "?")
        summary = f.get("summary", "")
        rationale = f.get("arms_rationale", "")
        url = f.get("post_url", "")
        lines.append(
            f"{i}. **{name}** — {summary}  \n"
            f"   ARMS親和{'★' * arms}{'☆' * (5-arms)} / 難易度{'★' * diff}{'☆' * (5-diff)} / {rationale}  \n"
            f"   {url}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------
# Main Tool
# ---------------------------------------------------------

def tool_generate_briefing() -> str:
    """全プロバイダーから情報を収集し、3項目のブリーフィングを生成する。

    Returns:
        Success メッセージ + ファイルパス、またはエラーメッセージ
    """
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    now_str = datetime.datetime.now().strftime("%H%M")
    print(f"[Briefing] 📋 Generating briefing for {today_str}...")

    # 1. Collect data from all providers
    raw_sections = []
    for name, fn in _providers.items():
        print(f"  → Fetching: {name}...")
        try:
            data = fn()
            raw_sections.append(f"## {name}\n{data}")
        except Exception as e:
            raw_sections.append(f"## {name}\nエラー: {e}")

    raw_data = "\n\n".join(raw_sections)

    # 2. Summarize with Gemini (retry scoped to API call only)
    prompt = f"""あなたは個人AIエージェントのブリーフィング担当です。
今日は {today_str} です。

以下の情報ソースから、夜勤明け・活動開始前のユーザーが最短で状況把握できるよう、
最も重要な **3項目** に絞ってブリーフィングを作成してください。

ルール:
- 各項目は1-2文で簡潔に
- 数値（価格、変動率など）は具体的に記載
- アクションが必要な場合は明示
- Markdown形式（箇条書き）

--- 情報ソース ---
{raw_data}
--- ここまで ---

3項目のブリーフィング:"""

    briefing_text = _call_gemini(prompt)

    # 3. Save to 30_Main/Briefing (timestamp suffix to avoid overwrite)
    output_dir = os.path.join(VAULT_ROOT, "30_Main", "Briefing")
    os.makedirs(output_dir, exist_ok=True)
    filename = f"Briefing_{today_str}.md"
    filepath = os.path.join(output_dir, filename)

    # 既存ファイルがあればタイムスタンプ付きに変更
    if os.path.exists(filepath):
        filename = f"Briefing_{today_str}_{now_str}.md"
        filepath = os.path.join(output_dir, filename)

    xtrend_section = _build_xtrend_section(top_n=3)

    report = f"""---
up: "[[10_MOCs/Dashboard]]"
date: {today_str}
type: briefing
---

# Daily Briefing — {today_str}

{briefing_text}
{xtrend_section}

---

## Raw Data

{raw_data}
"""

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"[Briefing] ✅ Saved: {filepath}")
    return (
        f"Success: ブリーフィング生成完了。"
        f" Report: [[30_Main/Briefing/{filename}]]"
        f" (Path: {filepath})"
    )


@retry(max_retries=3)
def _call_gemini(prompt: str) -> str:
    """Gemini API 呼び出し（リトライはここだけに限定）。"""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )
    return (response.text or "").strip()
