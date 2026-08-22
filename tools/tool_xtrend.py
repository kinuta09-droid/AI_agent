# -*- coding: utf-8 -*-
"""
X Trend Tool — AI/Tech バズ投稿コレクター (twitterAPI.io)

Flow:
  1. twitterAPI.io で AI関連クエリを並列検索
  2. エンゲージメント閾値でフィルタリング + 重複排除
  3. Gemini で「機能名 / 課題 / 技術スタック / 難易度 / ARMS親和性」を構造化
  4. コンソール出力 + Obsidian 保存

Usage:
  py tools/tool_xtrend.py                    # デフォルト設定で実行
  py tools/tool_xtrend.py --min-likes 300    # いいね閾値変更
  py tools/tool_xtrend.py --query "MCP agent" --min-likes 200
"""

import os
import sys
import re
import json
import datetime
import argparse
import time
import concurrent.futures

# プロジェクトルートを sys.path に追加（tools/ 直接実行時対応）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)

from lib.lib_shared import (
    client,
    MODEL_NAME_HEAVY,
    VAULT_ROOT,
    WORK_DIR,
    retry,
)
from lib.engagement_log import log_impressions

import requests

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

TWITTER_API_KEY = os.getenv("TWITTER_API_IO_KEY", "")
TWITTER_API_BASE = "https://api.twitterapi.io"
SEARCH_ENDPOINT = "/twitter/tweet/advanced_search"

# Obsidian 保存先
XTREND_DIR = os.path.join(WORK_DIR, "40_Report", "XTrend")

# デフォルト検索クエリ — AI/エージェント関連トレンド
DEFAULT_QUERIES = [
    "AI agent tool use",
    "LLM prompt engineering technique",
    "MCP model context protocol",
    "Claude Code Cursor Windsurf",
    "RAG retrieval augmented generation pipeline",
    "open source AI local llm",
]

DEFAULT_MIN_LIKES = 500
DEFAULT_MIN_RETWEETS = 100
MAX_PER_QUERY = 15          # 1クエリあたりの最大取得数（20→15に削減）
FETCH_TIMEOUT = 15          # seconds

# 取得済みツイートIDのキャッシュファイル
SEEN_IDS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "logs", "xtrend_seen_ids.json")


# ---------------------------------------------------------
# PHASE 1: FETCH
# ---------------------------------------------------------

def _search_single_query(query: str, min_likes: int, min_rt: int) -> list[dict]:
    """1クエリ分の投稿を twitterAPI.io から取得し、フィルタリングして返す。"""
    if not TWITTER_API_KEY:
        print(f"[XTrend] ERROR: TWITTER_API_IO_KEY が未設定です", file=sys.stderr)
        return []

    # Twitter 高度検索オペレータでエンゲージメント足切り
    full_query = f'({query}) min_faves:{min_likes} lang:en -is:retweet'

    headers = {"X-API-Key": TWITTER_API_KEY}
    params  = {
        "query":     full_query,
        "queryType": "Top",
        "count":     MAX_PER_QUERY,
    }

    try:
        resp = requests.get(
            TWITTER_API_BASE + SEARCH_ENDPOINT,
            headers=headers,
            params=params,
            timeout=FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[XTrend] FETCH ERROR ({query!r}): {e}", file=sys.stderr)
        return []

    tweets_raw = data.get("tweets", [])
    results = []

    for tw in tweets_raw:
        likes = tw.get("likeCount", 0) or 0
        rts   = tw.get("retweetCount", 0) or 0
        if likes < min_likes and rts < min_rt:
            continue

        results.append({
            "id":       tw.get("id", ""),
            "text":     tw.get("text", "").replace("\n", " "),
            "author":   tw.get("author", {}).get("userName", "unknown"),
            "likes":    likes,
            "retweets": rts,
            "views":    tw.get("viewCount", 0) or 0,
            "created":  tw.get("createdAt", ""),
            "url":      tw.get("url", ""),
            "query":    query,
        })

    print(f"[XTrend] ✓ '{query}' → {len(results)} posts (filtered from {len(tweets_raw)})")
    return results


def _load_seen_ids() -> tuple[set[str], list[str]]:
    """キャッシュから取得済みツイートID (set, list) を読み込む。
    set は O(1) 検索用、list は挿入順保持（古い→新しい順）。
    """
    cache_path = os.path.normpath(SEEN_IDS_CACHE)
    if not os.path.exists(cache_path):
        return set(), []
    try:
        with open(cache_path, encoding="utf-8") as f:
            id_list: list[str] = json.load(f)
        return set(id_list), id_list
    except (json.JSONDecodeError, OSError):
        return set(), []


def _save_seen_ids(ordered_ids: list[str]) -> None:
    """取得済みツイートIDをキャッシュに書き込む（挿入順を保ち直近5,000件に制限）。"""
    cache_path = os.path.normpath(SEEN_IDS_CACHE)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    # 末尾が最新: リスト末尾5,000件を保持（古いIDを捨てる）
    trimmed = ordered_ids[-5000:]
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def fetch_trending_ai_posts(
    queries: list[str],
    min_likes: int = DEFAULT_MIN_LIKES,
    min_rt: int = DEFAULT_MIN_RETWEETS,
) -> list[dict]:
    """複数クエリを並列実行し、新規投稿のみ返す（差分取得）。"""
    print(f"\n[XTrend] === PHASE 1: FETCH ({len(queries)} queries, min_likes={min_likes}) ===")

    # キャッシュから既読IDを読み込み（set: 高速検索用, list: 挿入順保持用）
    seen_ids_set, seen_ids_list = _load_seen_ids()
    cached_count = len(seen_ids_set)
    print(f"[XTrend] Cached IDs: {cached_count} (skipping already-seen tweets)")

    all_posts: list[dict] = []
    new_ids: set[str] = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(_search_single_query, q, min_likes, min_rt): q
            for q in queries
        }
        for fut in concurrent.futures.as_completed(futures):
            posts = fut.result()
            for p in posts:
                tid = p["id"]
                if tid and tid not in seen_ids_set and tid not in new_ids:
                    new_ids.add(tid)
                    all_posts.append(p)

    # 取得済みIDキャッシュを更新（挿入順リストに新IDを末尾追加）
    seen_ids_list.extend(sorted(new_ids))   # sorted で決定的な順序を保証
    _save_seen_ids(seen_ids_list)

    # エンゲージメント降順でソート（likes + retweets×2 の複合スコア）
    all_posts.sort(key=lambda x: x["likes"] + x["retweets"] * 2, reverse=True)

    print(f"[XTrend] New posts: {len(all_posts)}, skipped (cached): {cached_count}, total cached: {len(seen_ids_list)}")
    return all_posts


