# -*- coding: utf-8 -*-
"""
Antigravity Shared Library
agent_hello.py から抽出された共有リソース。
API クライアント、定数、ユーティリティ関数を提供する。
"""

import os
import sys
import json
import uuid
import re
import datetime
import time
import subprocess
import traceback
from functools import wraps
from dotenv import load_dotenv
from google import genai
from google.genai import types
from tavily import TavilyClient
import requests

# cost_guard はモジュールレベルで lib_shared を import しないため循環しない
# (save_json/load_json は cost_guard 側が関数内で遅延 import する)
from lib import cost_guard
from lib.cost_guard import GeminiBudgetExceeded
from lib.paths import PROJECT_ROOT, ENV_PATH, VAULT_ROOT

# Windowsコンソールでの文字化け防止
sys.stdout.reconfigure(encoding='utf-8')

# 環境変数の読み込み
load_dotenv(ENV_PATH)
api_key = os.getenv("GOOGLE_API_KEY")
tavily_api_key = os.getenv("TAVILY_API_KEY")

if not api_key:
    # 探した場所を必ず出す。ルート直書きをやめた結果、チェックアウト毎に
    # .env が要るようになったため、「どこを見て無かったのか」が分からないと
    # 新しい worktree で無言に近い終了に見える
    print(f"Error: GOOGLE_API_KEY not found. (.env を探した場所: {ENV_PATH})")
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

# ルーティング判定・多数のツール向け（軽量・高スループット）
# .env の GEMINI_MODEL_LIGHT で上書き可（未設定時は現行値＝挙動不変）。
# 世代交代・ロールバックはコード編集不要で .env 1行 + 再起動のみ（施策③）。
MODEL_NAME = os.getenv("GEMINI_MODEL_LIGHT", 'gemini-3.1-flash-lite-preview')
# コンテンツ生成・最終サマリー等、出力品質が重要なタスク向け
# .env の GEMINI_MODEL_HEAVY で上書き可（未設定時は現行値＝挙動不変）。
MODEL_NAME_HEAVY = os.getenv("GEMINI_MODEL_HEAVY", 'gemini-2.5-flash')
MAX_RETRIES = 5

# Vault は外部ドライブなので __file__ 相対にできない。実体と既定値は lib/paths.py
# (.env の OBSIDIAN_VAULT_ROOT で差し替え可・未設定なら従来値) に集約し、
# ここは後方互換のための再輸出に留める。WORK_DIR は VAULT_ROOT の歴史的別名。
WORK_DIR = VAULT_ROOT
HISTORY_FILE = os.path.join(VAULT_ROOT, 'Dev_Logs', 'chat_history.json')

REPORT_DIR = os.path.join(WORK_DIR, '40_Report')
OUTPUT_FILE = os.path.join(WORK_DIR, 'Dev_Logs', 'Agent_output.md')
ERROR_FILE = os.path.join(WORK_DIR, 'Dev_Logs', 'ErrorReport.md')

NEWS_DIR_NAME = "02_News"
CRYPTO_DIR_NAME = "03_Crypto"
TRADE_LOG_PATH = os.path.join(VAULT_ROOT, "Dev_Logs", "trade_log.json")
LEARNING_DB_PATH = os.path.join(PROJECT_ROOT, "learning_db.json")
LEARNING_DB_ARCHIVE_PATH = os.path.join(PROJECT_ROOT, "learning_db_archive.jsonl")
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
            last_exc = None
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except GeminiBudgetExceeded:
                    # 日次予算超過は待っても回復しない — バックオフ再試行せず即時再送出
                    raise
                except Exception as e:
                    last_exc = e
                    print(f"Retry {retries+1}/{max_retries} failed. Waiting {delay}s... Error: {e}")
                    traceback.print_exc()
                    retries += 1
                    time.sleep(delay)
                    delay *= 2
            print(f"Function {func.__name__} failed after {max_retries} retries.")
            # 裸の raise は except の外では RuntimeError("No active exception") に
            # 化けて元のエラーを隠すため、保持した最終例外を投げ直す
            raise last_exc
        return wrapper
    return decorator_retry

