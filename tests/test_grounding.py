import os
import sys
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Windowsコンソールでの文字化け防止
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv('C:\\AI_agent\\.env')
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("Error: GOOGLE_API_KEY not found.")
    sys.exit(1)

client = genai.Client(api_key=api_key)
MODEL_NAME = 'gemini-2.5-flash'

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
