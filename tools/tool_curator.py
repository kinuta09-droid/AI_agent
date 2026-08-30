# -*- coding: utf-8 -*-
"""
The Curator -- Interest-driven knowledge acquisition system.
Reads user context, generates topic suggestions via Gemini,
deep-searches via Tavily, and saves curated knowledge to the vault.
"""

import os
import sys
import datetime
import re
import random
import concurrent.futures
import json
import time

sys.stdout.reconfigure(encoding='utf-8')

from lib.lib_shared import (
    client,
    tavily_client,
    MODEL_NAME,
    VAULT_ROOT,
    WORK_DIR,
    retry,
    safe_json_loads,
    EDITORIAL_STYLE_RULES,
)

from tools.tool_obsidian import (
    _extract_and_integrate_sections,
    _determine_parent_moc,
    _strip_section_or_warn,
    PERSONAL_INFO_SECTION_HEADING,
)
from lib.lib_obsidian_cli import run_obsidian_cli

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

KB_DIR_NAME = "Knowledge_Base"
KB_DIR = os.path.join(WORK_DIR, "30_Main", KB_DIR_NAME)
PROFILE_DIR = os.path.join(VAULT_ROOT, "00_System")
PROFILE_PATH = os.path.join(PROFILE_DIR, "User_Profile.md")
TEMPLATE_PATH = os.path.join(VAULT_ROOT, "99_Assets", "Templates", "Tpl_Permanent.md")
CACHE_PATH = os.path.join(WORK_DIR, "external_trends_cache.json")


# ---------------------------------------------------------
# Function 1: Context Reader
# ---------------------------------------------------------

def read_main_context(max_tokens: int = 30000) -> str:
    """30_Main + Knowledge_Base + User_Interests + User_Profile を読み込み、
    コンテキスト文字列を構築する。KB優先→更新日順。"""
    print(f"[Curator] 📚 Reading user context (budget: {max_tokens} chars)...")

    context_parts = []
    char_count = 0

    # 1. User_Interests.md（常に含める）
    interests_path = os.path.join(VAULT_ROOT, "00_System", "User_Interests.md")
    if os.path.exists(interests_path):
        try:
            with open(interests_path, 'r', encoding='utf-8') as f:
                interests = f.read()
            context_parts.append(f"## User Interests\n{interests}\n")
            char_count += len(interests)
        except Exception:
            pass

    # 2. User_Profile.md（存在すれば含める）
    if os.path.exists(PROFILE_PATH):
        try:
            with open(PROFILE_PATH, 'r', encoding='utf-8') as f:
                profile = f.read()
            profile = _strip_section_or_warn(profile, PERSONAL_INFO_SECTION_HEADING, "Curator")
            context_parts.append(f"## User Profile (Preferences)\n{profile}\n")
            char_count += len(profile)
        except Exception:
            pass

    # 2b. User_Memory.md（会話からの長期記憶、存在すれば含める）
    try:
        from tools.tool_user_memory import get_memory_block
        memory = get_memory_block()
        if memory:
            context_parts.append(f"## User Memory (会話からの長期記憶)\n{memory}\n")
            char_count += len(memory)
    except ImportError:
        pass

    # 2c. 実測エンゲージメント（パーソナライズ Phase2、存在すれば含める）
    # 「宣言された興味」ではなく「実際に開いた/クリックしたもの」を反映する。
    # データが無ければ空文字が返り、既存のコンテキスト形は変わらない。
    try:
        from lib.preference_weights import engagement_context_block
        engagement = engagement_context_block()
        if engagement:
            context_parts.append(f"## 実測エンゲージメント（直近30日）\n{engagement}\n")
            char_count += len(engagement)
    except ImportError:
        pass

    # 3. ファイル収集: KB優先、次に30_Main
    file_list = []  # (path, mtime, source)

    if os.path.exists(KB_DIR):
        for fname in os.listdir(KB_DIR):
            if fname.endswith('.md'):
                fp = os.path.join(KB_DIR, fname)
                file_list.append((fp, os.path.getmtime(fp), "KB"))

    main_dir = os.path.join(WORK_DIR, "30_Main")
    if os.path.exists(main_dir):
        for fname in os.listdir(main_dir):
            fp = os.path.join(main_dir, fname)
            if fname.endswith('.md') and os.path.isfile(fp):
                file_list.append((fp, os.path.getmtime(fp), "Main"))

    # KB優先 → 更新日降順
    file_list.sort(key=lambda x: (0 if x[2] == "KB" else 1, -x[1]))

    # 4. バジェットまで読み込み
    for fp, mtime, source in file_list:
        if char_count >= max_tokens:
            break
        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()[:500]
            fname = os.path.basename(fp)
            snippet = f"### [{source}] {fname}\n{content}\n\n"
            context_parts.append(snippet)
            char_count += len(snippet)
        except Exception:
            pass

    print(f"[Curator] ✅ Context built: {char_count} chars from {len(file_list)} files")
    return "\n".join(context_parts)


