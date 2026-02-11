# -*- coding: utf-8 -*-

import os
import sys
import json
import re
import datetime
import time
import traceback
import glob
import uuid
from functools import wraps
from dotenv import load_dotenv
from google import genai
from google.genai import types
from tavily import TavilyClient
from youtube_transcript_api import YouTubeTranscriptApi
import feedparser
import ccxt
import pandas as pd
import pandas_ta as ta_lib
import logging
import requests
from bs4 import BeautifulSoup

# Windowsコンソールでの文字化け防止
sys.stdout.reconfigure(encoding='utf-8')

# 環境変数の読み込み
load_dotenv('C:\\AI_agent\\.env')
api_key = os.getenv("GOOGLE_API_KEY")
tavily_api_key = os.getenv("TAVILY_API_KEY")

if not api_key:
    print("Error: GOOGLE_API_KEY not found.")
    sys.exit(1)
if not tavily_api_key:
    # Use Tavily Key if available, otherwise warn
    print("Warning: TAVILY_API_KEY not found. Search may fail.")
    # sys.exit(1) # Let it crash gracefully if needed, or handle it. For now assume it exists.

try:
    client = genai.Client(api_key=api_key)
except Exception as e:
    print(f"Error initializing Gemini API: {e}")
    sys.exit(1)

try:
    tavily_client = TavilyClient(api_key=tavily_api_key)
except Exception as e:
    print(f"Error initializing Tavily API: {e}")
    # continue anyway

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

MODEL_NAME = 'gemini-2.5-flash'
MAX_RETRIES = 5
MAX_STEPS = 10 # ReAct loop limit

HISTORY_FILE = r'C:\obsidian\Obsidian\Dev_Logs\chat_history.json'
WORK_DIR = r'C:\obsidian\Obsidian'
VAULT_ROOT = r'C:\obsidian\Obsidian'

REPORT_DIR = os.path.join(WORK_DIR, '40_Report')
OUTPUT_FILE = os.path.join(WORK_DIR, 'Dev_Logs', 'Agent_output.md')
ERROR_FILE = os.path.join(WORK_DIR, 'Dev_Logs', 'ErrorReport.md')

NEWS_DIR_NAME = "02_News"
CRYPTO_DIR_NAME = "03_Crypto"
DAILY_NOTE_DIR_NAME = "01_Daily"
TRADE_LOG_PATH = os.path.join(VAULT_ROOT, "Dev_Logs", "trade_log.json")
LEARNING_DB_PATH = os.path.join("C:\\AI_agent", "learning_db.json")
PERFORMANCE_DASHBOARD_PATH = os.path.join(VAULT_ROOT, "Dev_Logs", "Agent_Performance.md")

# 除外したいフォルダ
EXCLUDE_DIRS = {'.git', '.obsidian', 'Dev_Logs', 'Trash'}

# ---------------------------------------------------------
# Utilities
# ---------------------------------------------------------

def safe_json_loads(text: str) -> dict:
    """LLMの回答からJSON部分のみを抽出してパースする
    
    Args:
        text: LLMからの生テキスト出力
    
    Returns:
        パースされたJSONオブジェクト（パース失敗時は空の辞書）
    """
    if not text or not text.strip():
        print("[safe_json_loads] ⚠️ Empty input received")
        return {}
    
    # Markdownのコードブロック（```json ... ```）を削除
    clean_text = re.sub(r'```(?:json)?\s*|```', '', text).strip()
    
    # 最初に辞書形式 { ... } を試す
    match = re.search(r'(\{.*\})', clean_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            print(f"[safe_json_loads] ⚠️ Failed to parse dict: {e}")
    
    # 配列形式 [ ... ] も対応
    match = re.search(r'(\[.*\])', clean_text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1))
            # 配列の場合はplanとしてラップして返す
            if isinstance(result, list):
                return {"plan": result}
            return result
        except json.JSONDecodeError as e:
            print(f"[safe_json_loads] ⚠️ Failed to parse array: {e}")
    
    # 最終手段: 全体をそのままパース
    try:
        return json.loads(clean_text)
    except json.JSONDecodeError as e:
        print(f"[safe_json_loads] ❌ All parsing attempts failed: {e}")
        print(f"[safe_json_loads] Raw input (first 200 chars): {text[:200]}")
        return {}

