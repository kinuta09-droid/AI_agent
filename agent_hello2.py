import os
import sys
import time
import json
import re
from dotenv import load_dotenv  # type: ignore
from google import genai

# 文字化け対策
sys.stdout.reconfigure(encoding='utf-8')

# 環境変数読み込み
load_dotenv(r'C:\AI_agent\.env')
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("Error: GOOGLE_API_KEY not found.")
    sys.exit(1)

client = genai.Client(api_key=api_key)

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
# ツール利用に定評のある安定版モデル
MODEL_NAME = 'gemini-flash-latest'
MAX_RETRIES = 3
RETRY_DELAY = 10
HISTORY_FILE = r'C:\obsidian\Obsidian\Dev_Logs\chat_history.json'
WORK_DIR = r'C:\obsidian\Obsidian\Dev_Logs'
# ---------------------------------------------------------

def read_input(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Failed to load history ({e}). Starting fresh.")
        return []

def append_and_save_history(current_history, user_text, response_content):
    # 簡易履歴保存
    current_history.append({"role": "user", "parts": [{"text": user_text}]})
    
    model_parts = []
    if response_content and response_content.parts:
        for part in response_content.parts:
            part_dict = {}
            if part.text: part_dict["text"] = part.text
            if part_dict: model_parts.append(part_dict)

    if model_parts:
        current_history.append({"role": "model", "parts": model_parts})

    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving history: {e}")

def save_generated_files(text):
    """マジックハンド機能：特殊タグを検出してファイルを保存"""
    if not text: return
    
    pattern = r'<<<FILE:(.*?)>>>\n(.*?)\n<<<END_FILE>>>'
    matches = re.findall(pattern, text, re.DOTALL)
    
    if matches:
        print("\n[System] Detecting file output...")
        for filename, content in matches:
            filename = filename.strip()
            save_path = os.path.join(WORK_DIR, filename)
            try:
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(content.strip())
                print(f"  --> Saved: {save_path}")
            except Exception as e:
                print(f"  --> Error saving {filename}: {e}")
    else:
        # ファイル生成がない場合もログを出す
        print("[System] No file generation tags found in this response.")

def main():
    # 引数があればそれを採用、なければファイルから読み込む
    if len(sys.argv) > 1:
        user_query = sys.argv[1]
    else:
        input_path = os.path.join(WORK_DIR, 'Agent_Input.md')
        user_query = read_input(input_path)

    if not user_query:
        print("No input provided.")
        return

    # システムプロンプト
    system_instruction = """
    あなたは優秀なAIエージェントです。
    
    【ルール】
    1. **Google Search** (`Google Search`) を積極的に使用して最新情報を取得してください。
    2. ユーザーのPCにファイルを保存したい場合は、必ず以下の形式で出力してください。
    
    <<<FILE: ファイル名>>>
    ここに中身
    <<<END_FILE>>>
    """

    # ツール設定（Google検索を有効化）
    config = {
        'tools': [{'google_search': {}}],
        'system_instruction': system_instruction
    }

    try:
        history = load_history()
        chat = client.chats.create(model=MODEL_NAME, config=config, history=history)
        
        print(f"Agent running (Model: {MODEL_NAME})...")
        print("Thinking & Searching...")
        
        # 実行
        response = chat.send_message(user_query)

        # 実行
        response = chat.send_message(user_query)

        # ---------------------------------------------------------
        # 1. 内部モデル情報の確認（デバッグ用）
        # ---------------------------------------------------------
        try:
            actual_model = getattr(response, 'model_version', 'Unknown')
            print(f"\n[System Info] Actual Model Used: {actual_model}")
        except Exception:
            pass

        # ---------------------------------------------------------
        # 2. 結果の構築（テキスト + 検索ソース）
        # ---------------------------------------------------------
        final_output = ""

        # A) AIの回答本文
        if response.text:
            final_output += f"## Answer\n{response.text}\n\n"
        
        # B) 検索ソース（Grounding Metadata）の抽出
        # ここが「本物のURL」を表示する新機能です
        try:
            if response.candidates and response.candidates[0].grounding_metadata:
                metadata = response.candidates[0].grounding_metadata
                
                if metadata.grounding_chunks:
                    sources_section = "### 🔍 Sources (Real Evidence)\n"
                    unique_urls = set()
                    has_source = False
                    
                    for chunk in metadata.grounding_chunks:
                        if chunk.web:
                            title = chunk.web.title or "No Title"
                            url = chunk.web.uri or ""
                            if url and url not in unique_urls:
                                sources_section += f"- [{title}]({url})\n"
                                unique_urls.add(url)
                                has_source = True
                    
                    if has_source:
                        final_output += sources_section
        except Exception as e:
            print(f"[System Log] Metadata extraction warning: {e}")

        # ---------------------------------------------------------
        # 3. 出力と保存
        # ---------------------------------------------------------
        if final_output:
            print(final_output)
            # ファイル生成機能（マジックハンド）は本文に対して行う
            save_generated_files(response.text)
        else:
            # 万が一空だった場合
            print("\n[System Notice] Response was empty.")
            if response.candidates:
                print(response.candidates[0].content)

        # 履歴保存
        if response.candidates:
            append_and_save_history(history, user_query, response.candidates[0].content)
            
    except Exception as e:
        print(f"\nError occurred: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()