# ---------------------------------------------------------
# Function 1.5: External Trend Fetcher (Bezos's Signal)
# ---------------------------------------------------------

def fetch_external_trends(max_results: int = 5) -> str:
    """Tavily APIで最新のテクノロジー・社会トレンドを取得し、
    Geminiプロンプトに注入するための文字列を返す。"""
    print("[Curator] 🌍 Fetching external trends...")
    
    # Check cache
    if os.path.exists(CACHE_PATH):
        try:
            mtime = os.path.getmtime(CACHE_PATH)
            if time.time() - mtime < 86400: # 24 hours
                with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    print(f"[Curator] ⚡ Using cached external trends ({len(data.get('content', ''))} chars)")
                    return data.get("content", "")
        except Exception as e:
            print(f"[Curator] ⚠️ Cache read failed: {e}")

    try:
        response = tavily_client.search(
            query="major technology and social trends 2026",
            search_depth="basic",
            max_results=max_results,
            topic="news",
            days=7,
        )
        results = response.get("results", [])
        if not results:
            print("[Curator] ⚠️ No external trends found, continuing without injection")
            return ""
        trend_text = "\n".join(
            f"{i}. {r['title']}: {r['content'][:200]}"
            for i, r in enumerate(results, 1)
        )
        print(f"[Curator] 🌍 Fetched {len(results)} external trends from API")
        
        # Save cache
        try:
            with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump({"content": trend_text, "timestamp": time.time()}, f, ensure_ascii=False)
        except Exception as e:
            print(f"[Curator] ⚠️ Cache write failed: {e}")
            
        return trend_text
    except Exception as e:
        print(f"[Curator] ⚠️ External trend fetch failed: {e}")
        return ""


# ---------------------------------------------------------
# Function 2: Suggestion Generator (Entropy Injector v4.0)
# ---------------------------------------------------------

