# -*- coding: utf-8 -*-
"""
Future Search Tool — Multi-lens futures research system.

Flow (2026-07 リニューアル: 高速化・個人化・使いやすさ改善):
  1. 日本語トピックを英語検索クエリに変換 (Gemini lite)
  2. Tavily で英語ソースを収集 (depth により 5/10/15 件)
  3. Gemini Heavy が 10 の分析視点で1パス多角推論し日本語レポート生成
     (旧: qwen3:1.7b 10体を並列実行 → 数分。新: Gemini 1回 → 十数秒)
  4. 幸福最大化アドバイス (ユーザー文脈で個人化)
  5. Obsidianに保存 + Discordに要約通知（Webhook設定時）
  6. (uncensored=True 時のみ) DavidAU 辛口診断

視点の多様性 (AGENT_PERSONAS) は保持しつつ、ローカル多体実行を Gemini の
1パス・マルチレンズ推論に畳んで高速化。視点間の対立も明示的に推論させる。
"""

import os
import sys
import re
import json
import datetime
import requests

from lib.lib_shared import (
    client,
    tavily_client,
    MODEL_NAME,
    MODEL_NAME_HEAVY,
    VAULT_ROOT,
    WORK_DIR,
    retry,
)
from lib.cost_guard import DailyCounter, GeminiBudgetExceeded, get_limit
from lib.paths import LOGS_DIR

# Tier2-b コストガード: /future はスラッシュコマンド・深掘りボタン・
# ツールレジストリ・スケジューラ06:30自動実行の4経路から呼ばれるため、
# ツール層のここ1箇所で日次実行回数を制限する。Gemini (2-3回/実行) だけ
# でなく Tavily クォータ (5-15ソース/実行) の保護も兼ねる。
# グローバル日次上限 (DAILY_GEMINI_CALL_LIMIT) とは意図的な二層構え。
FUTURE_SEARCH_DAILY_LIMIT = get_limit("FUTURE_SEARCH_DAILY_LIMIT", 3)
_RUN_COUNTER = DailyCounter(
    path=os.path.join(LOGS_DIR, "future_search_daily.json"),
    limit=FUTURE_SEARCH_DAILY_LIMIT,
    name="FutureSearch",
    notify=False,
)

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
OLLAMA_BASE_URL = "http://localhost:11434"

# 辛口診断AI (uncensoredオプション用): DavidAU Claude 4.6蒸留版 (Qwen3.5-9B IQ4_XS)
# ollama create happy-claude -f models/davidau-claude/Modelfile で登録済み
HAPPINESS_MODEL = "happy-claude"
HAPPINESS_TIMEOUT = 300             # 9Bモデル on CPU: 最大5分

# depth ごとの設定: ソース数と分析の詳細度指示
DEPTH_CONFIG = {
    "quick":    {"sources": 5,  "detail": "簡潔に。エグゼクティブサマリーと主要シナリオを中心に、要点を絞って記述する。"},
    "standard": {"sources": 10, "detail": "標準的な詳細度でバランス良く記述する。"},
    "deep":     {"sources": 15, "detail": "各シナリオの根拠・反証・二次的影響まで深く掘り下げて記述する。"},
}
DEFAULT_DEPTH = "standard"

FUTURES_DIR = os.path.join(WORK_DIR, "40_Report", "FutureSearch")