def retry(max_retries=MAX_RETRIES, initial_delay=2):
    def decorator_retry(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            delay = initial_delay
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    print(f"Retry {retries+1}/{max_retries} failed. Waiting {delay}s... Error: {e}")
                    traceback.print_exc()
                    retries += 1
                    time.sleep(delay)
                    delay *= 2
            print(f"Function {func.__name__} failed after {max_retries} retries.")
            raise
        return wrapper
    return decorator_retry

def find_file_in_vault(filename: str) -> str | None:
    """Vault内でファイルを検索し、見つかった場合はパスを返す"""
    if not filename.endswith('.md'):
        filename += '.md'
    direct_path = os.path.join(WORK_DIR, filename)
    if os.path.exists(direct_path):
        return direct_path
    for root, dirs, files in os.walk(VAULT_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        if filename in files:
            return os.path.join(root, filename)
    return None

def resolve_obsidian_links(text: str) -> str:
    """テキスト内の[[Obsidianリンク]]を実際のファイル内容に展開する"""
    pattern = r'\[\[(.*?)\]\]'
    matches = re.findall(pattern, text)
    if not matches:
        return text

    expanded_text = text
    for link_name in matches:
        real_filename = link_name.split('|')[0]
        file_path = find_file_in_vault(real_filename)
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                injection = (
                    f"\n\n--- Start of Referenced File: {real_filename} ---\n"
                    f"{content}\n"
                    f"--- End of Referenced File ---\n\n"
                )
                expanded_text = expanded_text.replace(f'[[{link_name}]]', injection)
            except Exception as e:
                print(f" --> Error reading {real_filename}: {e}")
    return expanded_text

# ---------------------------------------------------------
# Tools
# ---------------------------------------------------------

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
        # Note: Installed library requires instantiation
        yt = YouTubeTranscriptApi()
        transcript_list = yt.list(video_id)
        
        target_transcript = None
        # Manual search to avoid 'NoTranscriptFound' buggy exception
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
        # This library version returns objects, not dicts
        full_text = " ".join([item.text for item in data])
        
        # 文字数が多すぎる場合は、エージェントが処理しやすいよう冒頭から一定量を返す
        return f"【YouTube動画内容（文字起こし）】\n{full_text[:10000]}" 
    except Exception as e:
        return f"Error: 字幕の取得に失敗しました。字幕がオフの動画である可能性があります。({str(e)})"


def tool_vault_search(keywords: str) -> str:
    """Vault内をキーワード検索する"""
    print(f"[Tool] 🧠 Searching Vault: {keywords}")
    kw_list = [k.strip() for k in keywords.split() if k.strip()]
    if not kw_list: return "No keywords."
    hits = []
    MAX_HITS = 10
    for root, dirs, files in os.walk(VAULT_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for file in files:
            if not file.endswith('.md'): continue
            try:
                with open(os.path.join(root, file), 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                score = 0
                snippet = ""
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if any(k.lower() in line.lower() for k in kw_list):
                        score += 1
                        # Expand context: 5 lines before, 25 lines after
                        start = max(0, i - 5)
                        end = min(len(lines), i + 25)
                        snippet = "\n".join(lines[start:end])
                        break
                if score > 0:
                    hits.append(f"- File: [[{file}]]\n  Snippet:\n{snippet}\n...")
                    if len(hits) >= MAX_HITS: break
            except: pass
        if len(hits) >= MAX_HITS: break
    return "\n\n".join(hits) if hits else "Vault内に見つかりませんでした。"

def _extract_and_integrate_sections(content: str, tpl: str) -> tuple[str, str]:
    """コンテンツから参照・ソースを抽出し、テンプレートに統合する"""
    
    # 1. References (参照元) の抽出
    # Use [^\n]* to match header content without crossing lines, even with DOTALL
    ref_pattern = re.compile(r'^##\s*[^\n]*?(?:References|Reference|参照元|参考文献)[^\n]*?\n(.*?)(?=\n##|\Z)', re.MULTILINE | re.DOTALL | re.IGNORECASE)
    ref_match = ref_pattern.search(content)
    if ref_match:
        ref_body = ref_match.group(1).strip()
        # テンプレートの "### 参照元" セクションを探す
        tpl_ref_marker = "### 参照元"
        if tpl_ref_marker in tpl:
            # 既存のマーカー以下の行（例: "- "）を置換するか、追記するか
            # ここではシンプルにマーカーの下に追記する形にする
            # ただし、テンプレートに "- " だけの行がある場合はそれを削除したい
            tpl = re.sub(r'### 参照元\s*\n-\s*\n', f'### 参照元\n{ref_body}\n', tpl)
            # もし置換されなかった場合（"- "がない場合）は追記
            if ref_body not in tpl:
                 tpl = tpl.replace(tpl_ref_marker, f'{tpl_ref_marker}\n{ref_body}')
        
        # コンテンツから削除
        content = ref_pattern.sub('', content).strip()

    # 2. Sources (関連ノート) の抽出
    # Pattern A: ## Sources header
    src_pattern = re.compile(r'^##\s*[^\n]*?(?:Sources|Source|関連ノート|Sources/Citations)[^\n]*?\n(.*?)(?=\n##|\Z)', re.MULTILINE | re.DOTALL | re.IGNORECASE)
    src_match = src_pattern.search(content)
    src_links = []
    
    if src_match:
        src_body = src_match.group(1).strip()
        src_links.append(src_body)
        content = src_pattern.sub('', content).strip()

    # Pattern B: > Source: [[...]] (Lineage link)
    lineage_pattern = re.compile(r'^>\s*Source:\s*(\[\[.*?\]\])', re.MULTILINE | re.IGNORECASE)
    lineage_matches = lineage_pattern.findall(content)
    if lineage_matches:
        for match in lineage_matches:
             src_links.append(f"- {match}")
        content = lineage_pattern.sub('', content).strip()

    if src_links:
        src_body = "\n".join(src_links)
        # テンプレートの "### 関連ノート" セクションを探す
        tpl_src_marker = "### 関連ノート"
        if tpl_src_marker in tpl:
            # テンプレートのプレースホルダー "- [[ ]]" を置換
            tpl = re.sub(r'### 関連ノート\s*\n-\s*\[\[\s*\]\]\s*\n', f'### 関連ノート\n{src_body}\n', tpl)
            if src_body not in tpl:
                tpl = tpl.replace(tpl_src_marker, f'{tpl_src_marker}\n{src_body}')

    return content, tpl

@retry(max_retries=3)
def tool_create_note(filename: str, content: str, summary: str = "") -> str:
    """Vault内に新規ノートを作成する。Templateを使用する。"""
    print(f"[Tool] 📝 Creating Note: {filename}")
    # Fix: User/Agent often includes path (30_Main/...) in filename. Strip it.
    filename = filename.replace('\\', '/').split('/')[-1]
    
    if not filename.endswith('.md'): filename += '.md'
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)

    save_dir = os.path.join(WORK_DIR, '30_Main')
    save_path = os.path.join(save_dir, filename)
    template_path = os.path.join(WORK_DIR, '99_Assets', 'Templates', 'Tpl_Permanent.md')


    if os.path.exists(save_path):
        # 既存ファイルと被る場合は、タイムスタンプを付与して新規作成
        timestamp = datetime.datetime.now().strftime('%H%M%S')
        base_name = os.path.splitext(filename)[0]
        filename = f"{base_name}_{timestamp}.md"
        save_path = os.path.join(save_dir, filename)
        print(f"[Tool] ⚠️ File exists, creating with suffix: {filename}")

    try:
        os.makedirs(save_dir, exist_ok=True)
        note_content = content
        
        if os.path.exists(template_path):
            with open(template_path, 'r', encoding='utf-8') as f:
                tpl = f.read()
            
            # Template Integration: Extract references/sources to template
            content, tpl = _extract_and_integrate_sections(content, tpl)

            today_iso = datetime.datetime.now().strftime('%Y-%m-%d')
            title_text = os.path.splitext(filename)[0]
            tpl = tpl.replace('{{date}}', today_iso)
            tpl = tpl.replace('{{title}}', title_text)
            
            # Auto-link MOC
            try:
                # Combine summary and content for better classification
                check_content = (summary + "\n" + content)[:3000]
                parent_moc = _determine_parent_moc(check_content)
                if parent_moc:
                    tpl = tpl.replace('up: "[[00_Inbox]]"', f'up: "[[{parent_moc}]]"')
                    print(f"[Tool] 🔗 Linked to MOC: [[{parent_moc}]]")
            except Exception as e:
                print(f"[Tool] ⚠️ MOC auto-link failed: {e}")
            
            summary_placeholder = "> このノートが言いたいことを1〜3行で書く。"
            if summary:
                 tpl = tpl.replace(summary_placeholder, f"> {summary}")
            else:
                 tpl = tpl.replace(summary_placeholder, "> (要約未入力)")

            content_placeholder = "- ここに詳細を書く"
            if content_placeholder in tpl:
                tpl = tpl.replace(content_placeholder, content)
            else:
                tpl += "\n\n" + content
            note_content = tpl

        with open(save_path, 'w', encoding='utf-8') as f:
            f.write(note_content)
            # Return format for n8n (include folder in path)
        return f"Success: Created [[{filename}]] with template."
    except Exception as e:
        return f"Error creating note: {e}"

def tool_read_note(filename: str) -> str:
    """Vault内のノートの内容を読み込む"""
    print(f"[Tool] 📖 Reading Note: {filename}")
    path = find_file_in_vault(filename)
    if not path: return f"Error: File {filename} not found."
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Error reading note: {e}"

def tool_update_note(filename: str, append_text: str) -> str:
    """既存のノートに追記する"""
    print(f"[Tool] ✍️ Updating Note: {filename}")
    path = find_file_in_vault(filename)
    if not path: return f"Error: File {filename} not found."
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write("\n\n" + append_text)
        return f"Success: Updated [[{filename}]]"
    except Exception as e:
        return f"Error updating note: {e}"

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
        
        # Encoding check
        if response.encoding is None:
            response.encoding = response.apparent_encoding

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()

        text = soup.get_text()

        # Break into lines and remove leading/trailing space on each
        lines = (line.strip() for line in text.splitlines())
        # Break multi-headlines into a line each
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        # Drop blank lines
        text = '\n'.join(chunk for chunk in chunks if chunk)

        return f"【URL内容: {url}】\n{text[:10000]}" # Limit to 10k chars
    except Exception as e:
        return f"Error fetching URL: {e}"

def tool_merge_weekly_reports() -> str:
    """30_Main内の7日以内のレポートを週次レポートにまとめる"""
    print(f"[Tool] 🧹 Merging Weekly Reports...")
    target_dir = os.path.join(WORK_DIR, '30_Main')
    archive_dir = os.path.join(WORK_DIR, '50_Archive')

    # 今日の日付から 'YYYY-Www' を計算 (例: 2026-W06)
    today = datetime.datetime.now()
    week_str = today.strftime('%Y-W%U') 
    
    # マージ先のファイル名
    summary_filename = f"{week_str}_Weekly_Summary.md"
    summary_path = os.path.join(target_dir, summary_filename)
    
    # アーカイブ先
    archive_sub_dir = os.path.join(archive_dir, week_str)

    if not os.path.exists(target_dir):
        return "Error: 30_Main directory not found."

    merged_content = f"# Weekly Summary: {week_str}\n\nGenerated: {today.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    files_processed = []

    try:
        os.makedirs(archive_sub_dir, exist_ok=True)
        
        # 30_Main内の.mdを取得
        files = [f for f in os.listdir(target_dir) if f.endswith('.md')]
        
        # 週次まとめファイル自身は除外
        if summary_filename in files:
            files.remove(summary_filename)

        if not files:
            return "No files to merge."

        for file in files:
            file_path = os.path.join(target_dir, file)
            try:
                # 最終更新日時
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                # 7日以内かチェック（あるいは無条件でも良いが、安全のため）
                if (today - mtime).days > 7:
                    continue

                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                merged_content += f"## Source: [[{file}]] ({mtime.strftime('%Y-%m-%d')})\n\n{content}\n\n---\n\n"
                files_processed.append(file)
            except Exception as e:
                print(f"Error reading {file}: {e}")

        if not files_processed:
            return "No recent files found to merge."

        # まとめたファイルを保存
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(merged_content)

        # アーカイブへの移動
        for file in files_processed:
            src = os.path.join(target_dir, file)
            dst = os.path.join(archive_sub_dir, file)
            try:
                os.rename(src, dst)
            except Exception as e:
                print(f"Error moving {file}: {e}")

        return f"Success: Created [[{summary_filename}]]. Merged {len(files_processed)} files. Originals moved to 50_Archive/{week_str}."

    except Exception as e:
        return f"Error merging reports: {e}"

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


def _get_moc_list() -> list[str]:
    """10_MOCsフォルダ内のMOCファイル名リストを取得する"""
    moc_dir = os.path.join(VAULT_ROOT, "10_MOCs")
    if not os.path.exists(moc_dir):
        return []
    
    files = [f for f in os.listdir(moc_dir) if f.endswith('.md')]
    # 拡張子を除いたファイル名を返す (e.g., "MOC_AI")
    return [os.path.splitext(f)[0] for f in files]

def _determine_parent_moc(content: str) -> str | None:
    """コンテンツ内容に基づいて最適な親MOCを判定する"""
    moc_list = _get_moc_list()
    if not moc_list:
        return None

    moc_str = ", ".join(moc_list)
    # コンテンツが長すぎる場合は要約のために切り詰める
    content_snippet = content[:2000]

    prompt = f"""以下のテキストの内容に最も適した親カテゴリ（MOC: Map of Content）をリストから1つ選んでください。

【選択肢リスト】
{moc_str}

【テキスト内容】
{content_snippet}

【ルール】
- 最も関連性の高いMOC名を1つだけ出力してください。
- リストに適切なものがなければ "None" と出力してください。
- 出力はMOC名のみ（例: "MOC_AI"）。余計な説明は不要。
"""
    try:
        response = client.models.generate_content(
            model=MODEL_NAME, contents=prompt
        )
        result = response.text.strip()
        # クリーニング（引用符などを削除）
        result = result.replace('"', '').replace("'", "").replace("`", "")
        
        if result in moc_list:
            return result
        return None
    except Exception as e:
        print(f"⚠️ MOC determination failed: {e}")
        return None


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

    # 1. TavilyによるWeb検索
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

    # 2. RSSフィードの取得
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

    # 3. Geminiで「新聞記事」と「要約」を同時生成
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
"""
        editorial_resp = client.models.generate_content(
            model=MODEL_NAME, contents=editorial_prompt
        )
        editorial = editorial_resp.text.strip()
        print(f"  ✅ Editorial: {len(editorial)} chars")
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
        print(f"  ✅ Summary: {summary[:80]}...")
    except Exception as e:
        print(f"  ⚠️ Summary generation failed: {e}")
        summary = "（要約の自動生成に失敗しました）"

    # 4. Tpl_Permanentテンプレートを適用して02_Newsに保存
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

    # 5. デイリーノートの所定位置に要約+リンクを挿入
    daily_note_path = os.path.join(
        VAULT_ROOT, DAILY_NOTE_DIR_NAME, f"{today_str}.md"
    )

    if os.path.exists(daily_note_path):
        with open(daily_note_path, "r", encoding="utf-8") as f:
            existing = f.read()
        # 重複挿入を防止
        if output_filename not in existing:
            news_insert = (
                f"> {summary}\n\n"
                f"👉 [[{output_filename}|詳細はこちら]]"
            )
            # プレースホルダーがあればそこに挿入
            placeholder = "<!-- NEWS_PLACEHOLDER -->"
            if placeholder in existing:
                updated = existing.replace(placeholder, news_insert)
            # プレースホルダーがなくてもニュースセクション見出しがあれば直後に挿入
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
                # どちらもなければ末尾に追加
                updated = existing + f"\n\n## 📰 今日のニュース\n{news_insert}\n"
            with open(daily_note_path, "w", encoding="utf-8") as f:
                f.write(updated)
            link_msg = f"Linked to [[{today_str}]]"
        else:
            link_msg = f"Link already exists in [[{today_str}]]"
    else:
        link_msg = "Daily note not found (skipped link)"

    return f"Success: Created [[{NEWS_DIR_NAME}/{output_filename}]]. {link_msg}"


# ---------------------------------------------------------
# Next-Gen Quant Helpers
# ---------------------------------------------------------

def _calculate_kelly(win_rate: float, reward_ratio: float = 2.0, fraction: float = 0.25) -> float:
    """フラクショナル・ケリー基準でポジションサイズ(%)を算出"""
    if win_rate <= 0:
        return 0.0
    q = 1 - win_rate
    kelly_f = (win_rate * reward_ratio - q) / reward_ratio
    return max(kelly_f * fraction * 100, 0.0)


def _detect_regime(df) -> tuple:
    """ADX + ATR% で市場レジームを判定 → (regime, adx, atr_pct, dmp, dmn)"""
    adx_s = ta_lib.adx(df['high'], df['low'], df['close'], length=14)
    atr_s = ta_lib.atr(df['high'], df['low'], df['close'], length=14)
    adx = float(adx_s['ADX_14'].iloc[-1]) if adx_s is not None and pd.notna(adx_s['ADX_14'].iloc[-1]) else 20.0
    dmp = float(adx_s['DMP_14'].iloc[-1]) if adx_s is not None and 'DMP_14' in adx_s.columns and pd.notna(adx_s['DMP_14'].iloc[-1]) else 0.0
    dmn = float(adx_s['DMN_14'].iloc[-1]) if adx_s is not None and 'DMN_14' in adx_s.columns and pd.notna(adx_s['DMN_14'].iloc[-1]) else 0.0
    atr_val = float(atr_s.iloc[-1]) if atr_s is not None and pd.notna(atr_s.iloc[-1]) else 0.0
    atr_pct = (atr_val / float(df['close'].iloc[-1])) * 100 if df['close'].iloc[-1] > 0 else 0.0

    if atr_pct > 3.0:
        regime = "VOLATILE"
    elif adx > 25:
        regime = "TRENDING"
    else:
        regime = "SIDEWAYS"
    return regime, adx, atr_pct, dmp, dmn


def _detect_liquidity_sweep(df, window: int = 20) -> tuple:
    """SMC: 直近安値をヒゲで割り込み終値で戻すパターン → (bool, sweep_level)"""
    if len(df) < window + 1:
        return False, 0.0
    recent_low = float(df['low'].iloc[-(window+1):-1].min())
    latest = df.iloc[-1]
    is_sweep = (float(latest['low']) < recent_low) and (float(latest['close']) > recent_low)
    return is_sweep, recent_low


def _analyze_market_drivers(symbol: str, change_pct_24h: float) -> tuple:
    """
    価格変動の「理由」をWeb検索し、ネガティブ要因の深刻度を判定する。
    Returns: (news_summary, risk_level)
    risk_level: "SYSTEMIC_RISK" | "NORMAL_CORRECTION" | "NEUTRAL"
    """
    if abs(change_pct_24h) < 3.0:
        return "直近24hの変動が小さいため、ニュース検索をスキップしました。", "NEUTRAL"

    coin_name = symbol.split('/')[0]
    direction = "drop" if change_pct_24h < 0 else "rise"
    query = f"Why is {coin_name} price {direction} today crypto news"

    print(f"  🕵️ Searching reason for {symbol} ({change_pct_24h:+.1f}%)...")

    try:
        response = tavily_client.search(query=query, topic="news", max_results=3)
    except Exception as e:
        print(f"  ⚠️ Market driver search failed: {e}")
        return f"ニュース検索に失敗しました: {e}", "NEUTRAL"

    summary_lines = []
    sentiment_score = 0

    risk_keywords = ["hack", "sec", "ban", "bankruptcy", "fraud", "war", "collapse",
                     "exploit", "rug pull", "delisting", "shutdown"]
    fud_keywords = ["rumor", "correction", "profit taking", "liquidations",
                    "sell-off", "pullback", "fear"]

    for res in response.get("results", []):
        content = res.get('content', '').lower()
        title = res.get('title', 'No title')
        url = res.get('url', '')
        summary_lines.append(f"- [{title}]({url})")

        if any(w in content for w in risk_keywords):
            sentiment_score -= 2
        elif any(w in content for w in fud_keywords):
            sentiment_score -= 0.5

    if not summary_lines:
        return "関連ニュースが見つかりませんでした。", "NEUTRAL"

    if sentiment_score <= -2:
        risk_level = "SYSTEMIC_RISK"
    elif sentiment_score < 0:
        risk_level = "NORMAL_CORRECTION"
    else:
        risk_level = "NEUTRAL"

    return "\n".join(summary_lines), risk_level


def _check_divergence(df, rsi_col: str = 'rsi', window: int = 20) -> bool:
    """
    強気RSIダイバージェンス検知:
    価格は安値を更新（または同等）だが、RSIは切り上がっている場合にTrue。
    """
    if len(df) < window + 1 or rsi_col not in df.columns:
        return False

    lookback = df.iloc[-(window+1):-1]
    if lookback['low'].isna().all() or lookback[rsi_col].isna().all():
        return False

    last_low_idx = lookback['low'].idxmin()
    if pd.isna(last_low_idx):
        return False

    current_low = float(df['low'].iloc[-1])
    prev_low = float(df['low'].loc[last_low_idx])
    prev_rsi = df[rsi_col].loc[last_low_idx]
    current_rsi = df[rsi_col].iloc[-1]

    if pd.isna(prev_rsi) or pd.isna(current_rsi):
        return False

    return (current_low <= float(prev_low)) and (float(current_rsi) > float(prev_rsi) + 3)


# ---------------------------------------------------------
# Trade Log (Prediction Audit System)
# ---------------------------------------------------------

def _load_trade_log() -> list:
    """trade_log.json を読み込む。存在しなければ空リストを返す。"""
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    try:
        with open(TRADE_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_trade_log(logs: list):
    """trade_log.json を保存する。"""
    os.makedirs(os.path.dirname(TRADE_LOG_PATH), exist_ok=True)
    with open(TRADE_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)


def _save_prediction(symbol: str, signal: str, entry_price: float,
                     stop_loss: float, kelly_pct: float, reasons: list) -> str:
    """BUY系シグナル発生時に予測をログに記録する。"""
    logs = _load_trade_log()

    risk = entry_price - stop_loss
    target_price = entry_price + (risk * 2) if risk > 0 else entry_price * 1.10

    record = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.datetime.now().isoformat(),
        "symbol": symbol,
        "signal": signal,
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "target_price": round(target_price, 2),
        "kelly_pct": round(kelly_pct, 1),
        "reason": "; ".join(reasons) if reasons else "N/A",
        "status": "OPEN",
        "audit_note": ""
    }

    logs.append(record)
    _save_trade_log(logs)
    print(f"  📝 Prediction logged: {record['id']} ({symbol} {signal} @ ${entry_price:,.2f})")
    return record["id"]


def tool_analyze_crypto(symbol: str = "BTC/USDT") -> str:
    """
    次世代クオンツ分析: レジーム検知 + SMC + ケリー基準を統合。
    結果を03_Cryptoにレポート保存し、強いシグナルがあればn8nへ通知する。
    """
    symbol = symbol.upper()
    if "/" not in symbol:
        symbol += "/USDT"
    
    print(f"[Tool] 🪙 Analyzing Crypto: {symbol}")
    import requests as req

    exchange = ccxt.binance({'enableRateLimit': True})

    # 1. 価格データの取得 (4時間足, 100本)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '4h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    except Exception as e:
        return f"Error fetching {symbol} data: {e}"

    # 2. テクニカル指標の計算
    df['rsi'] = ta_lib.rsi(df['close'], length=14)
    df['ema50'] = ta_lib.ema(df['close'], length=50)
    df['ema200'] = ta_lib.ema(df['close'], length=200)
    macd_df = ta_lib.macd(df['close'])
    if macd_df is not None:
        df = pd.concat([df, macd_df], axis=1)

    latest = df.iloc[-1]
    current_price = float(latest['close'])
    current_rsi = float(latest['rsi']) if pd.notna(latest['rsi']) else 50.0
    ema50 = float(latest['ema50']) if pd.notna(latest['ema50']) else current_price
    ema200 = float(latest['ema200']) if pd.notna(latest['ema200']) else current_price

    # 3. Fear & Greed Index の取得
    fng_value = 50
    fng_class = "Neutral"
    try:
        fng_resp = req.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        fng_data = fng_resp.json()['data'][0]
        fng_value = int(fng_data['value'])
        fng_class = fng_data['value_classification']
    except Exception as e:
        print(f"  ⚠️ F&G fetch failed: {e}")

    # 4. Next-Gen: レジーム検知 + SMC + ダイバージェンス + マーケットドライバー
    regime, adx, atr_pct, dmp, dmn = _detect_regime(df)
    is_sweep, sweep_level = _detect_liquidity_sweep(df)
    has_divergence = _check_divergence(df)
    print(f"  📈 Regime: {regime} (ADX={adx:.1f}, ATR%={atr_pct:.2f})")
    if is_sweep:
        print(f"  🎯 Liquidity Sweep detected at {sweep_level:.2f}")
    if has_divergence:
        print(f"  📊 RSI Bullish Divergence detected")

    # 4b. 24h変動率 + マーケットドライバー分析 ("Why")
    price_24h_ago = float(df['close'].iloc[-7]) if len(df) >= 7 else current_price
    change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100
    news_summary, news_risk = _analyze_market_drivers(symbol, change_24h)
    print(f"  🕵️ News Risk: {news_risk}")

    # 4c. 落ちるナイフ判定 (ADX > 30 + 下降モメンタム)
    is_falling_knife = (adx > 30) and (dmn > dmp)
    if is_falling_knife:
        print(f"  🔪 Falling Knife warning (ADX={adx:.1f}, DM-={dmn:.1f} > DM+={dmp:.1f})")

    # 5. Strategy 2.0 統合判定ロジック
    signal = "NEUTRAL"
    confidence = 0.0
    reasons = []
    trend = "UP" if current_price > ema200 else "DOWN"

    # 0: ファンダメンタルズ・リスク（最優先 - テクニカル無効化）
    if news_risk == "SYSTEMIC_RISK":
        signal = "STAY AWAY"
        confidence = 0.0
        reasons.append("⚠️ ファンダメンタルズに深刻な悪材料あり。テクニカル無効。")

    # A: 落ちるナイフ回避（ダイバージェンスなしの強い下落局面）
    elif is_falling_knife and not has_divergence:
        signal = "WAIT"
        confidence = 0.0
        reasons.append(f"📉 下落トレンドが強すぎます (ADX: {adx:.1f})。底打ち確認（ダイバージェンス）まで待機。")

    else:
        # B: SMC Liquidity Sweep
        if is_sweep:
            signal = "STRONG BUY"
            confidence = 0.65
            reasons.append(f"流動性回収を検知: 直近安値 ${sweep_level:,.2f} をヒゲで割込み反発（SMC）")

        # C: パニック・リバーサル（ダイバージェンスで確度UP）
        if fng_value < 20 and current_rsi < 25:
            if has_divergence:
                signal = "STRONG BUY"
                confidence = max(confidence, 0.65)
                reasons.append(f"極度の恐怖 (F&G: {fng_value}) + 売られすぎ (RSI: {current_rsi:.1f}) + ダイバージェンス確認")
            else:
                if signal == "NEUTRAL":
                    signal = "WATCH LIST"
                    confidence = max(confidence, 0.35)
                reasons.append(f"極度の恐怖 (F&G: {fng_value}) + 売られすぎ (RSI: {current_rsi:.1f})。反転兆候待ち。")

        # D: トレンドフォロー押し目
        if signal == "NEUTRAL" and regime == "TRENDING" and trend == "UP" and current_rsi < 45:
            signal = "BUY"
            confidence = 0.55
            reasons.append(f"上昇トレンド中の押し目 (RSI: {current_rsi:.1f}, ADX: {adx:.1f})")

        # E: RSI判定（ダイバージェンスで確度UP）
        if signal == "NEUTRAL":
            if current_rsi < 30:
                if has_divergence:
                    signal = "BUY"
                    confidence = 0.55
                    reasons.append(f"RSI売られすぎ ({current_rsi:.1f}) + ダイバージェンス確認。反転の可能性。")
                else:
                    signal = "WATCH LIST"
                    confidence = 0.35
                    reasons.append(f"RSI売られすぎ水準 ({current_rsi:.1f}) だが反転兆候なし。監視継続。")
            elif current_rsi > 70:
                signal = "OVERBOUGHT"
                confidence = 0.45
                reasons.append(f"RSIが買われすぎ水準 ({current_rsi:.1f})")

    # 補足情報
    if current_price > ema200:
        reasons.append("価格がEMA200の上（上昇トレンド）")
    else:
        reasons.append("価格がEMA200の下（下降トレンド）")
    if ema50 > ema200:
        reasons.append("EMA50 > EMA200（ゴールデンクロス圏内）")
    else:
        reasons.append("EMA50 < EMA200（デッドクロス圏内）")
    if news_risk == "NORMAL_CORRECTION":
        reasons.append("ニュース分析: 一時的な調整局面（構造的リスクなし）")

    # 6. ケリー基準でポジションサイズ算出
    kelly_pct = _calculate_kelly(confidence)
    if regime == "VOLATILE":
        kelly_pct *= 0.5
        reasons.append(f"高ボラティリティ環境のためポジション半減 (ATR%: {atr_pct:.2f}%)")

    # 6b. DCA計画（分割エントリー水準）
    dca_entry1 = current_price * 0.95
    dca_entry2 = current_price * 0.90
    dca_entry3 = current_price * 0.85
    dca_stop = current_price * 0.88

    signal_emoji = {
        "STRONG BUY": "🔥", "BUY": "🟢", "WATCH LIST": "👀",
        "WAIT": "⏳", "STAY AWAY": "⛔",
        "OVERBOUGHT": "🔴", "NEUTRAL": "⚪"
    }.get(signal, "⚪")
    analysis_text = "\n".join([f"- {r}" for r in reasons]) if reasons else "- 特筆すべきシグナルなし。様子見推奨。"

    # 7. Geminiで初心者向け要約を生成
    print(f"  🤖 Generating beginner-friendly summary...")
    beginner_summary = ""
    dca_text = (f"DCA Entry1: ${dca_entry1:,.2f}(-5%), Entry2: ${dca_entry2:,.2f}(-10%), "
                f"Entry3: ${dca_entry3:,.2f}(-15%), Stop: ${dca_stop:,.2f}(-12%)")
    try:
        beginner_prompt = f"""あなたは仮想通貨の初心者向けアドバイザーです。
以下のテクニカル分析結果を、仮想通貨をこれから始めたい人でもわかるように、やさしい日本語で解説してください。

【分析データ】
- 通貨: {symbol}
- 現在価格: ${current_price:,.2f}（24h変動: {change_24h:+.1f}%）
- シグナル: {signal}（推定勝率: {confidence*100:.0f}%）
- 市場レジーム: {regime}（ADX={adx:.1f}, ボラティリティ={atr_pct:.2f}%）
- トレンド: {trend}
- RSI: {current_rsi:.1f}（30以下=売られすぎ、70以上=買われすぎ）
- EMA50: ${ema50:,.2f} / EMA200: ${ema200:,.2f}
- Fear & Greed Index: {fng_value} ({fng_class})
- ニュースリスク判定: {news_risk}
- 流動性回収（ストップ狩り）: {'検知あり' if is_sweep else 'なし'}
- RSIダイバージェンス（反転の兆し）: {'検知あり' if has_divergence else 'なし'}
- 落ちるナイフ判定: {'⚠️危険' if is_falling_knife else '安全'}
- 推奨ポジションサイズ: 資金の{kelly_pct:.1f}%（ケリー基準）
- DCA計画: {dca_text}
- 判定理由: {', '.join(reasons) if reasons else '特になし'}

【執筆ルール】
- 以下の見出し構成で書く（見出しは「**太字**」で表現する）:
  **今の状況** … 現在の価格・トレンド・なぜ動いているか（ニュース分析結果を踏まえて）を2〜3文で。
  **なぜこのシグナルか** … 判定理由を噛み砕いて2〜3文で。落ちるナイフやダイバージェンスも触れる。
  **具体的にどうすればいい？** … ケリー基準のポジションサイズとDCA（分割購入）戦略を初心者向けに2〜3文で。
  **初心者へのアドバイス** … 具体的にどうすべきか1〜2文で。
- 各見出しの後は1行空けて段落を分ける。
- 専門用語は必ず（）内に簡単な説明を添える。例: RSI（相場の過熱感を測る指標）
- 投資は自己責任であることを最後に一言添える。
- 絵文字は使わない。"""
        beginner_resp = client.models.generate_content(
            model=MODEL_NAME, contents=beginner_prompt
        )
        beginner_summary = beginner_resp.text.strip()
        print(f"  ✅ Beginner summary: {len(beginner_summary)} chars")
    except Exception as e:
        print(f"  ⚠️ Beginner summary generation failed: {e}")
        beginner_summary = "（初心者向け要約の生成に失敗しました）"

    beginner_callout = "\n".join(
        f"> {line}" if line.strip() else ">"
        for line in beginner_summary.split("\n")
    )

    # 8. レポート作成
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    sweep_str = f"検知あり (${sweep_level:,.2f})" if is_sweep else "なし"
    div_str = "検知あり ✅" if has_divergence else "なし"
    knife_str = "⚠️ 危険" if is_falling_knife else "安全"
    risk_label = {"SYSTEMIC_RISK": "⛔ 構造的リスク", "NORMAL_CORRECTION": "📉 一時的調整",
                  "NEUTRAL": "✅ 特になし"}.get(news_risk, "✅ 特になし")

    report = (
        f"# {signal_emoji} Crypto Analysis: {symbol} ({today_str})\n\n"
        f"> [!SUMMARY] 判定結果\n"
        f"> **Signal: {signal}** | Regime: {regime} | Trend: {trend} | Price: ${current_price:,.2f} ({change_24h:+.1f}%)\n"
        f"> 推定勝率: {confidence*100:.0f}% | 推奨ポジション: 資金の{kelly_pct:.1f}% | リスク: {risk_label}\n\n"
        f"> [!TIP] 初心者向けまとめ\n"
        f"{beginner_callout}\n\n"
        f"## 🕵️ マーケットドライバー (Why)\n"
        f"{news_summary}\n\n"
        f"## 📊 テクニカル指標\n"
        f"| 指標 | 値 | 基準 |\n"
        f"|---|---|---|\n"
        f"| Price | ${current_price:,.2f} ({change_24h:+.1f}%) | - |\n"
        f"| RSI (14) | {current_rsi:.1f} | <30: 買い / >70: 売り |\n"
        f"| EMA 50 | ${ema50:,.2f} | - |\n"
        f"| EMA 200 | ${ema200:,.2f} | - |\n"
        f"| Fear & Greed | {fng_value} ({fng_class}) | <20: 極度の恐怖 |\n"
        f"| ADX (14) | {adx:.1f} | >25: トレンド有 |\n"
        f"| ATR% | {atr_pct:.2f}% | >3%: 高ボラ |\n\n"
        f"## 🧠 構造分析 (Strategy 2.0)\n"
        f"| 項目 | 結果 |\n"
        f"|---|---|\n"
        f"| 市場レジーム | {regime} |\n"
        f"| ニュースリスク | {risk_label} |\n"
        f"| 落ちるナイフ | {knife_str} |\n"
        f"| RSIダイバージェンス | {div_str} |\n"
        f"| 流動性回収 (SMC) | {sweep_str} |\n"
        f"| 推定勝率 | {confidence*100:.0f}% |\n"
        f"| 推奨ポジション (Kelly) | 資金の**{kelly_pct:.1f}%** |\n\n"
    )

    # DCA計画セクション（BUY系シグナルのみ）
    if signal in ["STRONG BUY", "BUY", "WATCH LIST"]:
        report += (
            f"## 📋 DCA計画 (分割エントリー)\n"
            f"| レベル | 価格 | 現在値から |\n"
            f"|---|---|---|\n"
            f"| Entry 1 | ${dca_entry1:,.2f} | -5% |\n"
            f"| Entry 2 | ${dca_entry2:,.2f} | -10% |\n"
            f"| Entry 3 | ${dca_entry3:,.2f} | -15% |\n"
            f"| Stop Loss | ${dca_stop:,.2f} | -12% |\n\n"
        )

    report += f"## 📝 分析\n{analysis_text}\n"

    # 9. 03_Cryptoに保存
    clean_symbol = symbol.replace('/', '_')
    filename = f"Crypto_{clean_symbol}_{today_str}.md"
    save_dir = os.path.join(VAULT_ROOT, CRYPTO_DIR_NAME)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)

    with open(save_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✅ Saved: {save_path}")

    # 10. BUY/STRONG BUY なら n8n Webhook に通知
    if signal in ["BUY", "STRONG BUY"]:
        webhook_url = os.getenv("N8N_WEBHOOK_URL", "")
        if webhook_url:
            try:
                payload = {
                    "text": f"{signal_emoji} {symbol} **{signal}**\n"
                            f"Price: ${current_price:,.2f} ({change_24h:+.1f}%) | Regime: {regime}\n"
                            f"RSI: {current_rsi:.1f} | F&G: {fng_value} | Risk: {news_risk}\n"
                            f"Kelly: {kelly_pct:.1f}% | Divergence: {'Yes' if has_divergence else 'No'}",
                    "signal": signal,
                    "symbol": symbol,
                    "price": current_price,
                    "change_24h": round(change_24h, 2),
                    "rsi": round(current_rsi, 1),
                    "fng": fng_value,
                    "regime": regime,
                    "kelly_pct": round(kelly_pct, 1),
                    "sweep": is_sweep,
                    "divergence": has_divergence,
                    "falling_knife": is_falling_knife,
                    "news_risk": news_risk
                }
                req.post(webhook_url, json=payload, timeout=10)
                print(f"  🚨 n8n notification sent!")
            except Exception as e:
                print(f"  ⚠️ n8n notification failed: {e}")
        else:
            print(f"  ⚠️ N8N_WEBHOOK_URL not set, skipping notification")

    # 11. BUY系シグナルなら予測をtrade_logに記録（Prediction Audit System）
    if signal in ["BUY", "STRONG BUY", "WATCH LIST"]:
        try:
            _save_prediction(symbol, signal, current_price, dca_stop, kelly_pct, reasons)
        except Exception as e:
            print(f"  ⚠️ Prediction log failed: {e}")

    return f"Success: {symbol} = **{signal}** (RSI:{current_rsi:.1f}, F&G:{fng_value}, Risk:{news_risk}). Report: [[{CRYPTO_DIR_NAME}/{filename}]]"


# 主要アルトコインのスキャン対象リスト
SCAN_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "MATIC/USDT",
    "ATOM/USDT", "UNI/USDT", "LTC/USDT", "NEAR/USDT", "APT/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "FIL/USDT", "INJ/USDT",
]


def tool_scan_crypto_market() -> str:
    """
    主要20銘柄を一括スキャンし、RSI・出来高変化率・Fear&Greedから
    注目すべき通貨をランキング形式でピックアップする。
    結果を03_Crypto/Market_Scan_{date}.mdに保存。
    """
    print(f"[Tool] 🔭 Scanning Crypto Market ({len(SCAN_SYMBOLS)} symbols)...")
    import requests as req

    exchange = ccxt.binance({'enableRateLimit': True})

    # Fear & Greed Index（全銘柄共通）
    fng_value = 50
    fng_class = "Neutral"
    try:
        fng_resp = req.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        fng_data = fng_resp.json()['data'][0]
        fng_value = int(fng_data['value'])
        fng_class = fng_data['value_classification']
    except Exception as e:
        print(f"  ⚠️ F&G fetch failed: {e}")

    results = []
    for symbol in SCAN_SYMBOLS:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '4h', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            rsi_series = ta_lib.rsi(df['close'], length=14)
            current_rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and pd.notna(rsi_series.iloc[-1]) else 50.0
            current_price = float(df['close'].iloc[-1])

            # 出来高変化率（直近5本 vs 前20本平均）
            recent_vol = df['volume'].iloc[-5:].mean()
            past_vol = df['volume'].iloc[-25:-5].mean()
            vol_change = ((recent_vol / past_vol) - 1) * 100 if past_vol > 0 else 0.0

            # EMA200 トレンド判定
            ema200_s = ta_lib.ema(df['close'], length=200)
            ema200 = float(ema200_s.iloc[-1]) if ema200_s is not None and pd.notna(ema200_s.iloc[-1]) else current_price
            trend = "UP" if current_price > ema200 else "DOWN"

            # シグナル判定
            if current_rsi < 30 and fng_value < 20:
                signal = "STRONG BUY"
            elif current_rsi < 30:
                signal = "BUY"
            elif current_rsi > 70:
                signal = "OVERBOUGHT"
            else:
                signal = "NEUTRAL"

            # スコアリング（低いほど注目: RSI低い＋出来高急増＋恐怖 = 高スコア）
            score = 0
            if current_rsi < 30:
                score += 3
            elif current_rsi < 40:
                score += 1
            elif current_rsi > 70:
                score += 2  # 過熱も注目
            if vol_change > 50:
                score += 2
            elif vol_change > 20:
                score += 1
            if fng_value < 20:
                score += 2

            results.append({
                "symbol": symbol,
                "price": current_price,
                "rsi": current_rsi,
                "vol_change": vol_change,
                "trend": trend,
                "signal": signal,
                "score": score,
            })
            print(f"  ✅ {symbol}: RSI={current_rsi:.1f}, Vol={vol_change:+.1f}%, {signal}")
            time.sleep(0.3)

        except Exception as e:
            print(f"  ⚠️ {symbol} skipped: {e}")

    if not results:
        return "Error: Could not fetch any crypto data."

    # スコア降順 → RSI昇順でソート
    results.sort(key=lambda x: (-x['score'], x['rsi']))

    # Geminiで初心者向け解説を生成
    print(f"  🤖 Generating beginner-friendly market overview...")
    top5 = results[:5]
    top5_text = "\n".join([
        f"- {r['symbol']}: 価格=${r['price']:,.2f}, RSI={r['rsi']:.1f}, "
        f"出来高変化={r['vol_change']:+.1f}%, トレンド={r['trend']}, シグナル={r['signal']}"
        for r in top5
    ])
    beginner_overview = ""
    try:
        overview_prompt = f"""あなたは仮想通貨の初心者向けアドバイザーです。
以下は本日の仮想通貨市場スキャン結果のうち、注目度の高い上位5銘柄です。
仮想通貨をこれから始めたい人向けに、やさしい日本語で解説してください。

【市場の温度感】
Fear & Greed Index: {fng_value} ({fng_class})

【注目銘柄 TOP5】
{top5_text}

【執筆ルール】
- 以下の見出し構成で書く（見出しは「**太字**」で表現する）:
  **市場の温度感** … Fear & Greedの数値が何を意味するか1〜2文で。
  **注目銘柄ピックアップ** … 各銘柄1〜2文で「なぜ注目か」を説明。
  **アルトコイン投資の注意点** … 初心者向けの注意点を1〜2文で。
- 各見出しの後は1行空けて段落を分ける。
- 専門用語には必ず（）で簡単な説明を添える。
- 投資は自己責任であることを一言添える。
- 絵文字は使わない。"""
        overview_resp = client.models.generate_content(
            model=MODEL_NAME, contents=overview_prompt
        )
        beginner_overview = overview_resp.text.strip()
        print(f"  ✅ Overview: {len(beginner_overview)} chars")
    except Exception as e:
        print(f"  ⚠️ Overview generation failed: {e}")
        beginner_overview = "（初心者向け解説の生成に失敗しました）"

    # Callout用に各行に "> " を付与
    overview_callout = "\n".join(
        f"> {line}" if line.strip() else ">"
        for line in beginner_overview.split("\n")
    )

    # レポート作成
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    signal_emoji_map = {"STRONG BUY": "🔥", "BUY": "🟢", "OVERBOUGHT": "🔴", "NEUTRAL": "⚪"}

    table_rows = ""
    for r in results:
        emoji = signal_emoji_map.get(r['signal'], "⚪")
        table_rows += (
            f"| {emoji} {r['symbol']} | ${r['price']:,.2f} | "
            f"{r['rsi']:.1f} | {r['vol_change']:+.1f}% | "
            f"{r['trend']} | {r['signal']} |\n"
        )

    report = (
        f"# 🔭 Market Scan ({today_str})\n\n"
        f"> [!SUMMARY] 市場概況\n"
        f"> Fear & Greed: **{fng_value}** ({fng_class}) | スキャン銘柄数: {len(results)}\n\n"
        f"> [!TIP] 初心者向け：今日の注目ポイント\n"
        f"{overview_callout}\n\n"
        f"## 📊 全銘柄スキャン結果\n"
        f"| 銘柄 | 価格 | RSI | 出来高変化 | トレンド | シグナル |\n"
        f"|---|---|---|---|---|---|\n"
        f"{table_rows}\n"
        f"## 🏆 注目銘柄 TOP5\n"
    )
    for i, r in enumerate(top5, 1):
        emoji = signal_emoji_map.get(r['signal'], "⚪")
        report += (
            f"### {i}. {emoji} {r['symbol']}\n"
            f"- 価格: ${r['price']:,.2f}\n"
            f"- RSI: {r['rsi']:.1f} / 出来高変化: {r['vol_change']:+.1f}%\n"
            f"- トレンド: {r['trend']} / シグナル: **{r['signal']}**\n\n"
        )

    # 保存
    filename = f"Market_Scan_{today_str}.md"
    save_dir = os.path.join(VAULT_ROOT, CRYPTO_DIR_NAME)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)

    with open(save_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✅ Saved: {save_path}")

    # 結果サマリー
    buy_signals = [r for r in results if r['signal'] in ["BUY", "STRONG BUY"]]
    summary = f"Scanned {len(results)} symbols. "
    if buy_signals:
        names = ", ".join([r['symbol'] for r in buy_signals])
        summary += f"Buy signals: {names}. "
    else:
        summary += "No buy signals found. "
    summary += f"F&G: {fng_value}. Report: [[{CRYPTO_DIR_NAME}/{filename}]]"

    return f"Success: {summary}"


# ---------------------------------------------------------
# Prediction Audit Tool
# ---------------------------------------------------------

def tool_audit_predictions() -> str:
    """
    過去の予測ログ(trade_log.json)を読み込み、
    Binanceの実績価格と照合してWIN/LOSEを判定する。
    LOSEの場合はTavilyでニュース検索し敗因を分析。
    結果をDev_Logs/Audit_Report_{date}.mdに保存する。
    """
    print("[Tool] 🕵️ Auditing past predictions...")
    import requests as req

    logs = _load_trade_log()
    open_trades = [t for t in logs if t["status"] == "OPEN"]

    if not open_trades:
        return "評価待ちの予測（OPEN）はありません。"

    exchange = ccxt.binance({'enableRateLimit': True})
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')

    report = f"# 🕵️ AI Prediction Audit Report ({today_str})\n\n"
    report += f"> [!SUMMARY] 概要\n"
    report += f"> 評価対象: {len(open_trades)}件のOPENポジション\n\n"

    updated_count = 0
    win_count = 0
    lose_count = 0

    for trade in open_trades:
        symbol = trade["symbol"]
        entry_time = datetime.datetime.fromisoformat(trade["timestamp"])
        since_ts = int(entry_time.timestamp() * 1000)

        print(f"  📊 Checking {symbol} (ID: {trade['id']}, entered {trade['timestamp'][:10]})...")

        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '1h', since=since_ts, limit=500)
            if not ohlcv:
                print(f"    ⚠️ No data returned for {symbol}")
                continue
            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        except Exception as e:
            report += f"- ⚠️ {symbol} (ID: {trade['id']}): データ取得失敗 ({e})\n"
            print(f"    ⚠️ Data fetch error: {e}")
            continue

        lowest = float(df['low'].min())
        highest = float(df['high'].max())
        current_close = float(df['close'].iloc[-1])
        hours_elapsed = len(df)

        new_status = "OPEN"
        audit_comment = ""

        # 判定: SLヒット → LOSE, TPヒット → WIN
        if lowest <= trade["stop_loss"]:
            new_status = "LOSE"
            lose_count += 1

            # 敗因分析: Tavily で当時のニュースを検索
            coin_name = symbol.split('/')[0]
            query = f"Why did {coin_name} price drop crypto news {entry_time.strftime('%Y-%m')}"
            try:
                search_res = tavily_client.search(query=query, topic="news", max_results=3)
                news_items = []
                for r in search_res.get('results', []):
                    title = r.get('title', 'No title')
                    url = r.get('url', '')
                    news_items.append(f"[{title}]({url})")
                news_text = "; ".join(news_items) if news_items else "関連ニュース見つからず"
            except Exception as e:
                news_text = f"ニュース検索失敗: {e}"

            loss_pct = ((trade["stop_loss"] - trade["entry_price"]) / trade["entry_price"]) * 100
            audit_comment = f"損切りライン(${trade['stop_loss']:,.2f})に到達。損失: {loss_pct:+.1f}%。敗因: {news_text}"

        elif highest >= trade["target_price"]:
            new_status = "WIN"
            win_count += 1
            gain_pct = ((trade["target_price"] - trade["entry_price"]) / trade["entry_price"]) * 100
            audit_comment = f"利確目標(${trade['target_price']:,.2f})達成。利益: +{gain_pct:.1f}%。予測通りの値動き。"

        else:
            # まだ決着がついていない
            dist_to_tp = ((trade["target_price"] - current_close) / current_close) * 100
            dist_to_sl = ((current_close - trade["stop_loss"]) / current_close) * 100
            audit_comment = f"未決着 ({hours_elapsed}h経過)。現在値: ${current_close:,.2f} (TP まで {dist_to_tp:+.1f}%, SL まで {dist_to_sl:.1f}%)"

        # ログ更新
        if new_status != "OPEN":
            trade["status"] = new_status
            trade["audit_note"] = audit_comment
            trade["audit_date"] = today_str
            updated_count += 1

            icon = "✅" if new_status == "WIN" else "❌"
            report += (
                f"## {icon} {symbol} — {new_status} (ID: {trade['id']})\n"
                f"| 項目 | 値 |\n|---|---|\n"
                f"| シグナル | {trade['signal']} |\n"
                f"| エントリー | ${trade['entry_price']:,.2f} |\n"
                f"| ターゲット | ${trade['target_price']:,.2f} |\n"
                f"| ストップ | ${trade['stop_loss']:,.2f} |\n"
                f"| Kelly% | {trade['kelly_pct']}% |\n"
                f"| 予測日 | {trade['timestamp'][:10]} |\n"
                f"| 判定理由 | {trade.get('reason', 'N/A')[:80]} |\n\n"
                f"**監査結果**: {audit_comment}\n\n"
            )
            print(f"    {icon} {new_status}: {audit_comment[:80]}...")
        else:
            print(f"    ⏳ Still OPEN: {audit_comment[:80]}...")

        time.sleep(0.3)

    # JSONログ更新
    _save_trade_log(logs)

    # 統計セクション
    total_closed = win_count + lose_count
    if total_closed > 0:
        win_rate = (win_count / total_closed) * 100
        report += (
            f"## 📈 統計\n"
            f"| 項目 | 値 |\n|---|---|\n"
            f"| 判定済み | {total_closed}件 |\n"
            f"| WIN | {win_count}件 |\n"
            f"| LOSE | {lose_count}件 |\n"
            f"| 勝率 | {win_rate:.1f}% |\n"
            f"| 未決着 | {len(open_trades) - total_closed}件 |\n\n"
        )

    if updated_count == 0:
        still_open = len(open_trades)
        return f"OPEN {still_open}件を確認しましたが、まだSL/TPに到達していません。"

    # レポート保存
    report_filename = f"Audit_Report_{today_str}.md"
    report_dir = os.path.join(VAULT_ROOT, CRYPTO_DIR_NAME, "Audit_Report")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, report_filename)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✅ Audit report saved: {report_path}")

    return f"Success: {updated_count}件を判定 (WIN:{win_count}, LOSE:{lose_count})。レポート: [[{CRYPTO_DIR_NAME}/Audit_Report/{report_filename}]]"


# ---------------------------------------------------------
# Curator Wrapper (lazy import to avoid circular dependency)
# ---------------------------------------------------------

def _tool_curate_wrapper() -> str:
    """The Curator のトピック提案をReActツールとして公開する。"""
    try:
        from tool_curator import read_main_context, get_curation_suggestions
        context = read_main_context()
        suggestions = get_curation_suggestions(context, "normal")
        topics = suggestions.get("topics", [])
        if not topics:
            return "No suggestions generated."
        lines = [f"- {t['title']}: {t['description']}" for t in topics]
        return "Suggested topics:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------
# Plan-and-Execute Engine
# ---------------------------------------------------------

# tools_map / config を共有するためのヘルパー
def _get_tools_config():
    """ツール一覧と実行マップを返す（ReAct / Plan-and-Execute 共用）。"""
    # Calendar/Task tools (lazy import)
    try:
        from tool_calendar import (
            tool_get_calendar, tool_add_event,
            tool_create_task, tool_get_tasks,
            tool_sync_to_daily_note,
        )
        _has_calendar = True
    except ImportError:
        _has_calendar = False

    config_tools = [
        {"name": "tool_web_search", "usage": "tool_web_search(query)", "description": "Web検索 (Tavily)"},
        {"name": "tool_vault_search", "usage": "tool_vault_search(keywords)", "description": "Vault内を検索"},
        {"name": "tool_create_note", "usage": "tool_create_note(filename, content, summary)", "description": "ファイル作成"},
        {"name": "tool_read_note", "usage": "tool_read_note(filename)", "description": "ファイル読込"},
        {"name": "tool_update_note", "usage": "tool_update_note(filename, append_text)", "description": "追記"},
        {"name": "tool_get_youtube_transcript", "usage": "tool_get_youtube_transcript(url='URL')", "description": "YouTube文字起こし取得"},
        {"name": "tool_merge_weekly_reports", "usage": "tool_merge_weekly_reports()", "description": "週次レポートのマージとアーカイブ"},
        {"name": "tool_fetch_daily_news", "usage": "tool_fetch_daily_news()", "description": "ニュース収集・編集・保存"},
        {"name": "tool_fetch_url", "usage": "tool_fetch_url(url)", "description": "Webページのテキスト取得"},
        {"name": "tool_analyze_crypto", "usage": "tool_analyze_crypto(symbol='BTC/USDT')", "description": "仮想通貨クオンツ分析"},
        {"name": "tool_scan_crypto_market", "usage": "tool_scan_crypto_market()", "description": "主要20銘柄一括スキャン"},
        {"name": "tool_audit_predictions", "usage": "tool_audit_predictions()", "description": "予測ログの答え合わせ"},
        {"name": "tool_curate_topics", "usage": "tool_curate_topics()", "description": "ナレッジトピック提案"},
    ]
    tools_map = {
        "tool_web_search": tool_web_search,
        "tool_vault_search": tool_vault_search,
        "tool_create_note": tool_create_note,
        "tool_read_note": tool_read_note,
        "tool_update_note": tool_update_note,
        "tool_get_youtube_transcript": tool_get_youtube_transcript,
        "tool_merge_weekly_reports": tool_merge_weekly_reports,
        "tool_fetch_daily_news": tool_fetch_daily_news,
        "tool_fetch_url": tool_fetch_url,
        "tool_analyze_crypto": tool_analyze_crypto,
        "tool_scan_crypto_market": tool_scan_crypto_market,
        "tool_audit_predictions": tool_audit_predictions,
        "tool_curate_topics": _tool_curate_wrapper,
    }

    # Calendar/Task tools を動的に追加
    if _has_calendar:
        config_tools.extend([
            {"name": "tool_get_calendar", "usage": "tool_get_calendar(date='today', days=1)", "description": "Google Calendarから予定を取得"},
            {"name": "tool_add_event", "usage": "tool_add_event(title, start_time, end_time='', description='')", "description": "Google Calendarに予定を追加"},
            {"name": "tool_create_task", "usage": "tool_create_task(title, due_date='', priority=1, project='Inbox')", "description": "Todoistにタスクを追加"},
            {"name": "tool_get_tasks", "usage": "tool_get_tasks(filter='today')", "description": "Todoistからタスク一覧を取得"},
            {"name": "tool_sync_to_daily_note", "usage": "tool_sync_to_daily_note()", "description": "予定とタスクをデイリーノートに同期"},
        ])
        tools_map.update({
            "tool_get_calendar": tool_get_calendar,
            "tool_add_event": tool_add_event,
            "tool_create_task": tool_create_task,
            "tool_get_tasks": tool_get_tasks,
            "tool_sync_to_daily_note": tool_sync_to_daily_note,
        })

    return config_tools, tools_map


def generate_plan(user_query: str) -> dict:
    """ユーザーのクエリから実行計画を生成する。

    Returns:
        {
            "plan": [
                {"step": 1, "task": "...", "tool": "...", "args_hint": "..."},
                ...
            ],
            "expected_outcome": "最終的に何が達成されるか"
        }
    """
    config_tools, _ = _get_tools_config()
    tools_desc = "\n".join(
        f"- {t['name']}: {t['description']}  使い方: {t['usage']}"
        for t in config_tools
    )

    # Self-Improvement: 過去の学習からヒントを注入
    learning_hint = get_enhanced_prompt(user_query)
    learning_block = f"\n\n{learning_hint}" if learning_hint else ""

    today_str = datetime.datetime.now().strftime("%Y年%m月%d日")

    prompt = f"""あなたはタスクプランナーです。
ユーザーの要求を達成するための実行計画を作成してください。

【現在の日付】: {today_str}
※ 「最新」「今年」などの表現は、上記の日付を基準にしてください。

【要求】: {user_query}

【利用可能なツール】:
{tools_desc}

【ルール】
- 各ステップは1つのツール呼び出しに対応させる
- 情報収集 → 加工・保存 → 完了報告 の順序を守る
- ニュース収集には tool_fetch_daily_news のみを使い、tool_create_note での二重作成は禁止
- レポート・要約・分析の作成を求められた場合、必ず tool_create_note で30_Mainに保存する
- 必要なステップだけに絞り、冗長な計画にしない（最大6ステップ）

【制約の抽出と遵守】（重要！）
ユーザーの要求から以下のパターンで「禁止事項」を抽出し、厳守してください：
- 「〜せずに」「〜なしで」「〜を使わないで」「〜は禁止」「〜を避けて」
  → 該当するツールや行動を計画から除外する
- 例: 「Web検索せずに」→ tool_web_search, tool_fetch_daily_news, tool_fetch_url を使用禁止
- 例: 「ファイルを作成せずに」→ tool_create_note を使用禁止

【制約がある場合の代替案】（必ず代替手段を探す！空のplanは禁止！）
- Web検索禁止で「ニュース」を求められた場合:
  Step 1: tool_vault_search(keywords="news ニュース") で過去のニュースファイルを探す
  Step 2: tool_read_note でそのファイルを読んで内容を返す
- ファイル作成禁止の場合: 結果をテキストで返すだけにする
- **重要**: 代替手段がある限り、必ず1つ以上のステップを持つ計画を作成すること
- 「実行不可」と返すのは本当にどうしようもない場合のみ

【ツール選択の指針】
- **外部情報が必要な場合** (最新ニュース、一般知識、技術情報など): tool_web_search を使う
- **ユーザーの過去のメモや文脈が必要な場合**: tool_vault_search を使う
- **両方が必要な場合**: まず tool_web_search で外部情報を取得し、その後 tool_vault_search で関連する過去ノートを探す
- **URLが与えられた場合**: tool_fetch_url でページ内容を取得する

{learning_block}
【出力フォーマット (JSONのみ)】:
{{
    "plan": [
        {{"step": 1, "task": "タスクの説明", "tool": "使用するツール名", "args_hint": "引数のヒント"}},
        {{"step": 2, "task": "タスクの説明", "tool": "使用するツール名", "args_hint": "引数のヒント"}}
    ],
    "expected_outcome": "最終的に何が達成されるか"
}}"""

    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return safe_json_loads(response.text)


def execute_plan(plan: list, step_callback=None) -> dict:
    """計画を順番に実行する。

    Args:
        plan: generate_plan() が返した plan リスト
        step_callback: Discord等へのリアルタイム通知用コールバック

    Returns:
        {"success": bool, "results": [...], "final_output": "..."}
    """
    _, tools_map = _get_tools_config()
    results = []
    accumulated_context = []

    for item in plan:
        step_num = item.get("step", len(results) + 1)
        task = item.get("task", "")
        tool_name = item.get("tool", "")
        args_hint = item.get("args_hint", "")

        print(f"[P&E] ▶️ Step {step_num}: {task} (tool={tool_name})")

        if step_callback:
            step_callback({
                "phase": "execute",
                "step": step_num,
                "total": len(plan),
                "task": task,
                "tool": tool_name,
                "status": "running",
            })

        # ツールの引数を Gemini に解決させる
        args = _resolve_tool_args(tool_name, args_hint, task, accumulated_context)

        if tool_name not in tools_map:
            observation = f"Error: Unknown tool '{tool_name}'"
            success = False
        else:
            try:
                observation = tools_map[tool_name](**args)
                success = True
            except Exception as e:
                observation = f"Error: {e}"
                success = False

        print(f"  {'✅' if success else '❌'} {str(observation)[:200]}...")

        step_result = {
            "step": step_num,
            "task": task,
            "tool": tool_name,
            "args": args,
            "observation": observation,
            "success": success,
        }
        results.append(step_result)
        accumulated_context.append(step_result)

        if step_callback:
            step_callback({
                "phase": "execute",
                "step": step_num,
                "total": len(plan),
                "task": task,
                "tool": tool_name,
                "status": "done" if success else "error",
                "observation": str(observation)[:500],
            })

        if not success:
            print(f"[P&E] ⚠️ Step {step_num} failed, passing to Critic...")
            break

    all_ok = all(r["success"] for r in results)
    return {
        "success": all_ok,
        "results": results,
        "final_output": _summarize_results(results) if all_ok else "",
    }


def _resolve_tool_args(tool_name: str, args_hint: str, task: str,
                        context: list) -> dict:
    """Gemini を使ってツールの引数を具体化する。"""
    if not args_hint and tool_name in (
        "tool_merge_weekly_reports", "tool_fetch_daily_news",
        "tool_scan_crypto_market", "tool_audit_predictions",
        "tool_curate_topics",
    ):
        return {}

    # ツールのシグネチャ（必須引数情報）を取得
    config_tools, _ = _get_tools_config()
    tool_usage = ""
    for t in config_tools:
        if t["name"] == tool_name:
            tool_usage = t["usage"]
            break

    ctx_summary = ""
    if context:
        ctx_summary = "\n".join(
            f"Step {c['step']}: {c['task']} → {str(c['observation'])[:500]}"
            for c in context[-3:]
        )

    today_str = datetime.datetime.now().strftime("%Y年%m月%d日")

    prompt = f"""以下のツール呼び出しに必要な引数をJSON辞書で出力してください。

【現在の日付】: {today_str}
※ 「最新」「今年」などは{today_str[:4]}年を指します。検索クエリにも正しい年を使用してください。

ツール: {tool_name}
シグネチャ: {tool_usage}
タスク: {task}
引数ヒント: {args_hint}

これまでの実行結果（引数の値はここから取得してください）:
{ctx_summary}

【重要な注意事項】
- シグネチャにある引数は必ず全て含めてください
- tool_web_search の場合:
  - query: 「最新」と言われたら {today_str[:4]}年 を含めてください（例: "AI 最新トレンド {today_str[:4]}"）
- tool_create_note の場合:
  - filename: レポートのタイトル（日本語OK）
  - content: レポートの本文（これまでの実行結果から内容を生成してください）
  - summary: 1-2行の要約
- 引数の値は「これまでの実行結果」の observation から抽出・生成してください
- 引数が不要なら空の辞書 {{}} を返してください

JSONのみ出力（説明不要）:"""

    for attempt in range(3):
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            args = safe_json_loads(response.text)
            # 必須引数チェック (tool_create_note)
            if tool_name == "tool_create_note":
                if "filename" not in args or "content" not in args:
                    print(f"[P&E] ⚠️ Missing required args for tool_create_note, retry {attempt+1}/3")
                    continue
            return args
        except Exception as e:
            print(f"[P&E] ⚠️ _resolve_tool_args error: {e}, retry {attempt+1}/3")
    # 最終手段: デフォルト引数
    if tool_name == "tool_create_note":
        return {"filename": "Report.md", "content": "生成に失敗しました", "summary": ""}
    return {}


def _summarize_results(results: list) -> str:
    """実行結果リストから最終サマリーを生成する。"""
    parts = []
    for r in results:
        obs = str(r.get("observation", ""))
        if obs.startswith("Success:") or obs.startswith("✅"):
            parts.append(f"- Step {r['step']}: {obs[:200]}")
        else:
            parts.append(f"- Step {r['step']} ({r['task']}): 完了")
    return "\n".join(parts)


def critique_results(user_query: str, plan: list, results: list) -> dict:
    """実行結果を評価し、必要なら修正計画を返す。

    Returns:
        {
            "status": "ok" | "needs_revision" | "failed",
            "feedback": "フィードバック内容",
            "revised_plan": [...] (needs_revision の場合のみ)
        }
    """
    config_tools, _ = _get_tools_config()
    valid_tools = [t["name"] for t in config_tools]
    valid_tools_str = ", ".join(valid_tools)

    results_summary = json.dumps(
        [{"step": r["step"], "task": r["task"], "success": r["success"],
          "observation": str(r["observation"])[:800]}
         for r in results],
        ensure_ascii=False, indent=2,
    )

    plan_summary = "\n".join(
        f"  {p.get('step', i+1)}. {p.get('task', '')} [{p.get('tool', '')}]"
        for i, p in enumerate(plan)
    )

    prompt = f"""あなたはタスク評価者（Critic）です。
実行結果を評価し、ユーザーの目標が達成されたか判定してください。

【ユーザーの要求】: {user_query}

【元の計画】:
{plan_summary}

【実行結果】:
{results_summary}

【利用可能なツール（これ以外は使用禁止）】:
{valid_tools_str}

【ユーザー制約の遵守】（revised_plan作成時に厳守！）
- ユーザーの要求に「〜せずに」「〜なしで」「〜を使わないで」「〜禁止」があれば、該当ツールは使用禁止
- 例: 「Web検索せずに」→ revised_plan に tool_web_search, tool_fetch_daily_news, tool_fetch_url を含めてはいけない
- 代替手段がある場合は必ずその代替案で revised_plan を作成すること

【評価基準】
1. 全ステップが成功しているか
2. ユーザーの目標が達成されたか（情報が十分か、ファイルが作成されたか等）
3. **コンテンツ品質チェック**（最重要）:
   - 「調べて」「まとめて」系の要求 → observationに具体的な情報（手順、数字、固有名詞）が含まれているか？
   - 一般論や概要だけで具体性がない場合 → needs_revision（「具体的な手順/データを追加検索せよ」と指示）
   - 例: 「ストレッチの方法を調べて」→ 具体的なストレッチ名・やり方・回数がなければ不十分
4. 不足があれば、追加で必要なステップを提案
- **重要**: revised_planで提案するツールは上記の利用可能なツールのみ使用可能
- **重要**: revised_planはユーザーの制約を違反してはいけない

【revision判断の基準】（時間効率を考慮！）
- 全ステップ成功 + 具体的な情報が含まれている → status=ok（revisionしない）
- ステップ失敗あり → needs_revision
- 全ステップ成功だが情報が薄い → needs_revision（ただし追加ステップは最大2つまで）
- 同じツールの再実行は避け、別の検索クエリや別ツールで補完すること

【出力フォーマット (JSONのみ)】:
{{
    "status": "ok" or "needs_revision" or "failed",
    "feedback": "評価コメント",
    "revised_plan": [
        {{"step": 1, "task": "追加タスク", "tool": "ツール名", "args_hint": "引数ヒント"}}
    ]
}}

- status=ok: 目標達成、追加作業不要
- status=needs_revision: 部分的に成功、追加ステップが必要（revised_planに記載、最大2ステップ）
- status=failed: 根本的に失敗、リトライ不可"""

    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return safe_json_loads(response.text)
    except Exception as e:
        return {"status": "ok", "feedback": f"Critic evaluation failed ({e}), accepting results."}


MAX_REVISIONS = 1  # 品質向上のため1回に制限（2回は時間浪費の原因）


def run_plan_and_execute(user_query: str, step_callback=None) -> str:
    """Plan-and-Execute パターンのメインエントリーポイント。

    1. generate_plan() で計画を作成
    2. execute_plan() で実行
    3. critique_results() で評価
    4. 必要なら再計画・再実行（最大 MAX_REVISIONS 回）
    5. log_execution() で学習DBに記録
    6. 最終結果を返す
    """
    t_start = time.time()
    print(f"[P&E] 🧠 Planning for: {user_query[:80]}...")

    if step_callback:
        step_callback({"phase": "plan", "status": "planning"})

    try:
        plan_data = generate_plan(user_query)
    except Exception as e:
        print(f"[P&E] ❌ Plan generation failed: {e}")
        return f"計画生成に失敗しました: {e}"

    plan = plan_data.get("plan", [])
    original_plan = list(plan)
    expected = plan_data.get("expected_outcome", "")

    if not plan:
        return "計画を生成できませんでした。"

    print(f"[P&E] 📋 Plan ({len(plan)} steps): {expected}")
    for p in plan:
        print(f"  {p['step']}. {p['task']} [{p['tool']}]")

    if step_callback:
        step_callback({
            "phase": "plan",
            "status": "ready",
            "plan": plan,
            "expected_outcome": expected,
        })

    all_results = []
    last_critique = {}

    for revision in range(MAX_REVISIONS + 1):
        if revision > 0:
            print(f"[P&E] 🔄 Revision {revision}/{MAX_REVISIONS}")
            if step_callback:
                step_callback({"phase": "revision", "attempt": revision})

        exec_result = execute_plan(plan, step_callback)
        all_results.extend(exec_result["results"])

        if step_callback:
            step_callback({"phase": "critique", "status": "evaluating"})

        critique = critique_results(user_query, plan, exec_result["results"])
        last_critique = critique
        print(f"[P&E] 🔍 Critic: {critique.get('status')} - {critique.get('feedback', '')[:100]}")

        if critique["status"] == "ok":
            duration = time.time() - t_start
            try:
                log_execution(user_query, original_plan, all_results, critique, duration)
            except Exception as e:
                print(f"[P&E] ⚠️ Failed to log execution: {e}")
            return _generate_final_summary(user_query, all_results)
        elif critique["status"] == "needs_revision":
            revised = critique.get("revised_plan", [])
            if revised:
                plan = revised
                print(f"[P&E] 📝 Revised plan ({len(plan)} steps)")
            else:
                duration = time.time() - t_start
                try:
                    log_execution(user_query, original_plan, all_results, critique, duration)
                except Exception as e:
                    print(f"[P&E] ⚠️ Failed to log execution: {e}")
                return _generate_final_summary(user_query, all_results)
        else:
            duration = time.time() - t_start
            try:
                log_execution(user_query, original_plan, all_results, critique, duration)
            except Exception as e:
                print(f"[P&E] ⚠️ Failed to log execution: {e}")
            return f"タスク失敗: {critique.get('feedback', 'Unknown error')}"

    duration = time.time() - t_start
    try:
        log_execution(user_query, original_plan, all_results, last_critique, duration)
    except Exception as e:
        print(f"[P&E] ⚠️ Failed to log execution: {e}")
    return _generate_final_summary(user_query, all_results)


def _generate_final_summary(user_query: str, results: list) -> str:
    """Gemini を使って実行結果から最終サマリーを生成する。"""
    results_text = "\n".join(
        f"Step {r['step']}: {r['task']} → {'✅' if r['success'] else '❌'} {str(r['observation'])[:500]}"
        for r in results
    )

    prompt = f"""以下のタスク実行結果を、ユーザーへの簡潔な報告にまとめてください。

【ユーザーの要求】: {user_query}

【実行結果】:
{results_text}

【ルール】
- 作成したファイルがあれば [[ファイル名]] 形式でリンクを記載
- 簡潔に（5行以内）
- 実行サマリーのみ（詳細はファイルに保存済み）"""

    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return response.text.strip()
    except Exception:
        return _summarize_results(results)


# ---------------------------------------------------------
# Self-Improvement Protocol
# ---------------------------------------------------------

def _load_learning_db() -> dict:
    """学習DBを読み込む。存在しなければ空のDBを返す。"""
    if os.path.exists(LEARNING_DB_PATH):
        try:
            with open(LEARNING_DB_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"executions": []}


def _save_learning_db(db: dict) -> None:
    """学習DBを保存する。"""
    with open(LEARNING_DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def log_execution(query: str, plan: list, results: list,
                  critique: dict, duration_seconds: float) -> None:
    """実行結果を学習DBに記録する。"""
    db = _load_learning_db()

    all_success = all(r.get("success", False) for r in results)
    any_success = any(r.get("success", False) for r in results)
    if all_success:
        outcome = "success"
    elif any_success:
        outcome = "partial"
    else:
        outcome = "failed"

    failure_reason = None
    failed_step = None
    for r in results:
        if not r.get("success", False):
            failed_step = r.get("step")
            obs = str(r.get("observation", ""))
            failure_reason = obs[:300]
            break

    tools_used = list(dict.fromkeys(r.get("tool", "") for r in results))

    now = datetime.datetime.now()
    exec_id = f"exec_{now.strftime('%Y%m%d')}_{len(db['executions']) + 1:03d}"

    entry = {
        "id": exec_id,
        "timestamp": now.isoformat(timespec="seconds"),
        "query": query[:200],
        "plan_steps": len(plan),
        "outcome": outcome,
        "failure_reason": failure_reason,
        "failed_step": failed_step,
        "steps_count": len(results),
        "duration_seconds": round(duration_seconds, 1),
        "tools_used": tools_used,
        "critique_status": critique.get("status", "unknown"),
    }

    db["executions"].append(entry)
    _save_learning_db(db)
    print(f"[P&E] 📝 Logged execution: {exec_id} ({outcome})")


def extract_patterns() -> dict:
    """学習DBから成功/失敗パターンとツール信頼性を抽出する。

    Returns:
        {
            "total": int,
            "success_rate": float,
            "avg_duration": float,
            "tool_reliability": {tool_name: rate, ...},
            "failure_patterns": ["..."],
            "insights": "Gemini による自然言語サマリー"
        }
    """
    db = _load_learning_db()
    execs = db.get("executions", [])

    if len(execs) < 5:
        return {"total": len(execs), "message": "データ不足（5件未満）"}

    total = len(execs)
    success_count = sum(1 for e in execs if e["outcome"] == "success")
    success_rate = success_count / total if total > 0 else 0
    avg_duration = sum(e.get("duration_seconds", 0) for e in execs) / total

    # ツール別信頼性
    tool_calls = {}
    tool_success = {}
    for e in execs:
        is_ok = e["outcome"] in ("success", "partial")
        for t in e.get("tools_used", []):
            if not t:
                continue
            tool_calls[t] = tool_calls.get(t, 0) + 1
            if is_ok:
                tool_success[t] = tool_success.get(t, 0) + 1

    tool_reliability = {}
    for t, count in tool_calls.items():
        tool_reliability[t] = round(tool_success.get(t, 0) / count, 2)

    # 失敗パターン
    failure_patterns = []
    for e in execs:
        if e["outcome"] == "failed" and e.get("failure_reason"):
            failure_patterns.append(
                f"Query: {e['query'][:60]} → {e['failure_reason'][:100]}"
            )

    # Gemini でパターン要約（データが十分な場合のみ）
    insights = ""
    if len(execs) >= 10:
        try:
            summary_data = json.dumps({
                "total": total,
                "success_rate": round(success_rate, 2),
                "avg_duration": round(avg_duration, 1),
                "tool_reliability": tool_reliability,
                "recent_failures": failure_patterns[-5:],
            }, ensure_ascii=False)

            prompt = f"""以下のAIエージェント実行統計を分析し、改善ポイントを3つ箇条書きで述べてください。

{summary_data}

簡潔に（各項目1-2文）:"""

            resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            insights = resp.text.strip()
        except Exception:
            insights = ""

    return {
        "total": total,
        "success_rate": round(success_rate, 2),
        "avg_duration": round(avg_duration, 1),
        "tool_reliability": tool_reliability,
        "failure_patterns": failure_patterns[-10:],
        "insights": insights,
    }


def get_enhanced_prompt(user_query: str) -> str:
    """過去の学習から関連情報を取得し、Planner プロンプトに追記する文字列を返す。"""
    db = _load_learning_db()
    execs = db.get("executions", [])

    if len(execs) < 5:
        return ""

    # 直近の失敗パターンを収集
    recent_failures = []
    for e in reversed(execs[-20:]):
        if e["outcome"] == "failed" and e.get("failure_reason"):
            recent_failures.append(
                f"- クエリ「{e['query'][:50]}」→ {e['failure_reason'][:100]}"
            )
        if len(recent_failures) >= 3:
            break

    # ツール信頼性の低いものを警告
    tool_calls = {}
    tool_success = {}
    for e in execs:
        is_ok = e["outcome"] in ("success", "partial")
        for t in e.get("tools_used", []):
            if not t:
                continue
            tool_calls[t] = tool_calls.get(t, 0) + 1
            if is_ok:
                tool_success[t] = tool_success.get(t, 0) + 1

    low_reliability = []
    for t, count in tool_calls.items():
        if count >= 3:
            rate = tool_success.get(t, 0) / count
            if rate < 0.8:
                low_reliability.append(f"- {t}: 成功率 {rate:.0%}（要注意）")

    # 平均ステップ数
    avg_steps = sum(e.get("plan_steps", 0) for e in execs) / len(execs)

    parts = []
    if recent_failures:
        parts.append("【過去の失敗から学んだ注意事項】")
        parts.extend(recent_failures)
    if low_reliability:
        parts.append("【信頼性の低いツール】")
        parts.extend(low_reliability)
    if avg_steps:
        parts.append(f"【参考】平均的なタスクは {avg_steps:.1f} ステップで完了している")

    return "\n".join(parts) if parts else ""


def generate_performance_dashboard() -> str:
    """Performance Dashboard を Obsidian に出力する。"""
    patterns = extract_patterns()

    if patterns.get("message"):
        return f"データ不足: {patterns['message']}"

    total = patterns["total"]
    rate = patterns["success_rate"]
    avg_dur = patterns["avg_duration"]
    tool_rel = patterns.get("tool_reliability", {})
    failures = patterns.get("failure_patterns", [])
    insights = patterns.get("insights", "")

    # ツール信頼性ランキング
    sorted_tools = sorted(tool_rel.items(), key=lambda x: x[1], reverse=True)
    tool_lines = "\n".join(
        f"| {t} | {r:.0%} |" for t, r in sorted_tools
    )

    # 失敗パターン
    fail_lines = "\n".join(f"- {f}" for f in failures[-5:]) if failures else "なし"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    dashboard = f"""# Agent Performance Dashboard
> Updated: {now}

## 全体統計
| 指標 | 値 |
|---|---|
| 総実行回数 | {total} |
| 成功率 | {rate:.0%} |
| 平均実行時間 | {avg_dur:.1f}秒 |

## ツール信頼性
| ツール | 成功率 |
|---|---|
{tool_lines}

## 最近の失敗パターン
{fail_lines}

## AI分析
{insights if insights else '_データ蓄積中（10件以上で分析開始）_'}
"""

    with open(PERFORMANCE_DASHBOARD_PATH, 'w', encoding='utf-8') as f:
        f.write(dashboard)

    return f"Dashboard updated: {PERFORMANCE_DASHBOARD_PATH}"


# ---------------------------------------------------------
# ReAct Engine (Legacy — 後方互換性のために残す)
# ---------------------------------------------------------

def run_react_loop(user_query, initial_history=None, step_callback=None):
    """
    Think-Act-Observe Loop
    """
    print(f"[ReAct] 🚀 Starting for: {user_query[:50]}...")
    
    config = {
        "tools": [
            {"name": "tool_web_search", "usage": "tool_web_search(query)"},
            {"name": "tool_vault_search", "usage": "tool_vault_search(keywords)"},
            {"name": "tool_create_note", "usage": "tool_create_note(filename, content, summary)"},
            {"name": "tool_read_note", "usage": "tool_read_note(filename)"},
            {"name": "tool_update_note", "usage": "tool_update_note(filename, append_text)"},
            {"name": "tool_get_youtube_transcript", "usage": "tool_get_youtube_transcript(url='URL')"},
            {"name": "tool_merge_weekly_reports", "usage": "tool_merge_weekly_reports()"},
            {"name": "tool_fetch_daily_news", "usage": "tool_fetch_daily_news()"},
            {"name": "tool_fetch_url", "usage": "tool_fetch_url(url)"},
            {"name": "tool_analyze_crypto", "usage": "tool_analyze_crypto(symbol='BTC/USDT')"},
            {"name": "tool_scan_crypto_market", "usage": "tool_scan_crypto_market()"},
            {"name": "tool_audit_predictions", "usage": "tool_audit_predictions()"},
            {"name": "tool_curate_topics", "usage": "tool_curate_topics()"}
        ]
    }

    tools_map = {
        "tool_web_search": tool_web_search,
        "tool_vault_search": tool_vault_search,
        "tool_create_note": tool_create_note,
        "tool_read_note": tool_read_note,
        "tool_update_note": tool_update_note,
        "tool_get_youtube_transcript": tool_get_youtube_transcript,
        "tool_merge_weekly_reports": tool_merge_weekly_reports,
        "tool_fetch_daily_news": tool_fetch_daily_news,
        "tool_fetch_url": tool_fetch_url,
        "tool_analyze_crypto": tool_analyze_crypto,
        "tool_scan_crypto_market": tool_scan_crypto_market,
        "tool_audit_predictions": tool_audit_predictions,
        "tool_curate_topics": _tool_curate_wrapper
    }

    history = initial_history if initial_history else []
    
    for step_i in range(MAX_STEPS):
        print(f"\n[ReAct] Step {step_i + 1}/{MAX_STEPS}")
        
        prompt = f"""
        あなたは自律型AIアシスタントです。
        【ゴール】: {user_query}
        
        現在までの経緯（履歴）:
        {json.dumps(history, ensure_ascii=False, indent=2)}
        
        利用可能なツール:
        1. tool_web_search(query): Web検索 (Tavilyを使用)
        2. tool_vault_search(keywords): Vault内を検索
        3. tool_create_note(filename, content, summary): ファイル作成
        4. tool_read_note(filename): ファイル読込
        5. tool_update_note(filename, append_text): 追記
        6. tool_get_youtube_transcript(url): YouTube動画の文字起こしを取得
        7. tool_fetch_url(url): 指定URLのWebページの内容（テキスト）を取得。ニュース記事やブログなどを読む時に使う。
        8. tool_merge_weekly_reports(): 週次レポートのマージとアーカイブ（ファイルが増えすぎた時に提案/実行）
        9. tool_fetch_daily_news(days): Web検索+RSS巡回でニュースを収集し、Geminiで編集・要約して02_Newsに保存、デイリーノートにリンク追記（daysはオプション、デフォルト1）
           **重要**: このツールはニュースの収集・編集・保存・デイリーノート連携を全て完結する。実行後に tool_create_note で別のニュースファイルを作成してはならない。
        10. tool_analyze_crypto(symbol): Strategy 2.0 総合クオンツ分析。ファンダメンタルズ分析(Why検索) + 落ちるナイフ回避(ADX+DM) + RSIダイバージェンス検知 + SMC流動性回収 + ケリー基準 + DCA計画を統合。レポートを03_Cryptoに保存。symbol例: "BTC/USDT", "ETH/USDT"
        11. tool_scan_crypto_market(): 主要20銘柄を一括スキャンし、RSI・出来高変化・F&Gから注目すべき通貨をランキング。結果を03_Crypto/Market_Scan_{{date}}.mdに保存。「どの通貨が今アツい？」「おすすめは？」等の質問に使う。
        12. tool_audit_predictions(): 過去の予測ログ(trade_log.json)を検証。Binanceから実績価格を取得し、TP到達→WIN / SL到達→LOSE を判定。LOSEの場合はTavilyで敗因ニュースを検索。結果を03_Crypto/Audit_Report/Audit_Report_{{date}}.mdに保存。「予測の答え合わせ」「過去の成績は？」等の質問に使う。
        13. tool_curate_topics(): ユーザーの興味に基づいてナレッジトピックを3つ提案する（The Curator）。「おすすめのトピック」「何か面白い話題ない？」等の質問に使う。
        14. tool_get_calendar(date='today', days=1): Google Calendarから予定を取得。「今日の予定」「明日のスケジュール」等に使う。
        15. tool_add_event(title, start_time, end_time='', description=''): Google Calendarに予定を追加。「会議を追加して」等に使う。
        16. tool_create_task(title, due_date='', priority=1, project='Inbox'): Todoistにタスクを追加。「リマインドして」「タスクを作って」等に使う。priority: 1=最高〜4=最低。
        17. tool_get_tasks(filter='today'): Todoistからタスク一覧を取得。filter: 'today','overdue','week','all'。
        18. tool_sync_to_daily_note(): Google Calendar + Todoistの内容をデイリーノートに同期する。

        【指示】
        - 「次に何をするべきか」を考え、JSONで出力してください。
        - 必要な情報が揃ったら、"action": "finish" としてください。
        
        ## 出力およびツール利用の厳格なルール
        1. **レポート作成の強制**:
           ユーザーから「レポート」「要約」「分析」「まとめ」等の作成を求められた場合、その内容を直接最終回答（Agent_output）に記述してはならない。
           必ず `tool_create_note` を使用し、`30_Main` フォルダ内に新しい Markdown ファイルとして保存すること。
           **例外**: ニュース関連の依頼では `tool_fetch_daily_news` のみを使い、`tool_create_note` は使わない（二重作成防止）。

        2. **Agent_output（最終回答）の役割制限**:
            `Agent_output.md` に書き出す内容は、以下の「実行サマリー」のみに限定すること。
            - 実行したステップの簡潔な説明
            - 作成したレポートのファイル名（例：[[レポート名.md]] を作成しました）
            - エラーが発生した場合の報告

        3. **作業フローの順序**:
            ① 情報収集（YouTube/Vault/Web）
            ② `tool_create_note` による詳細レポートの作成（30_Main）
            ③ 作成成功を確認後、`finish` を呼び出しサマリーのみを報告

        - **重要**: ノートを作成する際、中身がプレースホルダー（例: [調査結果]）であってはいけません。必ず履歴にある「Observation」の内容を使ってください。
        - **重要**: 検索結果が不十分なら、まだノート作成せず、追加検索を行ってください。
        - **最重要 (パーソナライズ)**: Vault内検索（`tool_vault_search`）で関連する過去のメモが見つかった場合は、**必ずその内容を最終成果物に統合してください。**
          - 「あなたの過去のメモ [[ファイル名]] によると〜」のように引用し、ユーザーの文脈に合わせた内容にしてください。
          - 関連ノートのセクションには、見つかったノートへのリンク `[[ファイル名]]` を追記してください。

        出力フォーマット (JSONのみ):
        {{
            "thought": "思考プロセス...",
            "action": "ツール名" or "finish",
            "args": {{ "引数名": "値" ... }}  (actionがfinishの場合は "summary": "完了報告（作成したファイル名を含む）" をargsに入れる)
        }}
        """

        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            text = response.text
            
            # JSON Parse
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]
            
            data = safe_json_loads(text)
            
            thought = data.get("thought", "")
            action = data.get("action", "")
            args = data.get("args", {})
            
            print(f"  💭 Thought: {thought}")
            print(f"  🎬 Action: {action}")

            # Callback for Discord/UI
            if step_callback:
                step_callback({
                    "step": step_i + 1,
                    "thought": thought,
                    "action": action,
                    "args": args,
                    "observation": "Running..."
                })
            
            if action == "finish":
                print(f"[ReAct] ✅ Finished!")
                return args.get("summary", "Done")
            
            if action in tools_map:
                try:
                    observation = tools_map[action](**args)
                except Exception as e:
                    observation = f"Error: {e}"
            else:
                observation = f"Error: Unknown tool '{action}'"
            
            print(f"  👀 Observation: {str(observation)[:200]}...")
            
            history.append({
                "step": step_i + 1,
                "thought": thought,
                "action": action,
                "args": args,
                "observation": observation # Context for next turn
            })

            # Update Callback with Observation
            if step_callback:
                step_callback({
                    "step": step_i + 1,
                    "thought": thought,
                    "action": action,
                    "args": args,
                    "observation": str(observation)[:500] + "..." # Truncate for display
                })
            
        except Exception as e:
            print(f"[ReAct] ❌ Error in loop: {e}")
            traceback.print_exc()
            time.sleep(2)
            
    return "Max steps reached."

# ---------------------------------------------------------
# Error Handling
# ---------------------------------------------------------

def write_error_report(error_message, stacktrace, ai_suggestion=None):
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    report = f"## {timestamp}\n**Error:** {error_message}\n```\n{stacktrace}\n```\n"
    if ai_suggestion: report += f"**AI Suggestion:**\n{ai_suggestion}\n"
    report += "\n---\n"
    try:
        with open(ERROR_FILE, 'a', encoding='utf-8') as f: f.write(report)
    except: pass

def analyze_error_with_gemini(error_message, stacktrace):
    try:
        prompt = f"Analyze this error:\n{error_message}\n{stacktrace}\nSuggest fix."
        resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return resp.text
    except: return "Analysis failed."

# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():
    # Input Reading
    input_path = os.path.join(WORK_DIR, 'Dev_Logs', 'Agent_Input.md')
    user_query = ""
    try:
        if len(sys.argv) > 1:
            user_query = sys.argv[1]
        elif os.path.exists(input_path):
            with open(input_path, 'r', encoding='utf-8') as f:
                user_query = f.read().strip()
    except Exception as e:
        print(f"Error reading input: {e}")
        return

    if not user_query:
        print("No input provided.")
        return

    user_query = resolve_obsidian_links(user_query)
    today_str = datetime.datetime.now().strftime('%Y年%m月%d日')
    print(f"[System] 📅 {today_str} | Model: {MODEL_NAME}")
    
    # --- AUTO-CONTEXT INJECTION ---
    # Userのクエリからキーワードを抽出してVaultを検索し、文脈に強制注入する
    print(f"[Auto-Context] 🔍 Pre-searching Vault for: {user_query[:30]}...")
    
    # Use LLM to generate relevant keywords (including synonyms)
    try:
        kw_prompt = f"Extract 3-5 distinct search keywords from the following user query for a knowledge base search. Include synonyms or related terms. Return ONLY the keywords separated by spaces.\n\nQuery: {user_query}"
        kw_response = client.models.generate_content(model=MODEL_NAME, contents=kw_prompt)
        keywords = kw_response.text.strip().replace('\n', ' ')
        print(f"[Auto-Context] 🤖 Expanded Keywords: {keywords}")
    except:
        # Fallback to naive
        clean_query = re.sub(r'[\\/:*?"<>|]', ' ', user_query)
        keywords = " ".join([w for w in clean_query.split() if len(w) > 1][:5])

    vault_context = tool_vault_search(keywords)
    initial_history_injection = []
    
    if "Vault内に見つかりませんでした" not in vault_context:
        print(f"[Auto-Context] ✅ Found Vault info. Injecting into memory.")
        initial_history_injection.append({
            "step": 0,
            "thought": "System Auto-Action: Check Vault for existing knowledge.",
            "action": "tool_vault_search",
            "args": {"keywords": keywords},
            "observation": vault_context
        })
    else:
         print(f"[Auto-Context] ⚠️ No relevant Vault info found (or error).")
    
    try:
        # Run ReAct Loop
        final_summary = run_react_loop(user_query, initial_history=initial_history_injection)
        
        # Save Output
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write(f"\n## Execution Summary\n{final_summary}\n")
        print(f"[System] MISSION COMPLETE. Output: {OUTPUT_FILE}")
        
    except Exception as e:
        print(f"[System] ❌ Critical Error: {e}")
        traceback.print_exc()
        suggestion = analyze_error_with_gemini(str(e), traceback.format_exc())
        write_error_report(str(e), traceback.format_exc(), suggestion)

if __name__ == "__main__":
    main()