# ---------------------------------------------------------
# PHASE 2: ANALYZE
# ---------------------------------------------------------

ANALYZE_SYSTEM = """あなたはシニアAIエンジニアです。
以下のXの投稿リスト（JSON配列）を分析し、各投稿に含まれる「AIの新技術・手法・ツール」を抽出して構造化してください。

各アイテムについて以下のフィールドを持つJSONオブジェクトを生成してください:
- feature_name: 機能・技術・ツールの名前（日本語、15文字以内）
- summary: 何ができるか（日本語、50文字以内）
- problem_solved: 解決する課題（日本語、40文字以内）
- tech_stack: 必要な技術・ライブラリ（英語、カンマ区切り）
- difficulty: 実装難易度 1〜5（1=簡単、5=高度）
- arms_affinity: 個人AIエージェント「ARMS」への導入適性 1〜5（1=不向き、5=最適）
- arms_rationale: 適性の理由（日本語、40文字以内）
- post_url: 元投稿URL
- engagement_score: likes + retweets*2 の数値

重要: 技術的に意味のない投稿（宣伝・一般論など）は除外してください。
必ずJSON配列（[]）のみを返してください。余分なテキストは不要です。"""


@retry(max_retries=3, initial_delay=5)
def _call_gemini_analyze(posts_json: str) -> str:
    resp = client.models.generate_content(
        model=MODEL_NAME_HEAVY,
        contents=f"{ANALYZE_SYSTEM}\n\n投稿リスト:\n{posts_json}",
    )
    return resp.text


def _extract_json(raw: str) -> list[dict]:
    """LLM出力からJSON配列を抽出する。前置テキストやコードブロックに対応。"""
    raw = raw.strip()
    # ``` コードブロックが含まれる場合は中身だけ取り出す
    m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', raw)
    if m:
        raw = m.group(1).strip()
    # コードブロックがなくても [ で始まる JSON 配列を探す
    start = raw.find('[')
    if start > 0:
        raw = raw[start:]
    return json.loads(raw)