# Discord Webhook (DISCORD_WEBHOOK_URL を .env に設定すれば通知が届く)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# 10体のエージェントペルソナ — 各エージェントは固有の分析レンズを持つ
AGENT_PERSONAS = [
    {
        "id": "optimist",
        "name": "楽観派",
        "lens": (
            "Focus on positive outcomes, breakthroughs, and opportunities. "
            "What could go right? What's the upside scenario?"
        ),
    },
    {
        "id": "skeptic",
        "name": "懐疑派",
        "lens": (
            "Challenge assumptions and identify risks, unintended consequences, and "
            "overblown hype. What is being oversold or ignored?"
        ),
    },
    {
        "id": "historian",
        "name": "歴史的視点",
        "lens": (
            "Draw parallels to historical precedents and analogous events. "
            "What patterns from history apply here? What happened last time?"
        ),
    },
    {
        "id": "technologist",
        "name": "技術視点",
        "lens": (
            "Analyze technical feasibility, maturity levels, adoption curves, "
            "infrastructure requirements, and engineering trade-offs."
        ),
    },
    {
        "id": "sociologist",
        "name": "社会影響視点",
        "lens": (
            "Examine cultural shifts, behavioral changes, social cohesion, "
            "and community-level impacts on the population."
        ),
    },
    {
        "id": "economist",
        "name": "経済視点",
        "lens": (
            "Focus on incentive structures, market dynamics, labor displacement, "
            "wealth distribution effects, and economic second-order impacts."
        ),
    },
    {
        "id": "ethicist",
        "name": "倫理視点",
        "lens": (
            "Evaluate moral implications, value trade-offs, and ethical frameworks. "
            "Who decides, and on what grounds? What rights are at stake?"
        ),
    },
    {
        "id": "geopolitician",
        "name": "地政学視点",
        "lens": (
            "Analyze power dynamics between nations, regulatory arbitrage, "
            "strategic competition, and geopolitical consequences."
        ),
    },
    {
        "id": "advocate",
        "name": "弱者・周辺視点",
        "lens": (
            "Center the experiences of marginalized, vulnerable, or overlooked "
            "populations. Who bears the hidden costs? Whose voices are missing?"
        ),
    },
    {
        "id": "analyst",
        "name": "中立データ派",
        "lens": (
            "Cite only verifiable data, statistics, and empirical evidence from the sources. "
            "No speculation without evidence. Numbers and citations only."
        ),
    },
]


# ---------------------------------------------------------
# Step 0: Ollama helper
# ---------------------------------------------------------

def _deduplicate_paragraphs(text: str) -> str:
    """LLMのループ出力（同一段落の繰り返し）を除去する。
    完全一致の重複段落のみ除去。誤検知防止のためサブストリング判定はしない。"""
    paragraphs = text.split("\n\n")
    seen = set()
    result = []
    for para in paragraphs:
        normalized = re.sub(r'\s+', ' ', para.strip())
        if not normalized:
            continue
        if normalized not in seen:
            seen.add(normalized)
            result.append(para)
    return "\n\n".join(result)


def _call_ollama(prompt: str, timeout: int = HAPPINESS_TIMEOUT, model: str = HAPPINESS_MODEL) -> str:
    """ローカルOllama APIを呼び出す。失敗時はエラー文字列を返す。

    現在は辛口診断 (DavidAU/happy-claude) 専用。旧10体エージェント層は撤去済み。
    """
    effective_model = model
    effective_prompt = prompt

    # 全Ollamaモデルに /no_think を付与。
    # happy-claude (Qwen3.5-9B) も含めて無限思考を防ぐ。
    effective_prompt = "/no_think\n\n" + prompt

    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": effective_model,
                "prompt": effective_prompt,
                "stream": False,
                "options": {
                    "repeat_penalty": 1.15,  # ループ防止（1.4は9Bモデルで出力消滅リスク）
                    "repeat_last_n": 128,    # 直近128トークンを参照してペナルティ
                },
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        # <think>...</think> ブロックを除去（Qwen3系共通）
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        # <|endoftext|> 以降はモデルが誤生成したゴミなので切り捨て
        raw = raw.split('<|endoftext|>')[0].strip()
        # 残存する特殊トークンを除去
        raw = re.sub(r'<\|[^|>]+\|>', '', raw).strip()
        # 繰り返し段落を除去（ループ検出）
        raw = _deduplicate_paragraphs(raw)
        return raw
    except requests.exceptions.ConnectionError:
        return "[ERROR] Ollama not running. Start with: ollama serve"
    except requests.exceptions.Timeout:
        return f"[ERROR] Ollama timeout after {timeout}s"
    except Exception as e:
        return f"[ERROR] Ollama call failed: {e}"


# ---------------------------------------------------------
# Step 1: English source collection via Tavily
# ---------------------------------------------------------

