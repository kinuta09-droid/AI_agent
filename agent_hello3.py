# -*- coding: utf-8 -*-
import os
import sys
import json
import re
from dotenv import load_dotenv  # type: ignore
from google import genai
from google.genai import types # type: ignore

# Windowsコンソールでの文字化け防止
sys.stdout.reconfigure(encoding='utf-8')

# 環境変数の読み込み
load_dotenv('C:\\AI_agent\\.env')
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("Error: GOOGLE_API_KEY not found.")
    sys.exit(1)

client = genai.Client(api_key=api_key)

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
# リストで確認された「確実に存在する」モデル名
MODEL_NAME = 'gemini-2.5-flash-lite' 

MAX_RETRIES = 3
HISTORY_FILE = r'C:\obsidian\Obsidian\Dev_Logs\chat_history.json'
WORK_DIR = r'C:\obsidian\Obsidian' 
VAULT_ROOT = r'C:\obsidian\Obsidian' 
# ---------------------------------------------------------

def find_file_in_vault(filename):
    if not filename.endswith('.md'): filename += '.md'
    direct_path = os.path.join(WORK_DIR, filename)
    if os.path.exists(direct_path): return direct_path
    for root, dirs, files in os.walk(VAULT_ROOT):
        if filename in files: return os.path.join(root, filename)
    return None

def resolve_obsidian_links(text):
    pattern = r'\[\[(.*?)\]\]'
    matches = re.findall(pattern, text)
    if not matches: return text

    print(f"[System] Expanding {len(matches)} links...")
    expanded_text = text
    for link_name in matches:
        real_filename = link_name.split('|')[0]
        file_path = find_file_in_vault(real_filename)
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                injection = f"\n\n--- Start of Referenced File: {real_filename} ---\n{content}\n--- End of Referenced File ---\n\n"
                expanded_text = expanded_text.replace(f'[[{link_name}]]', injection)
                print(f"  --> Loaded: {real_filename}")
            except Exception as e:
                print(f"  --> Error reading {real_filename}: {e}")
        else:
            print(f"  --> File not found: {real_filename}")
    return expanded_text

def read_input(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return resolve_obsidian_links(f.read().strip())
    except:
        return ""

def load_history():
    if not os.path.exists(HISTORY_FILE): return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def append_and_save_history(current_history, user_text, response_content):
    current_history.append({"role": "user", "parts": [{"text": user_text}]})
    model_parts = []
    if response_content and response_content.parts:
        for part in response_content.parts:
            if part.text: model_parts.append({"text": part.text})
    if model_parts:
        current_history.append({"role": "model", "parts": model_parts})
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_history, f, ensure_ascii=False, indent=2)
    except: pass

def save_generated_files(text, footer_content=""):
    """マジックハンド機能（物理切断＆出典スタンプ版）"""
    if not text: return
    
    cursor = 0
    start_tag = "<<<FILE:"
    end_tag = "<<<END_FILE>>>"
    
    print(f"\n[System] Scanning text for files...")
    file_count = 0
    
    while True:
        start_index = text.find(start_tag, cursor)
        if start_index == -1: break
            
        filename_end_index = text.find(">>>", start_index)
        if filename_end_index == -1:
            cursor = start_index + 1
            continue
            
        content_end_index = text.find(end_tag, filename_end_index)
        if content_end_index == -1:
            cursor = filename_end_index + 1
            continue
            
        raw_filename = text[start_index + 8 : filename_end_index].strip()
        content = text[filename_end_index + 3 : content_end_index].strip()
        
        safe_filename = raw_filename.replace(":", "_").replace("?", "").replace("/", "_").replace("\\", "_").replace("*", "")
        if not safe_filename.endswith('.md'): safe_filename += '.md'
            
        full_path = os.path.join(WORK_DIR, safe_filename)
        
        # 出典リストを追記
        final_content = content
        if footer_content:
            final_content += "\n\n" + footer_content
        
        try:
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(final_content)
            print(f"  --> [SUCCESS] Saved: {full_path}")
            file_count += 1
        except Exception as e:
            print(f"  --> [ERROR] Could not save '{safe_filename}': {e}")
            
        cursor = content_end_index + len(end_tag)

    if file_count == 0:
        print("[System Notice] No files extracted. Check tags in input.")

