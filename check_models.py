import os
import sys
from dotenv import load_dotenv
from google import genai

# Windowsコンソールでの文字化け防止
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv('C:\\AI_agent\\.env')
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("Error: GOOGLE_API_KEY not found.")
    sys.exit(1)

client = genai.Client(api_key=api_key)

print("Listing available models...")
try:
    # Based on SDK documentation, listing models might look like this or similar
    # The error message suggested Call ListModels
    # For google.genai V1 SDK it is often client.models.list()
    with open(r'C:\obsidian\Obsidian\Dev_Logs\model_list.txt', 'w', encoding='utf-8') as f:
        for m in client.models.list():
            print(f"- {m.name}")
            f.write(f"{m.name}\n")
except Exception as e:
    print(f"Error listing models: {e}")