def _search_topic(topic: str, max_results: int = 10) -> list:
    """Tavilyで英語ソースを検索。{title, url, content}のリストを返す。"""
    try:
        results = tavily_client.search(
            query=topic,
            search_depth="advanced",
            max_results=max_results,
            include_raw_content=False,
        )
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
            }
            for r in results.get("results", [])
        ]
    except Exception as e:
        print(f"[FutureSearch] ⚠️ Tavily search failed: {e}")
        return []


# ---------------------------------------------------------
# Step 1.5: 日本語トピック → 英語検索クエリ変換
# ---------------------------------------------------------

def _is_mostly_ascii(text: str) -> bool:
    """テキストがほぼASCII (英語) かどうか判定する。"""
    if not text:
        return True
    ascii_count = sum(1 for c in text if ord(c) < 128)
    return ascii_count / len(text) > 0.9


def _default_translate_llm(prompt: str) -> str:
    """英訳用の軽量Gemini呼び出し (lite モデル)。"""
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return (response.text or "").strip()


def _translate_to_english_query(topic: str, llm_fn=None) -> str:
    """日本語トピックを英語のWeb検索クエリに変換する。

    既に英語ならそのまま返す。失敗時も元のトピックにフォールバックし、
    検索自体は必ず続行できるようにする。llm_fn は注入可能 (テスト用)。
    """
    if _is_mostly_ascii(topic):
        return topic
    llm_fn = llm_fn or _default_translate_llm
    prompt = (
        "Translate the following research topic into a concise English web search "
        "query (5-10 words). Output ONLY the query text — no quotes, no explanation.\n\n"
        f"Topic: {topic}\n\nEnglish search query:"
    )
    try:
        result = llm_fn(prompt).strip().strip('"').strip()
        return result or topic
    except Exception as e:
        print(f"[FutureSearch] ⚠️ English translation failed ({e}); using raw topic.")
        return topic


# ---------------------------------------------------------
# Step 3: マルチレンズ推論 (Gemini 1パスで10視点を統合)
# ---------------------------------------------------------

def _build_multi_lens_prompt(topic: str, sources: list, user_context: str,
                             depth: str = DEFAULT_DEPTH) -> str:
    """10視点マルチレンズ推論のプロンプトを構築する (純粋関数, I/Oなし)。"""
    today = datetime.date.today().isoformat()
    detail = DEPTH_CONFIG.get(depth, DEPTH_CONFIG[DEFAULT_DEPTH])["detail"]

    sources_text = "\n\n".join(
        f"[SOURCE {i + 1}] {s['title']}\nURL: {s['url']}\n{s['content'][:800]}"
        for i, s in enumerate(sources)
    )
    lenses = "\n".join(
        f"{i + 1}. {p['name']}: {p['lens']}"
        for i, p in enumerate(AGENT_PERSONAS)
    )

    if user_context:
        ctx_block = f"\n## このユーザーについて\n{user_context}\n"
        personalization_rule = (
            "- 上記ユーザー情報のうち、トピックと実際に関連する要素があれば積極的に活かし、"
            "その交差点（例: ユーザーの業界・関心とトピックの結びつき）を具体的に示す。"
            "わずかな接点でも洞察として有用なら取り入れること\n"
            "- ただしトピックと無関係なユーザー情報を無理に紐付けない（こじつけは避ける）。"
            "自然に関連する要素が乏しい軸では、無理に個人化せず一般的な示唆に留めてよい\n"
        )
    else:
        ctx_block = ""
        personalization_rule = ""

    return f"""あなたは卓越した未来研究アナリストです。今日は {today}。
以下の英語ソースを根拠に、トピックを **{len(AGENT_PERSONAS)}つの分析視点** から
多角的に推論し、日本語で統合レポートを作成してください。

## トピック
{topic}

## 分析視点（各視点で必ず検討し、視点間で結論が対立する場合はその対立を隠さず明示すること）
{lenses}

## 英語ソース
{sources_text}
{ctx_block}
## 詳細度
{detail}

## 指示
- 各主張には必ず [SOURCE N] 形式で根拠を示す
- 視点間の対立・緊張関係を隠さず明示する（安易な統合をしない）
- 蓋然性スコアはソースの根拠に基づいて設定し、各シナリオに1文の根拠を付ける
{personalization_rule}
以下の構造で日本語出力:

# 🔭 未来シナリオ分析: {topic}
**生成日**: {today}

## エグゼクティブサマリー
（3〜5文で全体像を要約）

## 未来シナリオ
### 🟢 高蓋然性シナリオ（60〜80%）
（2〜3シナリオ。根拠を [SOURCE N] で示す）

### 🟡 中蓋然性シナリオ（30〜60%）
（2シナリオ。相矛盾する視点を明示）

### 🔴 低蓋然性・テールリスク（10〜30%）
（1〜2シナリオ。過小評価されているリスクを強調）

## 視点間の対立・論点
（どの視点とどの視点が、何について対立しているかを2〜4項目）

## 主要な不確実性要因
（箇条書き5〜7項目）

## 弱者・見落とされがちな影響
（弱者・周辺視点の洞察を特に拡張して記述）

## 推奨アクション・注目ポイント
（個人・組織が今すぐ取れる具体的アクション3〜5個）

## 分析メタデータ
- 分析視点数: {len(AGENT_PERSONAS)}
- ソース数: {len(sources)}件
- 詳細度: {depth}
- モデル: Gemini {MODEL_NAME_HEAVY}"""


