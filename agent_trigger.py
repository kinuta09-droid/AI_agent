import time
import os
import requests
import sys

# Configuration
WATCH_FILE = r"C:\obsidian\Obsidian\Dev_Logs\Agent_Input.md"
WEBHOOK_URL = "http://localhost:5678/webhook/mobile-agent"

def get_file_mtime(filepath):
    try:
        return os.path.getmtime(filepath)
    except OSError:
        return 0

def read_file_content(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        print(f"[Error] Failed to read {filepath}: {e}")
        return None

def trigger_n8n_webhook(text):
    if not text:
        print("[Info] File is empty, ignoring.")
        return False
    
    payload = {"text": text}
    try:
        response = requests.post(WEBHOOK_URL, json=payload)
        if response.status_code == 200:
            print(f"[Success] 🚀 Triggered n8n Webhook! (Text length: {len(text)})")
            return True
        else:
            print(f"[Error] n8n returned status code: {response.status_code}")
            print(response.text)
            return False
    except requests.exceptions.RequestException as e:
        print(f"[Error] Could not connect to n8n: {e}")
        return False

def main():
    print(f"[System] 👁️ Watching input file: {WATCH_FILE}")
    print(f"[System] 🔗 Target Webhook: {WEBHOOK_URL}")
    print("[System] Ctrl+C to stop.")

    if not os.path.exists(WATCH_FILE):
        print(f"[Warning] File not found: {WATCH_FILE}. Waiting for creation...")
    
    last_mtime = get_file_mtime(WATCH_FILE)

    try:
        while True:
            time.sleep(1)
            current_mtime = get_file_mtime(WATCH_FILE)
            
            if current_mtime != last_mtime:
                # Debounce: Wait a bit to ensure write is complete
                time.sleep(0.5) 
                
                # Double check mtime just in case
                current_mtime = get_file_mtime(WATCH_FILE)
                
                print(f"\n[Change Detected] {time.strftime('%H:%M:%S')}")
                content = read_file_content(WATCH_FILE)
                
                if content:
                    trigger_n8n_webhook(content)
                
                last_mtime = current_mtime
                
    except KeyboardInterrupt:
        print("\n[System] Monitoring stopped.")

if __name__ == "__main__":
    main()