@retry(max_retries=5, initial_delay=5)
def get_curation_suggestions(context: str, mode: str = "normal",
                             rejected_topics: list = None,
                             reject_count: int = 0,
                             external_trends: str = "") -> dict:
    """Gemini にコンテキストを送り、トピック提案を JSON で返す。
    normal: Entropy Injector 3スロット + A/B質問(ランダム)、setup: トピック10-15
    rejected_topics: 直前に拒否されたトピック名リスト（再提案時に除外）
    reject_count: 連続拒否回数（多いほど entropy 比率を上げる）
    external_trends: Tavily から取得した最新トレンド文字列"""
    print(f"[Curator] 🤖 Generating suggestions (mode={mode}, rejects={reject_count})...")

    # 拒否トピックの除外指示を構築
    rejected_block = ""
    if rejected_topics:
        rejected_list = "\n".join(f"- {t}" for t in rejected_topics)
        rejected_block = f"""
【拒否されたトピック（以下と同じ・類似のトピックは提案しないこと）】
{rejected_list}

ユーザーはこれらに興味を示しませんでした。全く異なる切り口で、かつユーザーの根本的な関心に近いものを再提案してください。
"""

    if mode == "setup":
        prompt = f"""あなたはパーソナル・ナレッジ・エクスプローラーです。
以下のユーザーの興味関心と既存ノートに基づき、探索すべきナレッジトピックを10〜15個提案してください。

{context}
{rejected_block}
以下のJSON形式のみで出力してください:
{{
    "topics": [
        {{"title": "トピック名（日本語）", "branch": "deepen|expand|jump", "source_note": "派生元のノート名（あれば）", "description": "なぜ面白いか1文（日本語）", "search_query": "Tavily検索クエリ（英語推奨）"}}
    ]
}}

ルール:
- トピックは具体的でニッチに（「AI」ではなく「マルチエージェントオーケストレーションのパターン」）
- Knowledge_Baseセクションにあるトピックは避ける
- 【ジャンル分散・最重要】AI/テクノロジーに偏禁止。ユーザーの興味リストにある全カテゴリー（政治、社会、介護、歴史、文化など）からバランスよく提案する。10〜15個のうちAI/テック系は最大3〜4個まで
- branch は以下の3種を混ぜる:
  - "deepen": 既存ノートの専門性を深掘りする（例: BTC → ライトニングネットワークの詳細）
  - "expand": 同カテゴリーの隣接領域へ広げる（例: BTC → 金（ゴールド）の歴史）
  - "jump": 全く別ジャンルだが構造的に共通点がある概念（例: BTC → 生物学における分散型システム）
- source_note は Knowledge_Base セクションにある派生元のノート名（なければ空文字）
- search_query はTavily Web検索に最適化（英語推奨、具体的なフレーズ）
"""
    else:
        # --- Entropy Injector v4.0: Normal Mode ---

        # A/B質問は3回に1回程度ランダムで出す（再提案時は出さない）
        include_ab = reject_count == 0 and random.randint(1, 3) == 1
        # 反証モード: A/B質問のうち25%は Devil's Advocate 型
        is_contrarian = include_ab and random.randint(1, 4) == 1

        ab_block = ""
        if include_ab:
            if is_contrarian:
                ab_block = """,
    "ab_question": {{
        "question": "ユーザーの既存の信念・前提に挑戦する質問文（日本語）",
        "option_a": "ユーザーの現在の立場を肯定する選択肢（日本語）",
        "option_b": "ユーザーの現在の立場に反論する選択肢（日本語）",
        "type": "contrarian"
    }}"""
            else:
                ab_block = """,
    "ab_question": {{
        "question": "嗜好や価値観を探る質問文（日本語）",
        "option_a": "選択肢A（日本語）",
        "option_b": "選択肢B（日本語）",
        "type": "normal"
    }}"""

        # 外部トレンド注入ブロック (Bezos's Signal)
        trend_block = ""
        if external_trends:
            trend_block = f"""
【外部刺激（直近1週間の主要テクノロジー・社会トレンド）】
以下は世界で今話題のトピックです。Slot 2 (Bridge) の提案では、これらの中から1つ以上を
ユーザーの既存の興味と掛け合わせて活用してください。Slot 1, 3 でも参考にして構いません。

{external_trends}
"""

        # スロット配分（拒否カウントに応じて調整）
        if reject_count >= 2:
            slot_instruction = """【3スロット配分（拒否が多いため大胆に）】
- Slot 1 (Consistent): なし（安定志向は求められていない）
- Slot 2 (Bridge): 1個
- Slot 3 (Entropy): 2個（意外性を最大化）
ユーザーが何度も拒否しています。常識を破る大胆な提案をしてください。"""
        elif reject_count == 1:
            slot_instruction = """【3スロット配分（拒否データあり、やや冒険的に）】
- Slot 1 (Consistent): 1個
- Slot 2 (Bridge): 1個（外部トレンドを意識）
- Slot 3 (Entropy): 1個（前回より意外性のあるものを）"""
        else:
            slot_instruction = """【3つのスロット（必ず各1つずつ）】
1. 🔬 Consistent（安定）: 既存ノートへの忠実度が高い深掘りトピック。ユーザーが確実に興味を持つ安全な選択。branch値は "consistent"。
2. 🌐 Bridge（橋渡し）: ユーザーの既存の興味と外部トレンドを交差させたトピック。外部刺激セクションのトレンドの中から1つを選び、ユーザーの関心と掛け合わせる。branch値は "bridge"。
3. 🚀 Entropy（飛躍）: 全く異なるドメインだが、構造的・哲学的に共通点がある意外な概念。なぜこの飛躍が妥当かの reasoning を必ず含める。branch値は "entropy"。"""

        # 反証質問のプロンプト指示
        if is_contrarian:
            ab_instruction = """- ab_question は【Devil's Advocate型】: ユーザーのUser_Profileに記録された既存の嗜好・価値観を分析し、その前提を揺さぶる質問を作成する。例: ユーザーが「実装の容易さ」を重視するなら「あえて実装が難しいが革新的な技術を選ぶべき場面はどんなとき？」。option_a はユーザーの現在の立場を肯定し、option_b は反論する立場。"""
        elif include_ab:
            ab_instruction = "- ab_question はユーザーの価値観・世界観・美意識を多角的に探る質問。技術系の質問に偏らないこと。例:「物語の結末は明確派？余韻派？」「旅先では計画派？直感派？」「歴史上の人物に会えるなら誰？」「朝型？夜型？」など、人間性・ライフスタイル・文化的嗜好を幅広く聞く"
        else:
            ab_instruction = ""

        prompt = f"""あなたはパーソナル・ナレッジ・エクスプローラー "Entropy Injector" です。
以下のユーザーの興味関心、好み、既存ノートに基づき、
3つの異なるスロットからトピックを提案してください。

{context}
{rejected_block}
{trend_block}
以下のJSON形式のみで出力してください:
{{
    "topics": [
        {{"title": "トピック名", "branch": "consistent", "source_note": "派生元ノート名", "description": "なぜ面白いか1文", "search_query": "英語検索クエリ", "reasoning": ""}},
        {{"title": "トピック名", "branch": "bridge", "source_note": "派生元ノート名", "description": "なぜ面白いか1文", "search_query": "英語検索クエリ", "reasoning": "どのトレンドとどの興味を組み合わせたか"}},
        {{"title": "トピック名", "branch": "entropy", "source_note": "派生元ノート名", "description": "なぜ面白いか1文", "search_query": "英語検索クエリ", "reasoning": "なぜこの飛躍が構造的に妥当か、詳細な推論（3〜5文）"}}
    ]{ab_block}
}}

{slot_instruction}

ルール:
- トピックは具体的でニッチに
- Knowledge_Baseセクションに既にあるトピックは避ける
- 【ジャンル分散・最重要】3つのトピックは必ず異なるジャンルから選ぶこと。AI/テクノロジーに偏禁止。ユーザーの興味リストにある全カテゴリー（政治、社会、介護、歴史、文化など）から均等に提案する。既存Knowledge_Baseでカバーが薄いジャンルを優先する
- source_note は Knowledge_Base にある派生元のノート名（拡張子なし）。なければ空文字
- search_query はTavily Web検索に最適化（英語推奨）
- contextに「実測エンゲージメント」セクションがあれば、スロット内のトピック選定の参考にする（ただし上記のジャンル分散ルールは維持し、実測ジャンルに偏らせすぎない）
- reasoning は Slot 3 (Entropy) で必須: なぜこの異分野への飛躍が構造的・哲学的に妥当なのかを3〜5文で詳細に説明する。Slot 1 (Consistent) は空文字、Slot 2 (Bridge) はどのトレンドとどの興味を掛け合わせたか簡潔に記載
{ab_instruction}
"""

    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    result = safe_json_loads(response.text)

    topic_count = len(result.get("topics", []))
    print(f"[Curator] ✅ Generated {topic_count} topic suggestions")
    return result