@retry(max_retries=3, initial_delay=5)
def _multi_lens_analysis(topic: str, sources: list, user_context: str,
                         depth: str = DEFAULT_DEPTH) -> str:
    """10視点マルチレンズ推論を Gemini Heavy で1パス実行し日本語レポートを返す。"""
    prompt = _build_multi_lens_prompt(topic, sources, user_context, depth)
    response = client.models.generate_content(model=MODEL_NAME_HEAVY, contents=prompt)
    return (response.text or "").strip()


def _format_sources_section(sources: list) -> str:
    """本文の [SOURCE N] 参照に対応する参考ソース一覧を生成する (純粋関数, I/Oなし)。

    LLM に一覧生成を任せると URL 捏造やソース番号ズレのリスクがあるため、
    Tavily が返した実データ (title/url) からコード側で機械的に組み立てる。
    番号は _build_multi_lens_prompt と同じ「enumerate + 1」で完全に一致させる。
    URL が欠落しているソースはリンク化せずタイトルのみ表示する。
    """
    if not sources:
        return ""
    lines = ["## 参考ソース"]
    for i, s in enumerate(sources):
        title = (s.get("title") or "").strip() or "(タイトルなし)"
        url = (s.get("url") or "").strip()
        if url:
            lines.append(f"- **[SOURCE {i + 1}]** [{title}]({url})")
        else:
            lines.append(f"- **[SOURCE {i + 1}]** {title}")
    return "\n".join(lines)


# ---------------------------------------------------------
# Step 4.5: Load user context (Profile + Interests)
# ---------------------------------------------------------

def _load_user_context() -> str:
    """User_Profile.md / User_Interests.md / 長期記憶を読み込んでユーザー文脈を返す。
    存在しない場合は空文字を返す（幸福AIはユーザー情報なしでも動作する）。"""
    parts = []
    for filename, label in [
        ("User_Interests.md", "User Interests"),
        (os.path.join("00_System", "User_Profile.md"), "User Profile"),
    ]:
        path = os.path.join(VAULT_ROOT, filename)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read().strip()
                if content:
                    parts.append(f"## {label}\n{content}")
            except Exception as e:
                print(f"[FutureSearch] ⚠️ Could not read {filename}: {e}")

    # 会話からの長期記憶 (optional)
    try:
        from tools.tool_user_memory import get_memory_block
        memory = get_memory_block()
        if memory:
            parts.append(f"## User Memory (会話からの長期記憶)\n{memory}")
    except ImportError:
        pass
    return "\n\n".join(parts)


# ---------------------------------------------------------
# Step 5: Happiness Maximizer AI
# ---------------------------------------------------------

