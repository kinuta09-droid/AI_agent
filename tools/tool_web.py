# -*- coding: utf-8 -*-
"""
Web Tools Module
Extracted from agent_hello.py
"""

import os
import sys
import json
import datetime
import time
import re
import requests
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from google.genai import types
import feedparser

from lib.lib_shared import (
    client, tavily_client, MODEL_NAME,
    WORK_DIR, VAULT_ROOT, NEWS_DIR_NAME,
    retry, EDITORIAL_STYLE_RULES,
)
from lib.cost_guard import GROUNDING_COUNTER

# 下流の最終要約プロンプトは各ステップの observation 全体を1500字で切り詰める
# (engine_plan_execute.py の _build_final_summary_prompt)。1件の記事が長文だと
# 後続の結果がその外側に落ち、要約LLMに一度も見せられないまま消える
# (2026-08-03: recency_days で正しい記事を取得できていたのに、その記事が
# 6474文字目から始まっていたため最終回答に一切反映されなかった実例で発覚)。
_RESULT_CONTENT_CHARS = 500


@retry(max_retries=3)
def tool_web_search(query: str, recency_days: int = None) -> str:
    """Web検索を行い結果を返す。

    recency_days 指定時は一般検索に加えて topic="news" の直近検索も実行し、
    結果をURLで重複除去してマージする。news検索に**置き換える**と、答えが
    検索窓より前の出来事だった場合に正しい情報ごと落ちてしまうため、
    一般検索は必ず残す (取りこぼしをゼロにしたまま鮮度の高い情報を混ぜる)。

    news検索の結果を**先頭に**置き、各結果の content も
    _RESULT_CONTENT_CHARS で個別に切り詰める。どちらも上の下流切り詰め問題
    への対策で、一般検索の長文記事が予算を独占して直近ニュースを
    押し出さないようにする。
    """
    print(f"[Tool] 🌐 Searching Web: {query}" + (f" (recency_days={recency_days})" if recency_days else ""))
    try:
        max_results = 3 if recency_days else 5
        response = tavily_client.search(query=query, search_depth="advanced", max_results=max_results)
        general_results = response.get("results", [])

        results = general_results
        if recency_days:
            # news検索は「あれば良い追加情報」。ここで例外にすると一般検索の
            # 成功結果まで丸ごと失敗扱いになる (recency_days を付けた質問だけ
            # 失敗率が上がる回帰になるため、個別に握って一般検索側は必ず返す)
            try:
                news_response = tavily_client.search(
                    query=query, search_depth="advanced", max_results=3,
                    topic="news", days=recency_days,
                )
                news_results = news_response.get("results", [])
                seen_urls = {r["url"] for r in news_results}
                results = news_results + [r for r in general_results if r["url"] not in seen_urls]
            except Exception as e:
                print(f"[Tool] ⚠️ news検索に失敗、一般検索の結果のみ返す: {e}")

        if not results: return "Web検索結果なし。"

        blocks = []
        for r in results:
            content = r["content"]
            if len(content) > _RESULT_CONTENT_CHARS:
                content = content[:_RESULT_CONTENT_CHARS] + "…"
            blocks.append(f"- {r['title']}\n  URL: {r['url']}\n  {content}")
        return "\n\n".join(blocks)
    except Exception as e:
        return f"Error searching web: {e}"


# grounding_chunksは上限なく返り得るため、下流の最終要約プロンプトの1500字切り詰め
# (tool_web_search の _RESULT_CONTENT_CHARS と同じ理由) で消えないよう件数を絞る
_GROUNDING_MAX_SOURCES = 5


