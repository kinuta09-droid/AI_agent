# -*- coding: utf-8 -*-
"""
Future Search Tool — Multi-agent futures research system.

Flow:
  1. Tavily で英語ソースを収集
  2. 10体のOllamaエージェントが異なる視点で意見生成（引用なしは棄却）
  3. Gemini 2.0 Flash が全意見を統合し日本語レポートを生成
  4. Obsidianに保存 + Discordに要約通知（Webhook設定時）
"""

import os
import sys
import re
import json
import datetime
import requests
import concurrent.futures

from lib.lib_shared import (
    client,
    tavily_client,
    MODEL_NAME_HEAVY,
    VAULT_ROOT,
    WORK_DIR,
    retry,
)

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
# 10エージェント用: 速度重視・並列実行
OLLAMA_MODEL = "qwen3:1.7b"          # 1行変更で切り替え可能
OLLAMA_BASE_URL = "http://localhost:11434"
AGENT_TIMEOUT = 90                  # seconds per agent call

# 幸福最大化AI用: DavidAU Claude 4.6蒸留版 (Qwen3.5-9B IQ4_XS)
# ollama create happy-claude -f models/davidau-claude/Modelfile で登録済み
HAPPINESS_MODEL = "happy-claude"
HAPPINESS_TIMEOUT = 300             # 9Bモデル on CPU: 最大5分

SEARCH_RESULTS = 10                 # Tavily で取得するソース数 (エージェント多様性のため増量)
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


def _call_ollama(prompt: str, timeout: int = AGENT_TIMEOUT, model: str = None) -> str:
    """ローカルOllama APIを呼び出す。失敗時はエラー文字列を返す。

    model: 省略時は OLLAMA_MODEL (10エージェント用)。
           HAPPINESS_MODEL などを渡すことで別モデルに切り替え可能。
    """
    effective_model = model or OLLAMA_MODEL
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

def _search_topic(topic: str, max_results: int = SEARCH_RESULTS) -> list:
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
# Step 2: Single agent analysis
# ---------------------------------------------------------

def _run_single_agent(persona: dict, sources: list, topic: str) -> dict:
    """1エージェントがソースを読んで意見を生成する。引用なしは棄却。"""
    if not sources:
        return {
            "agent_id": persona["id"],
            "agent_name": persona["name"],
            "opinion": "[SKIP] No sources available.",
            "citations": [],
            "valid": False,
        }

    sources_text = "\n\n".join(
        f"SOURCE [{i + 1}] — {s['title']}\nURL: {s['url']}\n{s['content'][:600]}"
        for i, s in enumerate(sources)
    )

    prompt = f"""You are a futures analyst. Your analytical perspective: {persona['lens']}

TOPIC: {topic}

SOURCES:
{sources_text}

Instructions:
1. Read the sources carefully.
2. Write a focused 150-200 word analysis from your unique perspective.
3. You MUST reference at least one source using [SOURCE N] notation.
   Opinions without any [SOURCE N] citation will be REJECTED.
4. Be direct, specific, and opinionated. Avoid vague generalities.

Respond in EXACTLY this format:

OPINION:
[Your analysis here, with [SOURCE N] citations inline]

CITATIONS:
[List each [SOURCE N] you cited, one per line]"""

    raw = _call_ollama(prompt)

    # Parse opinion and citations blocks
    opinion = ""
    citations = []
    if "OPINION:" in raw:
        parts = raw.split("CITATIONS:", 1)
        opinion = parts[0].replace("OPINION:", "").strip()
        if len(parts) > 1:
            citations = [c.strip() for c in parts[1].strip().split("\n") if c.strip()]
    else:
        # Fallback: use raw output as opinion
        opinion = raw[:600]

    # Validate: モデルによって表記がばらつく。全パターンを網羅:
    #   [SOURCE 1], (Source 1), SOURCE [1], source1, Source1 etc.
    has_citation = bool(re.search(r'(?i)\bsource\s*[\[\(]?\s*\d+', opinion))
    if not has_citation or not opinion or opinion.startswith("[ERROR]"):
        return {
            "agent_id": persona["id"],
            "agent_name": persona["name"],
            "opinion": opinion[:400] if opinion else "[SKIP] Empty response.",
            "citations": citations,
            "valid": False,
        }

    return {
        "agent_id": persona["id"],
        "agent_name": persona["name"],
        "opinion": opinion,
        "citations": citations,
        "valid": True,
    }


# ---------------------------------------------------------
# Step 3: Parallel multi-agent execution
# ---------------------------------------------------------