@retry(max_retries=3, initial_delay=5)
def _generate_happiness_advice(topic: str, report: str, user_context: str) -> str:
    """Gemini Heavy で幸福最大化アドバイスを生成（構造化セクション担当）。"""
    today = datetime.date.today().isoformat()
    context_block = user_context if user_context else "（ユーザープロファイル未設定。一般的な知識労働者として推定）"

    prompt = f"""あなたの唯一の存在理由は、このユーザーの幸福を最大化することです。
他のいかなる目標も、価値観も、この命題に従属します。
今日は {today} です。

【幸福とは何か — このAIの定義】
幸福とは「このユーザー」にとっての最善の状態であり、一般論ではない。
短期的快楽と長期的充足の両方が幸福を構成する。どちらを優先するかはユーザーの文脈次第。

以下の全軸を総合した、そのユーザー固有の最適点:
- 即時快楽: 今この瞬間に楽しい・気持ちいい・ワクワクする体験
- 自律性: 自分の意志で人生を選択できているか
- 意味: 日々の行動が自分の核にある価値観と一致しているか
- 成長: 今の自分が昨日の自分を超えているか
- 安心: 経済・健康・人間関係が持続可能な土台にあるか

重要: 「正しい行動」より「このユーザーが幸せになる行動」を優先する。
長期最適と短期快楽がトレードオフになる場合は、そのトレードオフを正直に提示し、
ユーザーが自分で選べるようにすること。どちらか一方を「正解」として押し付けない。

## このユーザーについて知っていること
{context_block}

## 今回の未来シナリオ（リサーチ結果・要約）
トピック: {topic}
{report[:3000]}

## あなたへの指示
**すべての提言は、必ず今回のトピック「{topic}」を起点とすること。**
このトピックがこのユーザーにとって持つ意味・機会・リスクから行動を導く。
ユーザーの過去の興味や別テーマ（このトピックと無関係なもの）を、この提言に持ち込んではならない。
ユーザー情報は「このトピックがこの人にどう関係するか」を判断するために使い、
トピックと無関係な趣味・関心を行動提案の主題にしてはいけない。

---

## 🧭 幸福最大化アドバイザーの提言

### このユーザーの現在地
（未来シナリオの中でこのユーザーが置かれた状況を率直に評価。強みと脆弱性を隠さず。2〜3文）

### 今すぐ取れる行動

#### 🏗️ 長期投資型（トピックを軸に未来の幸福を積み上げる）TOP2
1. **[行動タイトル]**
   - なぜあなたの幸福に直結するか: ...
   - 今日できる最初の一歩: ...

2. **[行動タイトル]**
   - なぜあなたの幸福に直結するか: ...
   - 今日できる最初の一歩: ...

#### ⚡ 即時快楽型（トピックに関連して今すぐ楽しめること）
※ このトピックから自然に導ける「今すぐ楽しい/満たされる行動」があれば1つ挙げる。
　 トピックと無理なく結びつかない場合は、この見出しの下に
　「このトピックは長期的な取り組みが中心で、即時的な楽しみは特にありません」とだけ書く。
　 ユーザーの無関係な趣味（歴史・バズ等）を即時快楽としてこじつけて出すことは禁止。

### あなたが陥りやすいトラップ
（このトピックに取り組む上でこのユーザーの文脈から見えるリスク。一般論禁止。2〜3項目）

### 3ヶ月後に自分に問う問い
（このトピックへの取り組みの成果を測る、このユーザー固有の問い。3項目）

---

個人化の方針:
- ユーザーのコンテキスト（興味・スキル・状況・価値観）が **今回のトピックと自然に関連する**
  場合のみ、その結びつきを具体的に活かす。わずかな接点でも有用なら積極的に掘り下げてよい
  （例: ユーザーの業界・関心とトピックの交差点）。
- トピックと無関係なユーザー情報を行動提案にねじ込まない。こじつけは逆効果で信頼を損なう。
避けるべき: 当たり障りのない空虚な励まし、および今回のトピックと無関係な趣味・関心の持ち込み。"""

    print(f"[FutureSearch] 💛 Gemini: generating structured happiness advice...")
    response = client.models.generate_content(model=MODEL_NAME_HEAVY, contents=prompt)
    return response.text.strip()


