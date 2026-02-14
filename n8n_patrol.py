# -*- coding: utf-8 -*-
import requests
import json
import os
from dotenv import load_dotenv

# .envファイルの内容を読み込む
load_dotenv()

# 設定情報
N8N_API_KEY = os.getenv("N8N_API_KEY") or "あなたのAPIキーをここに貼り付け"
N8N_BASE_URL = "http://localhost:5678/api/v1" # 自己ホスト型のデフォルト
SAVE_DIR = "n8n_backups"

def fetch_all_workflows():
    if not N8N_API_KEY or "あなたのAPIキー" in N8N_API_KEY:
        print("Error: N8N_API_KEY is not set. Please set it in .env or hardcode it in the script.")
        return

    headers = {"X-N8N-API-KEY": N8N_API_KEY}
    
    # ワークフロー一覧を取得
    try:
        response = requests.get(f"{N8N_BASE_URL}/workflows", headers=headers)
    except requests.exceptions.ConnectionError:
        print(f"Error: Could not connect to n8n at {N8N_BASE_URL}. Is it running?")
        return

    if response.status_code != 200:
        print(f"Error: {response.text}")
        return

    workflows = response.json().get('data', [])
    print(f"Found {len(workflows)} workflows.")
    
    if not os.path.exists(SAVE_DIR):
        os.makedirs(SAVE_DIR)

    for wf in workflows:
        wf_id = wf['id']
        wf_name = wf['name']
        
        # 各ワークフローの詳細（ノード構成含む）を取得
        detail_res = requests.get(f"{N8N_BASE_URL}/workflows/{wf_id}", headers=headers)
        if detail_res.status_code == 200:
            # ファイル名に使えない文字を置換
            safe_name = "".join([c if c.isalnum() or c in (' ', '_', '-') else '_' for c in wf_name])
            safe_name = safe_name.replace(' ', '_')
            
            filename = f"{SAVE_DIR}/{safe_name}_{wf_id}.json"
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(detail_res.json(), f, indent=2, ensure_ascii=False)
            print(f"Saved: {filename}")
        else:
             print(f"Failed to fetch details for {wf_name} ({wf_id})")

if __name__ == "__main__":
    fetch_all_workflows()