@retry(max_retries=2)
def tool_web_search_grounded(query: str) -> str:
    """Google Search Grounding (Gemini本体組み込みのGoogle検索) を使ったWeb検索。

    tool_web_search (Tavily) は無料枠が大きく既定の選択肢だが、Tavilyの検索
    ランキングはクエリの言い回しの微妙な差で変動し、狙った一次情報を安定して
    拾えないことがある (2026-08-03の障害調査で判明)。Geminiが自前で使う
    Google検索インデックスの方が「今日/本日/急に」等、直近性が特に重要で
    ずれると回答の意味が無くなる質問に強い。日次呼び出し数は GROUNDING_COUNTER
    で別枠管理する (無料枠5,000プロンプト/月への安全弁)。
    """
    print(f"[Tool] 🔍 Grounded Web Search: {query}")
    try:
        GROUNDING_COUNTER.check_and_increment()

        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=query,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        text = (response.text or "").strip()
        if not text:
            return "Web検索結果なし。"
    except Exception as e:
        return f"Error searching web (grounded): {e}"

    # 引用元の抽出は本文取得と別のtry/exceptにする。SDKのバージョン差異や
    # 想定外のレスポンス形で失敗しても、既に取得できている本文まで
    # 丸ごと"Error"扱いで捨てない (本文取得の成功と引用元抽出の成否は独立)。
    try:
        gm = response.candidates[0].grounding_metadata if response.candidates else None
        chunks = getattr(gm, "grounding_chunks", None) if gm is not None else None
        if not chunks:
            return text

        sources = []
        for c in chunks[:_GROUNDING_MAX_SOURCES]:
            web = getattr(c, "web", None)
            if web is None:
                continue
            title = getattr(web, "title", None) or "(タイトル不明)"
            uri = getattr(web, "uri", None)
            if uri:
                sources.append(f"- {title}: {uri}")

        if not sources:
            return text
        return text + "\n\n[参照元 (Google検索)]\n" + "\n".join(sources)
    except Exception as e:
        print(f"[Tool] ⚠️ 引用元の抽出に失敗、本文のみ返す: {e}")
        return text


@retry(max_retries=2)
def tool_get_youtube_transcript(url: str) -> str:
    """YouTubeのURLから字幕（文字起こし）を取得して返す"""
    print(f"[Tool] 📺 Fetching YouTube Transcript for: {url}")
    try:
        # URLからVideo IDを抽出
        video_id_match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
        if not video_id_match:
            return "Error: 有効なYouTube URLではありません。"
        
        video_id = video_id_match.group(1)
        
        # 字幕の取得（日本語を優先、なければ英語）
        yt = YouTubeTranscriptApi()
        transcript_list = yt.list(video_id)
        
        target_transcript = None
        for t in transcript_list:
            if t.language_code == 'ja':
                target_transcript = t
                break
        
        if not target_transcript:
            for t in transcript_list:
                if t.language_code == 'en':
                    target_transcript = t
                    break

        if not target_transcript:
             try:
                 target_transcript = next(iter(transcript_list))
             except StopIteration:
                 return "Error: No transcripts available."
        
        data = target_transcript.fetch()
        full_text = " ".join([item['text'] for item in data])
        
        return f"【YouTube動画内容（文字起こし）】\n{full_text[:10000]}" 
    except Exception as e:
        return f"Error: 字幕の取得に失敗しました。字幕がオフの動画である可能性があります。({str(e)})"

@retry(max_retries=2)
def tool_fetch_url(url: str) -> str:
    """指定されたURLのWebページの内容を取得し、テキストとして返す"""
    print(f"[Tool] 🌐 Fetching URL: {url}")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        if response.encoding is None:
            response.encoding = response.apparent_encoding

        soup = BeautifulSoup(response.text, 'html.parser')
        
        for script in soup(["script", "style"]):
            script.decompose()

        text = soup.get_text()

        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = '\n'.join(chunk for chunk in chunks if chunk)

        return f"【URL内容: {url}】\n{text[:10000]}"
    except Exception as e:
        return f"Error fetching URL: {e}"

def _read_interests_and_rss():
    """設定ファイルからキーワードとRSS URLを読み込む"""
    path = os.path.join(VAULT_ROOT, "User_Interests.md")
    keywords = []
    rss_urls = []

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            mode = None
            for line in f:
                line = line.strip()
                if "重点監視トピック" in line:
                    mode = "keywords"
                elif "RSSフィード" in line:
                    mode = "rss"
                elif "除外する話題" in line or line.startswith("##"):
                    mode = None
                elif line.startswith("-") and len(line) > 2:
                    content = line.lstrip("- ").strip()
                    if mode == "keywords":
                        keywords.append(content)
                    elif mode == "rss":
                        rss_urls.append(content)
    return keywords, rss_urls