def analyze_posts(posts: list[dict], batch_size: int = 30) -> list[dict]:
    """投稿リストをバッチ処理し、Gemini で構造化分析する。"""
    print(f"\n[XTrend] === PHASE 2: ANALYZE ({len(posts)} posts, batch={batch_size}) ===")

    analyzed: list[dict] = []

    # 必要フィールドだけ渡してトークン節約
    for i in range(0, len(posts), batch_size):
        batch = posts[i:i + batch_size]
        slim = [
            {"text": p["text"], "url": p["url"],
             "likes": p["likes"], "retweets": p["retweets"]}
            for p in batch
        ]
        print(f"[XTrend] Analyzing batch {i//batch_size + 1} ({len(batch)} posts)...")

        try:
            raw = _call_gemini_analyze(json.dumps(slim, ensure_ascii=False))
            items = _extract_json(raw)
            analyzed.extend(items)
            print(f"[XTrend] ✓ {len(items)} features extracted")
        except (json.JSONDecodeError, Exception) as e:
            print(f"[XTrend] ANALYZE ERROR (batch {i//batch_size + 1}): {e}", file=sys.stderr)

        if i + batch_size < len(posts):
            time.sleep(2)   # Gemini レートリミット回避

    # 重複排除: feature_name を正規化して同一技術をまとめる（engagement_score 最大を残す）
    analyzed = _deduplicate_features(analyzed)

    # post_url から元クエリを逆引きして付与する (項目9: 収集クエリの動的チューニング用)。
    # Gemini には post_url をそのまま出力させているので、新しいフィールドを
    # 生成させずにコード側だけで紐付けられる。
    url_to_query = {p["url"]: p["query"] for p in posts if p.get("url")}
    for f in analyzed:
        f["source_query"] = url_to_query.get(f.get("post_url", ""), "")

    # ARMS親和性降順でソート
    analyzed.sort(key=lambda x: (x.get("arms_affinity", 0), x.get("engagement_score", 0)), reverse=True)

    print(f"[XTrend] Total features: {len(analyzed)}")
    return analyzed


def _normalize_name(name: str) -> str:
    """比較用キー: 小文字化・記号除去・空白圧縮。"""
    name = name.lower()
    name = re.sub(r'[\s\-_\.()（）【】「」/]', '', name)
    # 括弧内の補足説明を除去（例: "model context protocol (mcp)" → "modelcontextprotocol"）
    return name


def _deduplicate_features(features: list[dict]) -> list[dict]:
    """同一技術の重複エントリを統合し、engagement_score が最大のものを残す。"""
    best: dict[str, dict] = {}   # normalized_name → feature

    for f in features:
        key = _normalize_name(f.get("feature_name", ""))
        if not key:
            continue
        existing = best.get(key)
        if existing is None:
            best[key] = f
        else:
            # engagement_score が大きい方を優先
            if (f.get("engagement_score") or 0) > (existing.get("engagement_score") or 0):
                best[key] = f

    deduped = list(best.values())
    removed = len(features) - len(deduped)
    if removed:
        print(f"[XTrend] Deduplicated: {removed} duplicate(s) removed → {len(deduped)} unique features")
    return deduped


# ---------------------------------------------------------
# OUTPUT: Console + Obsidian
# ---------------------------------------------------------

def print_results(features: list[dict]) -> None:
    """コンソールに結果を表示する。"""
    print(f"\n{'='*60}")
    print(f"  X TREND ANALYSIS — {len(features)} features found")
    print(f"{'='*60}")

    for i, f in enumerate(features, 1):
        arms = f.get("arms_affinity", 0)
        diff = f.get("difficulty", 0)
        stars_arms = "★" * arms + "☆" * (5 - arms)
        stars_diff = "★" * diff + "☆" * (5 - diff)

        print(f"\n[{i:02d}] {f.get('feature_name', 'N/A')}")
        print(f"      概要    : {f.get('summary', '')}")
        print(f"      課題    : {f.get('problem_solved', '')}")
        print(f"      技術    : {f.get('tech_stack', '')}")
        print(f"      難易度  : {stars_diff} ({diff}/5)")
        print(f"      ARMS親和: {stars_arms} ({arms}/5) — {f.get('arms_rationale', '')}")
        print(f"      URL     : {f.get('post_url', '')}")