# ---------------------------------------------------------
# Function 3: Search & Save
# ---------------------------------------------------------

@retry(max_retries=3)
def search_and_save_topic(topic: str, search_query: str = "",
                          source_note: str = "", branch: str = "",
                          reasoning: str = "") -> str:
    """Tavily deep検索 → Gemini要約 → Knowledge_Base にテンプレート適用して保存。
    source_note: 派生元ノート名（Lineageリンク用）
    branch: consistent/bridge/entropy（スロット種別タグ用）
    reasoning: Entropy スロットのAI推論（ノート冒頭に記録）"""
    print(f"[Curator] 🔍 Deep searching: {topic}")

    query = search_query if search_query else topic

    # 1. Tavily 検索
    try:
        response = tavily_client.search(
            query=query, search_depth="advanced", max_results=5
        )
        results = response.get("results", [])
        if not results:
            return f"Error: '{topic}' の検索結果が見つかりませんでした。"
    except Exception as e:
        return f"Error searching for topic: {e}"

    raw_material = "\n\n".join([
        f"### {r['title']}\nURL: {r['url']}\n{r['content']}"
        for r in results
    ])

    # 2. Gemini でナレッジノート生成
    print(f"[Curator] 🤖 Generating knowledge note...")
    try:
        summary_prompt = f"""あなたは優秀なテック系メディアの編集長です。
忙しい40代のビジネスパーソンが、朝の5分で読み切れるレポートを書いてください。

以下の検索結果を元に、「{topic}」についての調査レポートを作成してください。

【素材】
{raw_material}

【フォーマット（厳守）】
冒頭に「# 」見出しは付けない（テンプレートが自動付与するため）。

## 💡 3行でわかる要点
- 結論を1文で。
- なぜ今これが重要かを1文で。
- 最も意外な事実、または見逃せないポイントを1文で。

## 🔍 何が起きているのか？
3〜4段落で状況を整理する。各段落は2〜3文まで。

重要な**キーワード**は太字にする。
段落間に空行を入れて、視覚的に区切ること。

## ⚡ ここがポイント
- **強み/可能性**: 箇条書きで2〜3項目。
- **課題/リスク**: 箇条書きで2〜3項目。
- 具体的な企業名、数値、プロジェクト名があれば入れる。

## 🚀 で、どうすればいい？
この知識を活かして「明日からできること」を1〜3項目で。
抽象論ではなく、具体的なアクション（ツール名、サイト名など）まで踏み込む。

    ## 📚 References
    - [記事タイトル](URL) の形式でソースを列挙。

    ---

    {EDITORIAL_STYLE_RULES}"""

        gen_response = client.models.generate_content(
            model=MODEL_NAME, contents=summary_prompt
        )
        note_content = gen_response.text.strip()
    except Exception as e:
        return f"Error generating summary: {e}"

    # 3. テンプレート用の短い要約を生成
    try:
        sum_prompt = (
            "以下のノートを1〜3行で要約してください。"
            "箇条書きではなく自然な文章で。\n\n"
            f"{note_content}"
        )
        sum_response = client.models.generate_content(
            model=MODEL_NAME, contents=sum_prompt
        )
        summary_text = sum_response.text.strip()
    except Exception:
        summary_text = "(要約の自動生成に失敗しました)"

    # 4. テンプレート適用 & 保存
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    clean_title = re.sub(r'[\\/:*?"<>|]', '_', topic)
    filename = f"{today_str}_{clean_title}.md"

    os.makedirs(KB_DIR, exist_ok=True)
    save_path = os.path.join(KB_DIR, filename)

    if os.path.exists(save_path):
        return f"既にノートが存在します: {filename}"

    # Lineage リンク + branch タグを構築
    lineage_line = ""
    if source_note:
        # 拡張子を除去してWikiリンク化
        source_clean = re.sub(r'\.md$', '', source_note)
        lineage_line = f"> Source: [[{source_clean}]]\n\n"

    # Reasoning callout (Entropy スロットのみ)
    reasoning_line = ""
    if reasoning and branch == "entropy":
        reasoning_line = (
            f"> [!TIP] AI Reasoning: Why this leap?\n"
            f"> {reasoning}\n\n"
        )

    # Combine lineage + reasoning + content so it can be extracted
    full_content = lineage_line + reasoning_line + note_content

    branch_tag = ""
    if branch and branch in ("deepen", "expand", "jump",
                              "consistent", "bridge", "entropy"):
        branch_tag = f"\n  - branch/{branch}"

    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            tpl = f.read()
            
        # 1. Template Integration (References / Sources / Lineage)
        processed_content, tpl = _extract_and_integrate_sections(full_content, tpl)
        
        tpl = tpl.replace('{{date}}', today_str)
        tpl = tpl.replace('{{title}}', clean_title)
        
        # 2. Auto-link MOC
        try:
             # Use summary + content for better classification
             check_content = (summary_text + "\n" + processed_content)[:3000]
             parent_moc = _determine_parent_moc(check_content)
             if parent_moc:
                 tpl = tpl.replace('up: "[[00_Inbox]]"', f'up: "[[{parent_moc}]]"')
                 print(f"[Curator] 🔗 Linked to MOC: [[{parent_moc}]]")
        except Exception as e:
            print(f"[Curator] ⚠️ MOC auto-link failed: {e}")

        tpl = tpl.replace(
            'このノートが言いたいことを1〜3行で書く。',
            summary_text
        )
        tpl = tpl.replace('- ここに詳細を書く', processed_content)
        tpl = tpl.replace('type/permanent', f'type/permanent\n  - source/curator{branch_tag}')
        final_content = tpl
    else:
        final_content = f"# {clean_title}\n\n{full_content}\n\n> {summary_text}"

    with open(save_path, 'w', encoding='utf-8') as f:
        f.write(final_content)

    print(f"[Curator] ✅ Saved: {save_path}")

    # Vault ルートからの相対パス。Obsidian CLI と [[wikilink]] の両方で使う。
    # KB_DIR は 30_Main/Knowledge_Base なので、この 30_Main/ を省くと Obsidian が
    # リンクを解決できず「未解決リンク = クリックすると新規作成」になる (2026-07-26 修正)。
    vault_rel_path = os.path.relpath(save_path, VAULT_ROOT).replace('\\', '/')

    # Google ドライブ上の Vault では、外部プロセスの書き込みが Obsidian のファイル監視に
    # 載らない。インデックスされないため検索にも出ず、同期にも乗らない = 「作成したと
    # 表示されたのにファイルが無い」の正体。tool_obsidian.tool_create_note と同じく
    # CLI の open で掴ませる。
    # 例外は握りつぶす: ここで送出すると @retry が3回走り、2周目以降は
    # 「既にノートが存在します」を返すため、成功した書き込みが失敗として報告される。
    try:
        run_obsidian_cli(["open", vault_rel_path])
    except Exception as e:
        print(f"[Curator] ⚠️ Failed to trigger Obsidian CLI open: {e}")

    return f"Success: Created [[{vault_rel_path}]] (Path: {save_path})"


