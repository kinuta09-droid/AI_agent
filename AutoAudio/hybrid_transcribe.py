import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ.get("OPENAI_API_KEY")

if not api_key:
    print("Error: OPENAI_API_KEY not found in .env")
    exit(1)

AUDIO_FILE = "sample_audio.wav"

def transcribe():
    print(f"Transcribing {AUDIO_FILE} using OpenAI Whisper...")
    
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    
    files = {
        "file": open(AUDIO_FILE, "rb")
    }
    
    data = {
        "model": "whisper-1",
        "language": "ja"
    }
    
    res = requests.post(url, headers=headers, files=files, data=data)
    res.raise_for_status()
    
    transcript = res.json()["text"]
    print("\n--- Transcription Result ---")
    print(transcript)
    
    with open("transcript.text", "w", encoding="utf-8") as f:
        f.write(transcript)
    
    return transcript

def summarize(text):
    print("\nSummarizing with Gemini...")
    from google import genai
    # 意図: 最新の google-genai SDK クライアントを初期化する
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
    
    prompt = f"""
    以下の会議の文字起こしを元に要約を作成してください。

    【文字起こし】
    {text}

    【出力形式】
    1. 議題 (Topic)
    2. 重要なポイント (3点)
    3. 次のアクション (Action Items)

    言語: 日本語
    """
    
    # 意図: 推奨モデルである gemini-2.5-flash を利用してコンテンツを生成する
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    
    print("\n--- Summary Result ---")
    print(response.text)
    
    with open("gemini_summary.md", "w", encoding="utf-8") as f:
        f.write("# 音声解析要約\n\n")
        f.write(response.text)
        f.write("\n\n## 文字起こし全文\n\n")
        f.write(text)
    
    print("\nResults saved to gemini_summary.md")

try:
    transcript = transcribe()
    summarize(transcript)
except Exception as e:
    print(f"Error: {e}")
