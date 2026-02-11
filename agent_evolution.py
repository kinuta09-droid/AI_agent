# -*- coding: utf-8 -*-
import os
import sys
import shutil
import datetime
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Windowsコンソールでの文字化け防止
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv('C:\\AI_agent\\.env')
api_key = os.getenv("GOOGLE_API_KEY")
client = genai.Client(api_key=api_key)

# ★コード書き換えには賢いモデルを使う
MODEL_NAME = 'gemini-2.5-flash' 

# パス設定
TARGET_SCRIPT = r'C:\AI_agent\agent_hello.py'
EVOLUTION_LOG = r'C:\obsidian\Obsidian\Dev_Logs\System_Evolution.md'

def read_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f: return f.read()
    except FileNotFoundError:
        return ""

def write_file(path, content):
    with open(path, 'w', encoding='utf-8') as f: f.write(content)

def backup_script():
    """念のためバックアップを取る"""
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f"{TARGET_SCRIPT}.{timestamp}.bak"
    try:
        shutil.copy2(TARGET_SCRIPT, backup_path)
        print(f"[Evo] 🛡️ Backup created: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"[Evo] ⚠️ Backup failed: {e}")
        return None

def propose_improvements():
    """現在のコードを読んで改善案を出す (CTOモード)"""
    print("[Evo] 🧠 Analyzing current code for improvements...")
    current_code = read_file(TARGET_SCRIPT)
    
    prompt = f"""
    あなたは優秀なPythonアーキテクト（CTO）です。
    以下の「現在のソースコード」を分析し、次に実装すべき機能や改善点を3つ提案してください。

    【現在のコード】
    {current_code[:25000]} (省略...)

    【提案の方向性】
    - エラー処理の強化
    - **新機能の追加（最優先）**: ユーザーが「あっと驚く」ような新機能（画像生成、音声対話、カレンダー連携、外部API連携など）
    - 作業の自動化（File Watcherの強化、定期実行など）
    - コードの可読性向上（優先度低）
    - 「作業の自動化」の推進

    【出力フォーマット】
    # Next Development Plan
    ## Proposal 1: [タイトル]
    - **内容**: ...
    - **メリット**: ...
    
    ## Proposal 2: ...
    ## Proposal 3: ...
    
    （最後に、ユーザーに「どれを実装しますか？ 'IMPLEMENT: 1' のように書いてください」と促すメッセージを入れる）
    """
    
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    
    # Obsidianに提案を書き込む
    write_file(EVOLUTION_LOG, response.text)
    print(f"[Evo] 💡 Proposal written to: {EVOLUTION_LOG}")

def implement_feature(request):
    """指定された機能を実装する (Engineerモード)"""
    print(f"[Evo] 🔨 Implementing feature: {request}")
    
    # 1. バックアップ
    backup_script()
    
    # 2. コード生成
    current_code = read_file(TARGET_SCRIPT)
    
    prompt = f"""
    あなたは熟練のPythonエンジニアです。
    以下の「現在のコード」に対して、ユーザーの要望する「機能追加」を行ってください。
    
    【ユーザーの要望】
    {request}
    
    【現在のコード】
    {current_code}
    
    【重要ルール】
    1. **コード全体を出力**してください（差分ではなく、ファイルの中身すべて）。
    2. 既存の機能を壊さないように注意してください。
    3. Pythonの構文エラーがないか確認してください。
    4. コードブロック（```python ... ```）の中身だけを抽出して保存します。
    """
    
    print("[Evo] ⏳ Generating new code (this may take a while)...")
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    
    # コードブロックの抽出
    new_code = response.text
    if "```python" in new_code:
        new_code = new_code.split("```python")[1].split("```")[0].strip()
    elif "```" in new_code:
        new_code = new_code.split("```")[1].split("```")[0].strip()
        
    # 3. 上書き保存
    if len(new_code) > 100: # 明らかに短すぎる場合はエラーとみなす
        write_file(TARGET_SCRIPT, new_code)
        print("[Evo] ✅ Update Complete! `agent_hello.py` has been evolved.")
        
        # ログに完了報告
        log_content = f"# Evolution Log\n\n## Status: Completed\nImplemented: {request}\n\nDate: {datetime.datetime.now()}"
        write_file(EVOLUTION_LOG, log_content)
    else:
        print("[Evo] ❌ Error: Generated code seems too short. Operation aborted.")

import re

# ... (imports) ...

def extract_proposal_content(file_content, proposal_number):
    """
    指定された番号のProposalの内容を抽出する
    """
    # Pattern to find "## Proposal X"
    # Allows flexibility like "Proposal 1", "Proposal1", "Proposal 1:"
    pattern = r'##\s*Proposal\s*:?\s*' + str(proposal_number) + r'(.*?)(?=##\s*Proposal|\Z)'
    match = re.search(pattern, file_content, re.DOTALL | re.IGNORECASE)
    
    if match:
        return match.group(1).strip()
    return None

def main():
    print(f"[Evo] 👁️ Watching: {EVOLUTION_LOG}")
    
    # 初回起動時にファイルがなければ作成して提案モードへ
    if not os.path.exists(EVOLUTION_LOG):
        write_file(EVOLUTION_LOG, "REQUEST: PROPOSE_NEXT")
    
    last_mtime = 0
    if os.path.exists(EVOLUTION_LOG):
        last_mtime = os.path.getmtime(EVOLUTION_LOG)
    
    while True:
        time.sleep(2)
        try:
            if not os.path.exists(EVOLUTION_LOG):
                continue

            current_mtime = os.path.getmtime(EVOLUTION_LOG)
            if current_mtime != last_mtime:
                last_mtime = current_mtime
                content = read_file(EVOLUTION_LOG).strip()
                
                # Case 1: Proposal Generation
                if "REQUEST: PROPOSE_NEXT" in content:
                    propose_improvements()
                    
                # Case 2: Explicit Implementation Request
                elif "REQUEST: IMPLEMENT" in content:
                    try:
                        instruction = content.split("REQUEST: IMPLEMENT")[1].strip()
                        if instruction:
                           implement_feature(instruction)
                    except IndexError:
                        print("[Evo] ⚠️ Invalid IMPLEMENT format")

                # Case 3: Proposal Selection Trigger (Refined)
                else:
                    # Look for "Proposal X" or "IMPLEMENT: X" at the end of file (last 500 chars)
                    tail = content[-500:] 
                    
                    # Regex: Must be on a line by itself (ignoring whitespace)
                    # This prevents matching "Please write 'IMPLEMENT: 1'" inside a sentence.
                    trigger_match = re.search(r'^[\s]*(?:Proposal|IMPLEMENT)[:\s]*(\d+)[\s]*$', tail, re.IGNORECASE | re.MULTILINE)
                    
                    if trigger_match:
                        proposal_num = trigger_match.group(1)
                        print(f"[Evo] 🎯 Detected Trigger for Proposal {proposal_num}")
                        
                        proposal_content = extract_proposal_content(content, proposal_num)
                        
                        if proposal_content:
                            print(f"[Evo] 📜 Extracted Proposal Content: {proposal_content[:50]}...")
                            # Add context to instruction
                            full_instruction = f"Implement the following proposal:\n{proposal_content}"
                            implement_feature(full_instruction)
                        else:
                            print(f"[Evo] ⚠️ Proposal {proposal_num} not found in content.")

        except Exception as e:
            print(f"[Evo] Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
