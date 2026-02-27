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
import feedparser

from lib.lib_shared import (
    client, tavily_client, MODEL_NAME,
    WORK_DIR, VAULT_ROOT, NEWS_DIR_NAME, DAILY_NOTE_DIR_NAME,
    retry, EDITORIAL_STYLE_RULES,
)

@retry(max_retries=3)
def tool_web_search(query: str) -> str:
    """Web検索を行い結果を返す"""
    print(f"[Tool] 🌐 Searching Web: {query}")
    try:
        response = tavily_client.search(query=query, search_depth="advanced", max_results=5)
        results = response.get("results", [])
        if not results: return "Web検索結果なし。"
        return "\n\n".join([f"- {r['title']}\n  URL: {r['url']}\n  {r['content']}" for r in results])
    except Exception as e:
        return f"Error searching web: {e}"

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
    デイリーノートに要約テキスト+リンクを追記する。
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

    daily_note_path = os.path.join(
        VAULT_ROOT, DAILY_NOTE_DIR_NAME, f"{today_str}.md"
    )

    if os.path.exists(daily_note_path):
        with open(daily_note_path, "r", encoding="utf-8") as f:
            existing = f.read()
        if output_filename not in existing:
            news_insert = (
                f"> {summary}\n\n"
                f"👉 [[{output_filename}|詳細はこちら]]"
            )
            placeholder = "<!-- NEWS_PLACEHOLDER -->"
            if placeholder in existing:
                updated = existing.replace(placeholder, news_insert)
            elif "## 📰" in existing:
                lines = existing.split('\n')
                insert_idx = None
                for i, line in enumerate(lines):
                    if line.startswith("## 📰"):
                        insert_idx = i + 1
                        break
                if insert_idx is not None:
                    lines.insert(insert_idx, news_insert)
                    updated = '\n'.join(lines)
                else:
                    updated = existing + f"\n\n## 📰 今日のニュース\n{news_insert}\n"
            else:
                updated = existing + f"\n\n## 📰 今日のニュース\n{news_insert}\n"
            with open(daily_note_path, "w", encoding="utf-8") as f:
                f.write(updated)
            link_msg = f"Linked to [[{today_str}]]"
        else:
            link_msg = f"Link already exists in [[{today_str}]]"
    else:
        link_msg = "Daily note not found (skipped link)"

    return f"Success: Created [[{NEWS_DIR_NAME}/{output_filename}]]. {link_msg}"