# ---------------------------------------------------------
# Function 4: User Profile Update
# ---------------------------------------------------------

def update_user_profile(question: str, answer: str) -> str:
    """A/B嗜好の回答を User_Profile.md に追記する。"""
    print(f"[Curator] 📝 Updating User Profile...")

    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    time_str = datetime.datetime.now().strftime('%H:%M')

    entry = f"\n## {today_str} {time_str}\n- **Q**: {question}\n- **A**: {answer}\n"

    os.makedirs(PROFILE_DIR, exist_ok=True)

    if not os.path.exists(PROFILE_PATH):
        header = (
            "---\n"
            "tags:\n"
            "  - type/system\n"
            f"created: {today_str}\n"
            "---\n\n"
            "# User Profile\n\n"
            "> [!INFO] About This File\n"
            "> This file is auto-maintained by The Curator.\n"
            "> A/B preference answers are logged here to personalize future suggestions.\n"
        )
        with open(PROFILE_PATH, 'w', encoding='utf-8') as f:
            f.write(header)

    with open(PROFILE_PATH, 'a', encoding='utf-8') as f:
        f.write(entry)

    print(f"[Curator] ✅ Profile updated")
    return f"Success: Preference saved to User_Profile.md"


# ---------------------------------------------------------
# Standalone Runners (for main.py launcher)
# ---------------------------------------------------------