def save_to_obsidian(features: list[dict], queries: list[str]) -> str:
    """Obsidian Vault に Markdown ファイルとして保存する。パスを返す。"""
    os.makedirs(XTREND_DIR, exist_ok=True)

    today = datetime.date.today().isoformat()
    filename = f"{today}_xtrend.md"
    filepath = os.path.join(XTREND_DIR, filename)

    lines = [
        f"# X Trend Analysis — {today}",
        "",
        f"**取得クエリ**: {', '.join(queries)}",
        f"**抽出機能数**: {len(features)}",
        f"**生成日時**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "---",
        "",
        "## Feature List",
        "",
    ]

    for i, f in enumerate(features, 1):
        arms = f.get("arms_affinity", 0)
        diff = f.get("difficulty", 0)
        lines += [
            f"### {i:02d}. {f.get('feature_name', 'N/A')}",
            "",
            f"| 項目 | 内容 |",
            f"|------|------|",
            f"| 概要 | {f.get('summary', '')} |",
            f"| 解決課題 | {f.get('problem_solved', '')} |",
            f"| 技術スタック | `{f.get('tech_stack', '')}` |",
            f"| 実装難易度 | {'★' * diff}{'☆' * (5-diff)} ({diff}/5) |",
            f"| ARMS親和性 | {'★' * arms}{'☆' * (5-arms)} ({arms}/5) |",
            f"| ARMS評価理由 | {f.get('arms_rationale', '')} |",
            f"| 元投稿 | {f.get('post_url', '')} |",
            "",
        ]

    content = "\n".join(lines)
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(content)

    # ブリーフィング連携用 JSON を同ディレクトリに上書き保存
    json_path = os.path.join(XTREND_DIR, "latest_features.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"date": today, "features": features}, fh, ensure_ascii=False, indent=2)

    print(f"\n[XTrend] 💾 Saved → {filepath}")
    print(f"[XTrend] 💾 JSON  → {json_path}")
    return filepath


# ---------------------------------------------------------
# PHASE 3: CLAUDE CODE PROMPT GENERATION
# ---------------------------------------------------------

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "logs", "xtrend_prompts"
)

PROMPT_SYSTEM = """あなたはシニア・ソフトウェアエンジニアです。
以下の「実装ターゲット情報」と「ARMSプロジェクト情報」をもとに、
Claude Code に投入するための**日本語実装指示書**を生成してください。

生成する指示書の構成（必須）:
1. **Goal** — 何を実現するか（1-2文）
2. **背景** — なぜARMSに必要か（1-2文）
3. **実装アプローチ** — 技術スタックと実装方式の推論（箇条書き）
4. **新規/修正ファイル** — 作成・変更が必要なファイルを推定して列挙
5. **実装手順** — Step 1〜N の番号付きリスト（具体的）
6. **依存パッケージ** — pip install が必要なもの（なければ「なし」）
7. **検証方法** — 動作確認の手順
8. **制約** — ARMS固有の制約（既存コードと競合しない点、Windowsパス等）

重要:
- 既存ツールとの重複を避ける（既存ファイルリストを参照）
- lib/lib_shared.py の既存クライアント（Gemini, Tavily等）を最大活用すること
- PEP8 + utf-8 + 絶対 import (from lib.xxx import) を守ること
- テキストのみを返すこと（コードは不要）"""


def _scan_arms_tools() -> dict:
    """ARMSプロジェクトの既存ファイル構成をスキャンして返す。"""
    project_root = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    )
    result = {"tools": [], "libs": [], "commands": []}

    tools_dir = os.path.join(project_root, "tools")
    if os.path.isdir(tools_dir):
        result["tools"] = sorted(
            f for f in os.listdir(tools_dir) if f.endswith(".py")
        )

    lib_dir = os.path.join(project_root, "lib")
    if os.path.isdir(lib_dir):
        result["libs"] = sorted(
            f for f in os.listdir(lib_dir) if f.endswith(".py")
        )

    commands_dir = os.path.join(project_root, "commands")
    if os.path.isdir(commands_dir):
        result["commands"] = sorted(
            f for f in os.listdir(commands_dir) if f.endswith(".py")
        )

    return result


def _load_feature_by_index(index: int) -> dict | None:
    """latest_features.json から 1-indexed で feature を取得する。"""
    json_path = os.path.join(XTREND_DIR, "latest_features.json")
    if not os.path.exists(json_path):
        print(f"[XTrend] latest_features.json が見つかりません。先に run() を実行してください。",
              file=sys.stderr)
        return None
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        features = data.get("features", [])
        if index < 1 or index > len(features):
            print(f"[XTrend] feature index {index} は範囲外です (1〜{len(features)})", file=sys.stderr)
            return None
        return features[index - 1]
    except (json.JSONDecodeError, OSError) as e:
        print(f"[XTrend] JSON読み込みエラー: {e}", file=sys.stderr)
        return None


@retry(max_retries=2, initial_delay=3)
def _call_gemini_prompt(user_content: str) -> str:
    resp = client.models.generate_content(
        model=MODEL_NAME_HEAVY,
        contents=f"{PROMPT_SYSTEM}\n\n{user_content}",
    )
    return resp.text