def _run_gws_command(args: list[str], timeout: int = 30) -> dict | None:
    """gws CLI コマンドを実行し、JSON結果を返す。

    Returns:
        成功時: パース済みJSON。
        API/認証エラー時: {"error": {...}} を含むdict (gws は認証切れ等でも
            エラーJSONをstdoutに出す。これを捨てると呼び出し元で「データ0件」と
            区別できず、認証切れが「新着なし/予定なし」に化ける)。
        実行不能時 (未インストール/タイムアウト/非JSON): None。
    呼び出し側は None と "error" キーの両方をチェックすること。
    """
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
        parsed = json.loads(result.stdout)
        if result.returncode != 0:
            # エラーJSONなら情報を保持して返す。それ以外の非ゼロ終了は従来通り None
            return parsed if isinstance(parsed, dict) and "error" in parsed else None
        return parsed
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
    """JSONファイルを原子的に保存する。親ディレクトリがなければ作成する。

    同一ディレクトリの一時ファイルへ書き込んでから os.replace で差し替える。
    書き込み途中でクラッシュしても元ファイルは無傷のまま残り、半端に truncate
    された JSON が残らない（load_json が破損を default 握り潰しで隠すため重要）。
    """
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    # pid だけでは同一プロセス内の並行スレッド (asyncio.to_thread 経由の
    # 同時 /agent 実行) が同じ tmp を掴んで潰し合うため uuid で一意化する。
    tmp_path = f"{path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)  # 同一ボリューム上で原子的
    except Exception:
        # 一時ファイルが残っていたら掃除して元ファイルを守る
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


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
# Tier2-b: API Cost Guard — 共有clientの generate_content を日次上限付きに差し替え
# ---------------------------------------------------------
# `from lib.lib_shared import client` は値importだが、ここではオブジェクト上の
# 束縛メソッドを差し替えるため、import順に関係なく全利用箇所に伝播する。
# 呼び出し「前」にカウントする: 失敗したAPI呼び出しもリクエスト試行として
# 消費されるため、過小計上より過大計上を選ぶ (セーフティネット用途)。
#
# LIGHT/HEAVY別カウンタ (2026-08-17): モデル毎にRPD上限が別枠のGoogle側の実態に
# 合わせて、cost_guard.GEMINI_LIGHT_COUNTER / GEMINI_HEAVY_COUNTER を使い分ける。
# HEAVYが日次上限に達した場合は例外を伝播させず、LIGHTへ切り替えて続行する
# (HEAVYは品質重視で選んでいるが、上限到達でタスク全体を失敗させるより、
# 精度の落ちる回答で完走させる方が有用という判断。LIGHT側も上限なら
# そこで初めて GeminiBudgetExceeded を伝播する)。
def _guarded_generate_content(*args, **kwargs):
    requested_model = kwargs.get("model") if "model" in kwargs else (args[0] if args else None)

    if requested_model == MODEL_NAME_HEAVY:
        try:
            cost_guard.GEMINI_HEAVY_COUNTER.check_and_increment()
        except GeminiBudgetExceeded:
            print(f"[CostGuard] HEAVY({MODEL_NAME_HEAVY})が日次上限に到達。"
                  f"LIGHT({MODEL_NAME})へフォールバックします。")
            cost_guard.GEMINI_LIGHT_COUNTER.check_and_increment()
            if "model" in kwargs:
                kwargs["model"] = MODEL_NAME
            elif args:
                args = (MODEL_NAME,) + args[1:]
    else:
        cost_guard.GEMINI_LIGHT_COUNTER.check_and_increment()

    return _original_generate_content(*args, **kwargs)


_original_generate_content = client.models.generate_content
client.models.generate_content = _guarded_generate_content