def _is_template_echo(text: str) -> bool:
    """モデルが回答ではなくテンプレート/質問集を生成したことを検出。

    2パターンを検出:
    1. 括弧描写エコー: (…を\\d+点…) などのプロンプト説明文をそのまま出力
    2. 質問集生成: 「〜を1文で書け」「〜せよ」などの指示文をモデルが自作 (3行以上)
    """
    # パターン1: 括弧描写エコー
    bracket_patterns = [
        r'（.*?を\d+点.*?）',
        r'（.*?について.*?省略.*?）',
        r'（.*?根拠付きで.*?）',
        r'（.*?最悪シナリオ.*?）',
        r'（.*?励まし不要.*?）',
    ]
    if any(re.search(p, text) for p in bracket_patterns):
        return True

    # パターン2: 質問集生成（指示文を3行以上出力 = 回答せずに問題集を作成している）
    instruction_hits = re.findall(
        r'(書け|せよ|挙げよ|提案せよ|解説せよ|予測せよ|定義せよ|暴露せよ)。',
        text,
    )
    if len(instruction_hits) >= 3:
        return True

    return False


def _call_davidau_section(section_prompt: str) -> str:
    """DavidAU に1セクション分だけ生成させる。
    質問文（書け/せよで終わる行）や短すぎる出力は空文字を返す。"""
    raw = _call_ollama(section_prompt, timeout=HAPPINESS_TIMEOUT, model=HAPPINESS_MODEL)
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    raw = raw.split('<|endoftext|>')[0].strip()
    raw = _deduplicate_paragraphs(raw)

    # 回答ではなく質問を生成した場合は棄却
    if _is_template_echo(raw) or len(raw) < 30:
        return ""
    return raw


def _generate_uncensored_take(topic: str, report: str, user_context: str) -> str:
    """DavidAU (happy-claude) が「普通のAIには言えないこと」を4回の個別呼び出しで生成。

    1プロンプトで4セクションを要求するとモデルがテンプレートを自作する問題を回避。
    各セクションを独立した最小プロンプトで呼び出し、失敗したセクションのみGeminiで補完。
    """
    today = datetime.date.today().isoformat()
    context_block = user_context if user_context else "ユーザープロファイル未設定。一般的な知識労働者として推定。"

    # コンテキストを短く絞る（各呼び出しが軽量になるように）
    ctx = context_block[:400]
    rep = report[:400]
    base = f"今日: {today}。トピック: {topic}。\nユーザー: {ctx}\nリサーチ要約: {rep}\n\n"

    # 4セクションを個別プロンプトで定義
    sections = [
        (
            "不快な真実",
            base + f"「{topic}」についてこのユーザーの状況を踏まえ、"
                   "普通のAIが配慮して言わない不快な本音を日本語で2文だけ述べよ。",
        ),
        (
            "逆張り評価",
            base + f"「{topic}」に関する世間の常識・専門家の定説への"
                   "逆張り反論を日本語で根拠付き2文で述べよ。",
        ),
        (
            "グレーゾーン戦略",
            base + f"「{topic}」に関して他のAIが推奨しにくいグレーゾーン手法を"
                   "日本語で具体的に2文で述べよ。",
        ),
        (
            "辛口診断",
            base + "このユーザーが今の路線を続けた場合の3年後の最悪シナリオを"
                   "日本語で2文だけ述べよ。",
        ),
    ]

    results = []
    fallback_needed = []

    for title, prompt in sections:
        print(f"[FutureSearch] 🔥 DavidAU [{title}]...")
        output = _call_davidau_section(prompt)
        if output:
            print(f"[FutureSearch] 🔥   ✅ {len(output)} chars")
            results.append((title, output))
        else:
            print(f"[FutureSearch] 🔥   ⚠️ rejected → Gemini fallback")
            fallback_needed.append(title)

    # 失敗セクションをGeminiで一括補完
    if fallback_needed:
        section_list = "\n".join(
            f"**{t}**: 2文で具体的に答えよ。" for t in fallback_needed
        )
        fallback_prompt = (
            f"あなたは率直で正直なAIアドバイザーです。今日: {today}\n"
            f"トピック: {topic}\nユーザー: {ctx}\nリサーチ: {rep}\n\n"
            f"以下のセクションを日本語で答えてください:\n{section_list}"
        )
        resp = client.models.generate_content(model=MODEL_NAME_HEAVY, contents=fallback_prompt)
        fb_text = resp.text.strip()
        # フォールバック結果を各セクションに割り当て（各タイトル自身のセクションを抽出。
        # 抽出できない場合は全文重複を避けるため最初の1件のみ全文を割り当てる）
        used_full_fallback = False
        for title in fallback_needed:
            m = re.search(
                r'\*\*' + re.escape(title) + r'\*\*[:：]?\s*(.*?)(?=\n\*\*|\Z)',
                fb_text, re.DOTALL
            )
            if m:
                results.append((title, m.group(1).strip()))
            elif not used_full_fallback:
                results.append((title, fb_text))
                used_full_fallback = True
            else:
                results.append((title, "(生成に失敗しました)"))

    # セクション順序を統一して組み立て
    order = {t: i for i, (t, _) in enumerate([
        ("不快な真実", ""), ("逆張り評価", ""), ("グレーゾーン戦略", ""), ("辛口診断", "")
    ])}
    results.sort(key=lambda x: order.get(x[0], 99))

    body = "\n\n".join(f"**{title}**\n{text}" for title, text in results)
    return f"### 🔥 普通のAIには言えないこと\n\n{body}"


