import os
import time
import sys

# 監視対象
WATCH_FILE = r'C:\obsidian\Obsidian\Dev_Logs\Agent_output.md'

def get_file_mtime(filepath):
    if not os.path.exists(filepath):
        return 0
    return os.path.getmtime(filepath)

def main():
    print(f"[System] 👁️ Monitoring started for: {WATCH_FILE}")
    print("[System] Waiting for external updates (e.g., from n8n)...")
    
    last_mtime = get_file_mtime(WATCH_FILE)
    
    try:
        while True:
            time.sleep(1) # 1秒ごとにチェック
            current_mtime = get_file_mtime(WATCH_FILE)
            
            if current_mtime != last_mtime:
                # 更新を検知！
                print("\n" + "="*30)
                print("[System] 🔄 DETECTED FILE UPDATE!")
                print("[System] MISSION COMPLETE") # エージェントへの起動トリガー
                print("="*30 + "\n")
                break # 役割を終えて終了（エージェントにバトンタッチ）
            
            last_mtime = current_mtime
            
    except KeyboardInterrupt:
        print("[System] Monitoring stopped by user.")

if __name__ == "__main__":
    main()