def generate_claude_prompt(feature: dict, save: bool = True) -> str:
    """feature dict から Claude Code 実装指示書を生成して返す。

    Args:
        feature: XTrend analyze_posts() が返す feature dict
        save: True のとき logs/xtrend_prompts/ にファイル保存

    Returns:
        生成された実装指示書テキスト
    """
    scan = _scan_arms_tools()

    user_content = f"""## 実装ターゲット情報
- 機能名: {feature.get('feature_name', '?')}
- 概要: {feature.get('summary', '')}
- 解決課題: {feature.get('problem_solved', '')}
- 技術スタック: {feature.get('tech_stack', '')}
- 実装難易度: {feature.get('difficulty', '?')}/5
- ARMS親和性: {feature.get('arms_affinity', '?')}/5
- 評価理由: {feature.get('arms_rationale', '')}
- 参考投稿: {feature.get('post_url', '')}

## ARMSプロジェクト情報
- OS: Windows 11 / Python 3.13 / 起動コマンド: py
- プロジェクトパス: {_PROJECT_ROOT}
- 既存ツール (tools/): {', '.join(scan['tools'])}
- 既存ライブラリ (lib/): {', '.join(scan['libs'])}
- 既存コマンド (commands/): {', '.join(scan['commands'])}
- 利用可能クライアント: Gemini (MODEL_NAME_HEAVY={MODEL_NAME_HEAVY}), Tavily, Discord, ccxt
- Obsidian Vault: {WORK_DIR} (WORK_DIR)

## 注意
- 既存ツールと重複する機能は新規作成せず、既存を拡張すること
- CLAUDE.md の制約に従うこと（エンコーディング安全規則など）"""

    print(f"[XTrend] 📝 Generating Claude Code prompt for: {feature.get('feature_name', '?')}...")
    prompt_text = _call_gemini_prompt(user_content)

    if save:
        os.makedirs(os.path.normpath(PROMPTS_DIR), exist_ok=True)
        today = datetime.date.today().isoformat()
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', feature.get('feature_name', 'feature'))[:30]
        filename = f"{today}_{safe_name}.md"
        filepath = os.path.normpath(os.path.join(PROMPTS_DIR, filename))
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Claude Code 実装指示書 — {feature.get('feature_name', '?')}\n\n")
            f.write(f"> 生成日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}  \n")
            f.write(f"> 参考: {feature.get('post_url', '')}\n\n---\n\n")
            f.write(prompt_text)
        print(f"[XTrend] 💾 Prompt saved → {filepath}")

    return prompt_text


# ---------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------

def run(
    queries: list[str] | None = None,
    min_likes: int = DEFAULT_MIN_LIKES,
    min_rt: int = DEFAULT_MIN_RETWEETS,
    no_save: bool = False,
) -> list[dict]:
    """メインフロー。分析済みfeatureリストを返す。"""
    if not queries:
        queries = DEFAULT_QUERIES

    posts    = fetch_trending_ai_posts(queries, min_likes=min_likes, min_rt=min_rt)
    if not posts:
        print("[XTrend] 投稿が取得できませんでした。クエリまたは閾値を確認してください。")
        return []

    features = analyze_posts(posts)

    print_results(features)

    # 項目9 (収集クエリの動的チューニング) Phase 0: クエリごとの提示実績を記録するだけ。
    # 自動反映はしない。log_impressions は失敗を握りつぶす設計なのでここでは無視してよい。
    log_impressions(features, source="xtrend", item_type="feature")

    if not no_save:
        save_to_obsidian(features, queries)

    return features


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="X Trend AI Feature Collector")
    parser.add_argument("--query",     nargs="+", help="検索クエリ（複数可）")
    parser.add_argument("--min-likes", type=int, default=DEFAULT_MIN_LIKES,
                        help=f"最低いいね数 (default: {DEFAULT_MIN_LIKES})")
    parser.add_argument("--min-rt",    type=int, default=DEFAULT_MIN_RETWEETS,
                        help=f"最低RT数 (default: {DEFAULT_MIN_RETWEETS})")
    parser.add_argument("--no-save",   action="store_true", help="Obsidianに保存しない")
    parser.add_argument("--prompt",    type=int, metavar="N",
                        help="feature番号(1始まり)のClaude Code実装指示書を生成して表示")
    args = parser.parse_args()

    if args.prompt:
        # Phase 3: 指示書生成モード（fetch/analyze は行わず JSON から読む）
        feature = _load_feature_by_index(args.prompt)
        if feature:
            prompt_text = generate_claude_prompt(feature, save=True)
            print("\n" + "=" * 60)
            print(f"  CLAUDE CODE PROMPT — {feature.get('feature_name', '?')}")
            print("=" * 60)
            print(prompt_text)
    else:
        run(
            queries=args.query,
            min_likes=args.min_likes,
            min_rt=args.min_rt,
            no_save=args.no_save,
        )