# ---------------------------------------------------------
# Step 6: Save to Obsidian
# ---------------------------------------------------------

def _save_to_obsidian(topic: str, report: str) -> str:
    """レポートをObsidianボールトに保存する。ファイル名を返す。"""
    from tools.tool_obsidian import tool_create_note

    today = datetime.date.today().strftime("%Y-%m-%d")
    safe_topic = re.sub(r'[\\/:*?"<>|]', '_', topic)[:50].strip()
    filename = f"FutureSearch_{today}_{safe_topic}"
    summary = f"未来シナリオ分析: {topic} ({today})"

    result = tool_create_note(filename=filename, content=report, summary=summary)
    print(f"[FutureSearch] 📝 Obsidian: {result}")
    return filename


# ---------------------------------------------------------
# Step 6: Discord notification (optional webhook)
# ---------------------------------------------------------

def _notify_discord(topic: str, report: str, filename: str) -> None:
    """Discord Webhookにサマリーを通知する。URLが未設定の場合はスキップ。"""
    if not DISCORD_WEBHOOK_URL:
        print("[FutureSearch] ℹ️ DISCORD_WEBHOOK_URL not set, skipping notification.")
        return

    # エグゼクティブサマリー部分を抽出
    marker = "## エグゼクティブサマリー"
    idx = report.find(marker)
    if idx != -1:
        snippet_raw = report[idx + len(marker):].strip()
        # 次のセクションで切る
        next_section = snippet_raw.find("\n##")
        snippet = snippet_raw[:next_section].strip() if next_section != -1 else snippet_raw[:400]
    else:
        snippet = report[:400]

    payload = {
        "embeds": [
            {
                "title": f"🔭 未来シナリオ分析完了: {topic}",
                "description": snippet[:800] + "\n\n_続きはObsidianで確認_",
                "color": 0x5865F2,
                "footer": {
                    "text": f"[[{filename}]] | {datetime.date.today().isoformat()}"
                },
            }
        ]
    }

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        print("[FutureSearch] 📣 Discord notification sent.")
    except Exception as e:
        print(f"[FutureSearch] ⚠️ Discord notification failed: {e}")


# ---------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------