def run_curation() -> str:
    """対話なしで Curation を実行し、結果を返す。"""
    
    # Pre-fetch contexts in parallel (Context reading is fast, waiting for trends might be slow if not cached)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_context = executor.submit(read_main_context)
        future_trends = executor.submit(fetch_external_trends)
        
        context = future_context.result()
        trends = future_trends.result()

    suggestions = get_curation_suggestions(
        context, "normal", external_trends=trends
    )
    topics = suggestions.get("topics", [])
    if not topics:
        return "トピック提案の生成に失敗しました。"
    _branch_label = {
        "deepen": "🔬", "expand": "🌐", "jump": "🚀",
        "consistent": "🔬", "bridge": "🌐", "entropy": "🚀",
    }
    MAX_WORKERS = 3
    results = []
    
    print(f"[Curator] 🚀 Starting parallel execution for {len(topics)} topics...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_topic = {
            executor.submit(
                search_and_save_topic, 
                t["title"], 
                t.get("search_query", ""),
                t.get("source_note", ""), 
                t.get("branch", ""),
                t.get("reasoning", "")
            ): t for t in topics
        }
        
        for future in concurrent.futures.as_completed(future_to_topic):
            t = future_to_topic[future]
            branch = t.get("branch", "")
            icon = _branch_label.get(branch, "📚")
            try:
                r = future.result()
                # r format: "Success: Created [[...]] (Path: C:\...)"
                results.append(f"- {icon} {t['title']}: {r}")
            except Exception as e:
                print(f"[Curator] ❌ Error processing {t['title']}: {e}")
                results.append(f"- {icon} {t['title']}: Error ({e})")

    return "\n".join(results)


def run_setup() -> str:
    """対話なしで Setup を実行し、結果を返す。"""
    context = read_main_context()
    suggestions = get_curation_suggestions(context, "setup")
    topics = suggestions.get("topics", [])
    if not topics:
        return "トピック提案の生成に失敗しました。"
    results = []
    for t in topics:
        r = search_and_save_topic(
            t["title"], t.get("search_query", ""),
            t.get("source_note", ""), t.get("branch", ""),
        )
        results.append(f"- {t['title']}: {r}")
    return "\n".join(results)
