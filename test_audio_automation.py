import os
import sys
import logging

# AI_agent ルートをパスに追加
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tools.tool_audio_automation import tool_sync_audio_transcriptions

# Logging setup for test
logging.basicConfig(level=logging.INFO)

def test_automation():
    print("Starting Audio Automation Sync Test...")
    result = tool_sync_audio_transcriptions()
    print("\n--- TEST RESULT ---")
    print(result)

if __name__ == "__main__":
    test_automation()