def main():
    if len(sys.argv) > 1:
        user_query = resolve_obsidian_links(sys.argv[1])
    else:
        input_path = os.path.join(WORK_DIR, 'Dev_Logs', 'Agent_Input.md')
        user_query = read_input(input_path)

    if not user_query:
        print("No input provided.")
        return

    # システムプロンプト（検索戦略とタグ出力を強制）
    system_instruction = """
    あなたはファイル生成エンジン「ARMS」です。
    
    【重要ルール】
    1. **Google検索の使用**: ユーザーのファイルを読み込んだ後、そこから「検索キーワード」を抽出し、必ずGoogle検索を行って外部の事実を確認してください。
    2. **ファイル出力形式**: ファイルを作成する際は、必ず `<<<FILE: ファイル名>>>` と `<<<END_FILE>>>` のタグで囲んでください。
    """

    # 【修正点1】検索を強制するための設定（DynamicRetrievalConfig）
    # dynamic_threshold=0.6 程度に設定することで、AIに「検索が必要か」を判断させつつ、標準ツールとして認識させる
    tools = [
        types.Tool(
            google_search=types.GoogleSearch()
        )
    ]
    
    config = types.GenerateContentConfig(
        tools=tools,
        system_instruction=system_instruction
    )

    try:
        history = load_history()
        chat = client.chats.create(model=MODEL_NAME, config=config, history=history)
        
        print(f"Agent running (Model: {MODEL_NAME})...")
        print("Thinking & Searching...")
        
        response = chat.send_message(user_query)

        # ---------------------------------------------------------
        # 【修正点2】検索実績チェック & 却下ロジック
        # ---------------------------------------------------------
        sources_footer = ""
        display_sources = ""
        is_grounded = False # 検索成功フラグ
        
        print("\n--- [Debug Diagnosis] ---")
        if response.candidates and response.candidates[0].grounding_metadata:
            metadata = response.candidates[0].grounding_metadata
            
            # 検索クエリの確認
            if metadata.search_entry_point and metadata.search_entry_point.rendered_content:
                 print(f"🔍 System Search Query: {metadata.search_entry_point.rendered_content}")
            
            # 【重要】検索結果（Chunks）が1件以上あるか確認
            if metadata.grounding_chunks:
                is_grounded = True # 成功！
                print(f"✅ Google Search Success! Found {len(metadata.grounding_chunks)} chunks.")
                
                sources_list = []
                seen = set()
                for c in metadata.grounding_chunks:
                    if c.web:
                        url = c.web.uri or ""
                        title = c.web.title or "No Title"
                        if url and url not in seen:
                            sources_list.append(f"- [{title}]({url})")
                            seen.add(url)
                
                if sources_list:
                    sources_footer = "\n---\n### 📚 参考文献 (システム自動挿入)\n" + "\n".join(sources_list)
                    display_sources = "\n### 🔍 Sources Used\n" + "\n".join(sources_list)
            else:
                # 検索結果ゼロ
                print("⚠️ Search executed but NO results used (Chunks=0).")
                if hasattr(metadata, 'web_search_queries'):
                     print(f"   (Tried searching for: {metadata.web_search_queries})")
        else:
            print("❌ No Search Metadata found. (AI ignored the Search Tool entirely)")
        print("-------------------------\n")

        # ---------------------------------------------------------
        # ファイル保存判断（ここが防波堤）
        # ---------------------------------------------------------
        final_output = ""
        if response.text:
            final_output += f"\n## Answer\n{response.text}\n"
            
            # 【重要】検索に成功していない場合は、ファイル保存を強制スキップする
            if is_grounded:
                save_generated_files(response.text, sources_footer)
            else:
                print("\n⛔ [REJECTED] The model did NOT perform a valid search.")
                print("   Generated content was discarded because 'grounding_chunks' is empty.")
                print("   (ハルシネーション防止のため、ファイルの保存を中止しました)")
        
        if display_sources:
            final_output += display_sources

        print(final_output)

        # 履歴には残すが、ユーザーには「失敗した」と伝わる
        if response.candidates:
            append_and_save_history(history, user_query, response.candidates[0].content)
            
    except Exception as e:
        print(f"\nError occurred: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()