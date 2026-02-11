# -*- coding: utf-8 -*-
import os
import time
import sys
import subprocess
import re
import datetime
import shutil
from dotenv import load_dotenv
from google import genai
from google.genai import types

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
MODEL_NAME = 'gemini-2.5-pro' # (Not used for judging anymore, but kept for config)

INPUT_FILE = r'C:\obsidian\Obsidian\Dev_Logs\Agent_Input.md'
# OUTPUT_FILE unused for judging now
SCRIPT_PATH = r'C:\AI_agent\agent_hello.py'
PYTHON_CMD = sys.executable 

def get_file_mtime(filepath):
    if not os.path.exists(filepath): return 0
    return os.path.getmtime(filepath)

def read_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except: return ""

def run_agent():
    """agent_hello.py を実行する"""
    print(f"\n[Runner] 🚀 Running Agent Script...")
    try:
        subprocess.run([PYTHON_CMD, SCRIPT_PATH], check=True)
        print(f"[Runner] ✅ Execution Complete.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Runner] ❌ Agent execution failed: {e}")
        return False

import keyboard  # pip install keyboard

def on_trigger():
    print("\n[Runner] 🎹 Hotkey 'Ctrl+Enter' detected!")
    
    # Reload input just in case
    instruction = read_file(INPUT_FILE)
    if not instruction:
        print("[Runner] ⚠️ Input file is empty. Skipping.")
        return
        
    run_agent()
    print("[Runner] Waiting for next trigger (Ctrl+Enter)...")

def main():
    print(f"[Runner] 👁️ Watching for Hotkey: Ctrl+Alt+Enter")
    print(f"[Runner] 🎯 Target Script: {SCRIPT_PATH}")
    print("[Runner] Ready. Press 'Ctrl+Alt+Enter' to execute Agent.")
    
    # Hotkey registration
    # suppress=True prevents the keystroke from being sent to other apps (optional, maybe unsafe if user needs newline)
    # let's keep suppress=False default so it works like a normal keypress too, or specific trigger
    
    try:
        # Register the hotkey
        keyboard.add_hotkey('ctrl+alt+enter', on_trigger)
        
        # Keep the script running
        keyboard.wait()
        
    except KeyboardInterrupt:
        print("[Runner] Stopped.")

if __name__ == "__main__":
    main()