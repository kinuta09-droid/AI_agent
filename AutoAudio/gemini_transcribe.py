import os
import time
import google.generativeai as genai
from dotenv import load_dotenv

# .env から API キーを読み込む
load_dotenv()
api_key = os.environ.get("GOOGLE_API_KEY")

if not api_key:
    print("Error: GOOGLE_API_KEY not found in .env")
    exit(1)

genai.configure(api_key=api_key)

AUDIO_FILE = "sample_audio.wav"

def process_audio():
    print(f"Uploading {AUDIO_FILE} to Gemini API...")
    audio_file = genai.upload_file(path=AUDIO_FILE, mime_type="audio/wav")
    print(f"Uploaded file: {audio_file.name}")

    # ファイルの処理待ち
    while audio_file.state.name == "PROCESSING":
        print("Processing...", end="\r")
        time.sleep(2)
        audio_file = genai.get_file(audio_file.name)

    if audio_file.state.name == "FAILED":
        print(f"File processing failed: {audio_file.state.name}")
        exit(1)

    print("\nProcessing complete. Generating transcript and summary...")

    # モデルの初期化（Gemini 2.0 Flash を使用）
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = """
    この音声ファイルを解析して、以下の形式で出力してください。

    1. 全文書き起こし (Transcription)
    2. 要約 (Summary): 
       - 議題は何ですか？
       - 決定事項やアクションアイテムは何ですか？
       - 重要なポイントを3点挙げてください。

    言語: 日本語
    """

    response = model.generate_content([audio_file, prompt])

    print("\n--- Result ---")
    print(response.text)

    # 結果を保存
    output_path = "gemini_result.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# 音声解析結果\n\n")
        f.write(response.text)
    
    # ファイルのクリーンアップ（任意）
    # genai.delete_file(audio_file.name)
    
    print(f"\nResult saved to {output_path}")

try:
    process_audio()
except Exception as e:
    print(f"Error: {e}")
