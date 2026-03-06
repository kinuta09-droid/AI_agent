import os
import sys
import logging

# AI_agent ルートをパスに追加
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tools.tool_gmail_manager import get_gmail_summary

# Logging setup
logging.basicConfig(level=logging.INFO)

def test_gmail():
    print("Testing Updated Gmail Summary (via gws)...")
    summary = get_gmail_summary()
    print("\n--- GMAIL SUMMARY ---")
    print(summary)

if __name__ == "__main__":
    test_gmail()