def run_future_search(topic: str = "", depth: str = DEFAULT_DEPTH,
                      uncensored: bool = False, progress_cb=None) -> str:
    """未来シナリオ分析: 英訳 → Tavily検索 → Gemini 10視点マルチレンズ推論 → 個人化。

    Args:
        topic: 分析トピック (日本語可。内部で英語検索クエリに変換)。
        depth: "quick" / "standard" / "deep"。ソース数と詳細度を決める。
        uncensored: True で DavidAU 辛口診断を追加生成 (遅いので明示選択制)。
        progress_cb: 各段階で呼ばれる同期コールバック fn(msg:str)。None なら無視。
    """
    def _progress(msg: str):
        print(f"[FutureSearch] {msg}")
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass  # 進捗表示の失敗は本処理を止めない

    if not topic or not topic.strip():
        return "Error: トピックが空です。"
    topic = topic.strip()

    # 日次実行上限 (Gemini/Tavily 消費前にチェック。文字列返却は既存エラー規約)
    try:
        _RUN_COUNTER.check_and_increment()
    except GeminiBudgetExceeded:
        return (f"Error: 未来検索の1日あたり実行上限"
                f"({FUTURE_SEARCH_DAILY_LIMIT}回)に達しました。明日再度お試しください。")
    if depth not in DEPTH_CONFIG:
        depth = DEFAULT_DEPTH
    n_sources = DEPTH_CONFIG[depth]["sources"]

    print(f"\n[FutureSearch] 🚀 Start: '{topic}' (depth={depth}, uncensored={uncensored})")

    # Step 1: 英語検索クエリに変換
    _progress("🌐 検索クエリを英語に変換中...")
    en_query = _translate_to_english_query(topic)
    if en_query != topic:
        print(f"[FutureSearch] 🌐 EN query: '{en_query}'")

    # Step 2: Tavily で英語ソース収集
    _progress(f"🔍 英語ソースを検索中 ({n_sources}件)...")
    sources = _search_topic(en_query, max_results=n_sources)
    if not sources:
        return f"Error: Tavily検索でソースが取得できませんでした。トピック: '{topic}'"
    print(f"[FutureSearch] 📰 Found {len(sources)} sources.")

    # Step 3: ユーザー文脈を読み込み (コア分析にも注入して個人化)
    _progress("👤 ユーザー文脈を読み込み中...")
    user_context = _load_user_context()

    # Step 4: Gemini 10視点マルチレンズ推論
    _progress(f"🧠 {len(AGENT_PERSONAS)}視点で多角的に分析中...")
    report = _multi_lens_analysis(topic, sources, user_context, depth)

    # Step 4.5: 本文の [SOURCE N] 参照に対応する参考ソース一覧をコード側で付与。
    # URL は Tavily の実データから機械生成するため捏造ゼロ。これにより
    # 従来「[SOURCE N] だけでリンク先が辿れない」空参照問題を解消する。
    sources_section = _format_sources_section(sources)
    if sources_section:
        report = report + "\n\n" + sources_section

    # Step 5: 幸福最大化アドバイス (個人化)
    _progress("💛 幸福最大化アドバイスを生成中...")
    happiness_advice = _generate_happiness_advice(topic, report, user_context)
    main_report = report + "\n\n---\n\n" + happiness_advice

    # Step 6: メインレポートを即時保存 (辛口を待たない)
    _progress("💾 Obsidianに保存中...")
    filename = _save_to_obsidian(topic, main_report)
    _notify_discord(topic, main_report, filename)

    # Step 7: (任意) DavidAU 辛口診断 — 遅いので uncensored=True の時のみ
    uncensored_filename = None
    if uncensored:
        _progress("🔥 辛口診断を生成中 (数分かかります)...")
        unc = _generate_uncensored_take(topic, report, user_context)
        uncensored_filename = _save_to_obsidian(topic + " [辛口診断]", unc)
        print(f"[FutureSearch] 🔥 Uncensored report saved: [[{uncensored_filename}]]")

    preview = "\n".join(main_report.split("\n")[:25])
    result = (
        f"✅ FutureSearch完了 (depth={depth}, {len(sources)}ソース)\n"
        f"メインレポート: [[{filename}]]\n"
    )
    if uncensored_filename:
        result += f"辛口診断: [[{uncensored_filename}]]\n"
    result += f"\n{preview}\n..."
    return result