def _sources_for_agent(sources: list, agent_index: int, window: int = 6) -> list:
    """エージェントごとにソースをローテーションして返す。
    10ソース × 10エージェントで各エージェントが異なる6ソースを受け取り、
    同じ情報に依存しない多様な分析を促す。
    """
    n = len(sources)
    if n <= window:
        return sources  # ソースが少ない場合は全件渡す
    step = max(1, n // len(AGENT_PERSONAS))
    start = (agent_index * step) % n
    return [sources[(start + j) % n] for j in range(window)]


def _run_all_agents(topic: str, sources: list) -> list:
    """10体のエージェントをThreadPoolExecutorで並列実行する。
    各エージェントには異なるソースサブセットを渡すことで分析の多様性を高める。
    """
    print(f"[FutureSearch] 🤖 Launching {len(AGENT_PERSONAS)} agents in parallel...")
    print(f"[FutureSearch]    Sources: {len(sources)} total, 6/agent (rotated)")
    results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(AGENT_PERSONAS)) as executor:
        future_to_persona = {
            executor.submit(
                _run_single_agent,
                persona,
                _sources_for_agent(sources, i),
                topic,
            ): persona
            for i, persona in enumerate(AGENT_PERSONAS)
        }
        for future in concurrent.futures.as_completed(future_to_persona):
            persona = future_to_persona[future]
            try:
                result = future.result(timeout=AGENT_TIMEOUT + 15)
                icon = "✅" if result["valid"] else "⚠️"
                print(f"  {icon} [{persona['name']}] done (valid={result['valid']})")
                results.append(result)
            except Exception as e:
                print(f"  ❌ [{persona['name']}] failed: {e}")
                results.append({
                    "agent_id": persona["id"],
                    "agent_name": persona["name"],
                    "opinion": f"[ERROR] {e}",
                    "citations": [],
                    "valid": False,
                })

    # Restore original persona order for readability
    order = {p["id"]: i for i, p in enumerate(AGENT_PERSONAS)}
    results.sort(key=lambda r: order.get(r["agent_id"], 99))
    return results


# ---------------------------------------------------------
# Step 4: Gemini synthesis → Japanese report
# ---------------------------------------------------------

@retry(max_retries=3, initial_delay=5)
def _synthesize_with_gemini(topic: str, agent_results: list, sources: list) -> str:
    """全エージェント意見をGemini Heavy に渡して日本語レポートを生成。"""
    today = datetime.date.today().isoformat()
    valid_count = sum(1 for r in agent_results if r["valid"])

    all_opinions = "\n\n".join(
        f"### {r['agent_name']} ({r['agent_id']})"
        + (" [INVALID - no citation]" if not r["valid"] else "")
        + f"\n{r['opinion']}"
        for r in agent_results
    )

    source_list = "\n".join(
        f"- [{s['title']}]({s['url']})" for s in sources
    )

    prompt = f"""あなたは未来研究の専門家です。今日は {today} です。

## 分析トピック
{topic}

## 10体のAIエージェントによる多角的分析（英語ソースベース）
{all_opinions}

## 使用英語ソース
{source_list}

## 指示
上記の10体のエージェント意見を統合し、以下の構造で**日本語**レポートを作成してください。
有効エージェント（引用あり）の意見を優先しつつ、無効意見も参考として扱うこと。

---
# 🔭 未来シナリオ分析: {topic}
**生成日**: {today}

## エグゼクティブサマリー
（3〜5文で全体像を要約）

## 未来シナリオ

### 🟢 高蓋然性シナリオ（60〜80%）
（2〜3シナリオ。根拠となるエージェント意見を引用して説明）

### 🟡 中蓋然性シナリオ（30〜60%）
（2シナリオ。相矛盾する視点を明示）

### 🔴 低蓋然性・テールリスク（10〜30%）
（1〜2シナリオ。過小評価されているリスクを強調）

## 主要な不確実性要因
（箇条書き5〜7項目）

## 弱者・見落とされがちな影響
（弱者視点エージェントの洞察を特に拡張して記述）

## 推奨アクション・注目ポイント
（個人・組織が今すぐ取れる具体的アクション3〜5個）

## 分析メタデータ
- 有効エージェント数: {valid_count}/{len(AGENT_PERSONAS)}
- 無効（引用なし）: {len(AGENT_PERSONAS) - valid_count}件
- ソース数: {len(sources)}件
- モデル: {OLLAMA_MODEL} × Gemini

---

重要: 蓋然性スコアはエージェント意見の根拠に基づき設定し、
各シナリオに1文の根拠説明を必ず付けること。"""

    response = client.models.generate_content(
        model=MODEL_NAME_HEAVY,
        contents=prompt,
    )
    return response.text.strip()


# ---------------------------------------------------------
# Step 4.5: Load user context (Profile + Interests)
# ---------------------------------------------------------