def tool_fetch_daily_news(days: int = 1) -> str:
    """
    Web検索とRSSフィードからニュースを収集し、Geminiで要約を生成。
    Tpl_Permanentテンプレートを適用して02_Newsに保存。
    """
    print(f"[Tool] 🗞️ Fetching Daily News...")
    keywords, rss_urls = _read_interests_and_rss()

    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    news_body = ""

    if keywords:
        news_body += "## 🌍 Web Topics\n"
        for topic in keywords:
            print(f"  🔍 Searching: {topic}")
            try:
                response = tavily_client.search(
                    query=topic, topic="news", days=days, max_results=2
                )
                news_body += f"### {topic}\n"
                for result in response.get("results", []):
                    news_body += f"- [{result['title']}]({result['url']})\n"
                news_body += "\n"
                time.sleep(1)
            except Exception as e:
                print(f"  ⚠️ Search error for '{topic}': {e}")

    if rss_urls:
        news_body += "## 📡 RSS Feeds\n"
        for url in rss_urls:
            try:
                feed = feedparser.parse(url)
                feed_title = feed.feed.get('title', 'Unknown Feed')
                news_body += f"### {feed_title}\n"
                for entry in feed.entries[:3]:
                    news_body += f"- [{entry.title}]({entry.link})\n"
                news_body += "\n"
            except Exception as e:
                print(f"  ⚠️ Error fetching RSS {url}: {e}")

    if not news_body.strip():
        return "No news found for today."

    print(f"  🤖 Generating editorial with Gemini...")
    editorial = ""
    summary = ""
    try:
        editorial_prompt = f"""以下の検索結果・RSS情報を元に、今日のニュースレポートを日本語で作成してください。

【素材】
{news_body}

【執筆ルール】
- カテゴリ別（政治、経済、テクノロジー、社会など）に分類する。
- 各カテゴリは「## カテゴリ名」で始める。
- 各記事には「### 短い見出し」を付け、その下に日本語で3行程度の要約を書く。
- 見出しは内容を端的に表す10〜20文字程度にする（例: ### n8nに重大な脆弱性が発覚）。
- 元記事のURLがあれば要約の末尾に [元記事](URL) のリンクを付ける。
- 最後に「---」の後、1行で全体のインサイトを書く。
- 冒頭に「# 」見出しは付けない（テンプレートが自動付与するため）。

{EDITORIAL_STYLE_RULES}"""
        editorial_resp = client.models.generate_content(
            model=MODEL_NAME, contents=editorial_prompt
        )
        editorial = editorial_resp.text.strip()
    except Exception as e:
        print(f"  ⚠️ Editorial generation failed, using raw data: {e}")
        editorial = news_body

    try:
        summary_prompt = (
            "以下のニュースレポートを読み、今日の重要ポイントを日本語で1〜3行に要約してください。"
            "箇条書きではなく、自然な文章で書いてください。\n\n"
            f"{editorial}"
        )
        summary_resp = client.models.generate_content(
            model=MODEL_NAME, contents=summary_prompt
        )
        summary = summary_resp.text.strip()
    except Exception as e:
        print(f"  ⚠️ Summary generation failed: {e}")
        summary = "（要約の自動生成に失敗しました）"

    output_filename = f"News_{today_str}.md"
    save_dir = os.path.join(VAULT_ROOT, NEWS_DIR_NAME)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, output_filename)

    template_path = os.path.join(
        VAULT_ROOT, '99_Assets', 'Templates', 'Tpl_Permanent.md'
    )

    if os.path.exists(template_path):
        with open(template_path, 'r', encoding='utf-8') as f:
            tpl = f.read()
        title_text = f"今日のニュース ({today_str})"
        tpl = tpl.replace('{{date}}', today_str)
        tpl = tpl.replace('{{title}}', title_text)
        tpl = tpl.replace(
            'このノートが言いたいことを1〜3行で書く。',
            summary
        )
        tpl = tpl.replace('- ここに詳細を書く', editorial)
        note_content = tpl
    else:
        note_content = (
            f"# 今日のニュース ({today_str})\n\n"
            f"> [!SUMMARY] 要約・核心\n> {summary}\n\n"
            f"{editorial}"
        )

    with open(save_path, "w", encoding="utf-8") as f:
        f.write(note_content)
    print(f"  ✅ Saved: {save_path}")

    return f"Success: Created [[{NEWS_DIR_NAME}/{output_filename}]]."
