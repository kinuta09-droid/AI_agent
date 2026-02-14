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
MAX_STEPS = 10  # ReAct loop limit

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