def _load_user_context() -> str:
    """User_Profile.md と User_Interests.md を読み込んでユーザー文脈を返す。
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
上記のすべてを統合し、**このユーザーの幸福を最大化するために、今この瞬間から取れる最善の行動**を提示せよ。
正直に、率直に、このユーザーの現実から目を逸らさずに答えること。

---

## 🧭 幸福最大化アドバイザーの提言

### このユーザーの現在地
（未来シナリオの中でこのユーザーが置かれた状況を率直に評価。強みと脆弱性を隠さず。2〜3文）

### 今すぐ取れる行動
「長期投資」と「即時快楽」を分けて提示する。どちらを選ぶかはあなた次第。

#### 🏗️ 長期投資型（未来の幸福を積み上げる）TOP2
1. **[行動タイトル]**
   - なぜあなたの幸福に直結するか: ...
   - 今日できる最初の一歩: ...

2. **[行動タイトル]**
   - なぜあなたの幸福に直結するか: ...
   - 今日できる最初の一歩: ...

#### ⚡ 即時快楽型（今この瞬間を最高にする）TOP1
1. **[行動タイトル]**
   - なぜ今すぐ楽しいか: ...
   - 今日できる最初の一歩: ...

#### ⚖️ トレードオフの正直な評価
（長期投資と即時快楽が衝突する場合、その構造を明示。どちらが「正解」かは言わない）

### あなたが陥りやすいトラップ
（このユーザーの文脈から見えるリスク。一般論禁止。具体的かつ正直に。2〜3項目）

### 3ヶ月後に自分に問う問い
（行動の成果を測る、このユーザー固有の問い。3項目）

---

絶対禁止: 一般論・当たり障りのないアドバイス・「〜しましょう」調の空虚な励まし。
必須: ユーザーのコンテキスト（興味・スキル・状況・価値観）に直接紐付いた具体的提言。"""

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
        # フォールバック結果を各セクションに割り当て（簡易: 全文を末尾に追加）
        for title in fallback_needed:
            results.append((title, fb_text))

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

def run_future_search(topic: str = "", skip_uncensored: bool = False) -> str:
    """
    英語圏リサーチ → 10体マルチエージェント議論 → 日本語レポート生成。

    Ollama ({OLLAMA_MODEL}) で各エージェントが英語ソースを分析し、
    Gemini Heavy が統合レポートを日本語で生成する。

    Args:
        skip_uncensored: True にすると DavidAU 辛口診断をスキップ（高速化）。
    """
    if not topic:
        topic = input("分析トピック (英語推奨): ").strip()
    if not topic:
        return "Error: トピックが空です。"

    print(f"\n[FutureSearch] 🚀 Start: '{topic}'")
    print(f"[FutureSearch] 🤖 Ollama model: {OLLAMA_MODEL}")

    # Step 1: Collect English sources
    print("[FutureSearch] 🔍 Searching English sources via Tavily...")
    sources = _search_topic(topic, max_results=SEARCH_RESULTS)
    if not sources:
        return f"Error: Tavily検索でソースが取得できませんでした。トピック: '{topic}'"
    print(f"[FutureSearch] 📰 Found {len(sources)} sources.")

    # Step 2 & 3: Run all agents in parallel
    agent_results = _run_all_agents(topic, sources)
    valid_count = sum(1 for r in agent_results if r["valid"])
    print(f"[FutureSearch] ✅ Valid opinions: {valid_count}/{len(AGENT_PERSONAS)}")

    if valid_count == 0:
        # All agents failed — likely Ollama is down; still attempt Gemini with raw output
        print("[FutureSearch] ⚠️ No valid agent opinions. Proceeding with raw output.")

    # Step 4: Gemini synthesis
    print("[FutureSearch] ✍️ Synthesizing Japanese report via Gemini...")
    report = _synthesize_with_gemini(topic, agent_results, sources)

    # Step 4.5: Load user context
    print("[FutureSearch] 👤 Loading user context...")
    user_context = _load_user_context()
    if user_context:
        print("[FutureSearch] 👤 User context loaded.")
    else:
        print("[FutureSearch] 👤 No user profile found, using generic context.")

    # Step 5a: Gemini → 構造化幸福アドバイス
    print("[FutureSearch] 💛 Generating happiness-maximizing advice (Gemini)...")
    happiness_advice = _generate_happiness_advice(topic, report, user_context)
    main_report = report + "\n\n---\n\n" + happiness_advice

    # Step 6a: メインレポートをObsidianに即時保存（DavidAUを待たない）
    filename = _save_to_obsidian(topic, main_report)
    if skip_uncensored:
        print(f"[FutureSearch] ✅ Main report saved. 辛口診断はスキップ。")
    else:
        print(f"[FutureSearch] ✅ Main report saved. Starting DavidAU uncensored section...")

    # Step 7: Discord notification（メインレポートで通知）
    _notify_discord(topic, main_report, filename)

    # Step 5b: DavidAU → 辛口セクション（別ファイル）— スキップ可能
    uncensored_filename = None
    if not skip_uncensored:
        print("[FutureSearch] 🔥 Generating uncensored take (DavidAU)...")
        uncensored = _generate_uncensored_take(topic, report, user_context)
        uncensored_filename = _save_to_obsidian(topic + " [辛口診断]", uncensored)
        print(f"[FutureSearch] 🔥 Uncensored report saved: [[{uncensored_filename}]]")

    # Return preview (first 25 lines of main report)
    preview_lines = main_report.split("\n")[:25]
    preview = "\n".join(preview_lines)
    result = (
        f"✅ FutureSearch完了\n"
        f"メインレポート: [[{filename}]]\n"
    )
    if uncensored_filename:
        result += f"辛口診断: [[{uncensored_filename}]]\n"
    result += f"有効エージェント: {valid_count}/{len(AGENT_PERSONAS)}\n\n{preview}\n..."
    return result


