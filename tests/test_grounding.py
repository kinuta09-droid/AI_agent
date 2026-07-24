import os
import sys
from google.genai import types

# Windowsコンソールでの文字化け防止
sys.stdout.reconfigure(encoding='utf-8')

# ルートを sys.path に追加して lib を import 可能にする
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# 施策③ Phase 4a: 独自 client + MODEL_NAME 直書き(3.1-preview)を廃し、共有 client /
# MODEL_NAME(LIGHT・.envで世代交代可) 経由へ。grounding をライブと同一モデル・clientで検証。
from lib.lib_shared import client, MODEL_NAME

print(f"Testing Grounding with {MODEL_NAME}...")

try:
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents="今日の東京の天気は？",
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
    )
    
    print("\n--- Response ---")
    print(response.text)
    with open('grounding_test.txt', 'w', encoding='utf-8') as f:
        f.write(response.text)
    
    if response.candidates[0].grounding_metadata:
        print("\n--- Grounding Metadata Found! ---")
        print("Search Queries:", response.candidates[0].grounding_metadata.search_entry_point.rendered_content)
        
except Exception as e:
    print(f"\nError: {e}")
