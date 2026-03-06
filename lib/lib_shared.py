# -*- coding: utf-8 -*-
"""
Antigravity Shared Library
agent_hello.py から抽出された共有リソース。
API クライアント、定数、ユーティリティ関数を提供する。
"""

import os
import sys
import json
import re
import datetime
import time
import subprocess
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

try:
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1beta")
    )
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

MODEL_NAME = 'gemini-3.1-flash-lite-preview'
# コンテンツ生成・最終サマリー等、出力品質が重要なタスク向け
MODEL_NAME_HEAVY = 'gemini-2.5-flash'
MAX_RETRIES = 5
MAX_STEPS = 10  # ReAct loop limit

HISTORY_FILE = r'D:\マイドライブ\Obsidian\Dev_Logs\chat_history.json'
WORK_DIR = r'D:\マイドライブ\Obsidian'
VAULT_ROOT = r'D:\マイドライブ\Obsidian'

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

# Gemini 共通文体ルール（ニュース／キュレーション等で共有）
EDITORIAL_STYLE_RULES = """【文体ルール】
- 冒頭に「# 」見出しは付けないこと（テンプレートが自動付与するため）。
- 末尾にハッシュタグ（#curated 等）は付けないこと。
- あなたは学者ではなく編集者。堅い漢語や「〜であると考えられる」のような論文調は禁止。
- 「〜です」「〜でしょう」「〜ですが」のような、自然で現代的な敬体を使う。
- 冗長な前置き、「いかがでしたでしょうか」などの無駄な結びは一切排除。
- 1段落は最大3文。それ以上になるなら箇条書きに分割する。
- 専門用語は初出時にだけ（）で短く補足。2回目以降はそのまま使う。
- 絵文字は見出しのみ。本文中には使わない。
- 読者が「なるほど」と唸るような洞察を1つは入れること。"""

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

def _run_gws_command(args: list[str], timeout: int = 30) -> dict | None:
    """gws CLI コマンドを実行し、JSON結果を返す。失敗時は None。"""
    import shutil
    gws_exe = shutil.which("gws")
    if gws_exe is None:
        return None
    try:
        result = subprocess.run(
            [gws_exe] + args,
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None

def fetch_fear_greed_index() -> tuple[int, str]:
    """Fear & Greed Index を取得。失敗時はデフォルト(50, 'Neutral')を返す。"""
    try:
        fng_resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        fng_data = fng_resp.json()['data'][0]
        return int(fng_data['value']), fng_data['value_classification']
    except Exception as e:
        print(f"  ⚠️ F&G fetch failed: {e}")
        return 50, "Neutral"


def load_json(path: str, default=None):
    """JSONファイルを読み込む。存在しない/パース失敗時は default を返す。"""
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default


def save_json(path: str, data):
    """JSONファイルを保存する。親ディレクトリがなければ作成する。"""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
