from google import genai
from google.genai.types import GenerateContentConfig, GoogleSearch, HttpOptions, Tool
from dotenv import load_dotenv
import os
from google import genai
from google.genai.types import GenerateContentConfig, GoogleSearch, Tool

load_dotenv()  # カレントディレクトリの .env を読む
API_KEY = os.getenv("GOOGLE_API_KEY")

client = genai.Client(api_key=API_KEY)
client = genai.Client(http_options=HttpOptions(api_version="v1"))

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="次のアメリカ合衆国の皆既日食はいつですか？",
    config=GenerateContentConfig(
        tools=[
            # Google Search ツール
            Tool(google_search=GoogleSearch())
        ],
    ),
)

print("TEXT:", response.text)

meta = response.candidates[0].grounding_metadata
print("search_entry_point:", getattr(meta, "search_entry_point", None))
print("chunks:", len(meta.grounding_chunks) if meta and meta.grounding_chunks else 0)
for c in meta.grounding_chunks or []:
    if c.web:
        print("-", c.web.title, c.web.uri)
