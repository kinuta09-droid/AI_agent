import os
import time
from google import genai
from dotenv import load_dotenv

# .env から API キーを読み込む
load_dotenv()
api_key = os.environ.get("GOOGLE_API_KEY")

if not api_key:
    print("Error: GOOGLE_API_KEY not found in .env")
    exit(1)

# 意図: 最新の google-genai SDK クライアントを初期化する
client = genai.Client(api_key=api_key)

AUDIO_FILE = "sample_audio.wav"

def process_audio():
    print(f"Uploading {AUDIO_FILE} to Gemini API...")
    # 意図: 新しい SDK では client.files.upload を利用してファイルをアップロードする
    audio_file = client.files.upload(file=AUDIO_FILE)
    print(f"Uploaded file: {audio_file.name}")

    # ファイルの処理待ち
    while audio_file.state.name == "PROCESSING":
        print("Processing...", end="\r")
        time.sleep(2)
        # 意図: ファイルのステータス更新のため client.files.get を呼び出す
        audio_file = client.files.get(name=audio_file.name)

    if audio_file.state.name == "FAILED":
        print(f"File processing failed: {audio_file.state.name}")
        exit(1)

    print("\nProcessing complete. Generating transcript and summary...")

    prompt = """
    この音声ファイルを解析して、以下の形式で出力してください。

    1. 全文書き起こし (Transcription)
    2. 要約 (Summary): 
       - 議題は何ですか？
       - 決定事項やアクションアイテムは何ですか？
       - 重要なポイントを3点挙げてください。

    言語: 日本語
    """

    # 意図: 最新の推奨モデルである gemini-2.5-flash を用いてコンテンツを生成する
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[audio_file, prompt]
    )

    print("\n--- Result ---")
    print(response.text)

    # 結果を保存
    output_path = "gemini_result.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# 音声解析結果\n\n")
        f.write(response.text)
    
    # ファイルのクリーンアップ（任意）
    # client.files.delete(name=audio_file.name)
    
    print(f"\nResult saved to {output_path}")

try:
    process_audio()
except Exception as e:
    print(f"Error: {e}")
