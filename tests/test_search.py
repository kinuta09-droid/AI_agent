import sys
import os
from google import genai
from google.genai.types import GenerateContentConfig, GoogleSearch, Tool, HttpOptions
from dotenv import load_dotenv

# プロジェクトルートをパスに追加
sys.path.insert(0, r'C:\AI_agent')

try:
    from lib.lib_shared import api_key, MODEL_NAME
    print(f"✅ Loaded config from lib_shared. Testing with model: {MODEL_NAME}")
except ImportError as e:
    print(f"❌ Failed to import lib_shared: {e}")
    sys.exit(1)

client = genai.Client(api_key=api_key, http_options=HttpOptions(api_version="v1beta"))

print(f"Generating content with grounding for {MODEL_NAME}...")
try:
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents="次のアメリカ合衆国の皆既日食はいつですか？",
        config=GenerateContentConfig(
            tools=[
                Tool(google_search=GoogleSearch())
            ],
        ),
    )

    print("TEXT:", response.text)

    if response.candidates[0].grounding_metadata:
        meta = response.candidates[0].grounding_metadata
        print("search_entry_point:", getattr(meta, "search_entry_point", None))
        print("chunks:", len(meta.grounding_chunks) if meta.grounding_chunks else 0)
        for c in meta.grounding_chunks or []:
            if c.web:
                print("-", c.web.title, c.web.uri)
    else:
        print("⚠️ No grounding metadata found.")

except Exception as e:
    print(f"❌ Error during search test: {e}")
