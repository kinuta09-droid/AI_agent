# -*- coding: utf-8 -*-
"""
Audio Automation Tool
Google Drive の特定フォルダ(Inbox)を監視し、
音声があれば文字起こし・要約を行い、Processed フォルダに移動する。
"""

import os
import json
import logging
import datetime
import requests
from lib.lib_shared import _run_gws_command

# Configuration
INBOX_FOLDER_ID = "18jyOBekKVjqk4ZULfTDau6lJhIIovEqB"
PROCESSED_FOLDER_ID = "1vXfxriqaBWW3HIWBQm1AU5W8-5RM6_NM"
TMP_DIR = "tmp_audio"

logger = logging.getLogger("AudioAutomation")

def tool_sync_audio_transcriptions() -> str:
    """Inbox フォルダをチェックし、新しい音声があれば文字起こし・要約を実行する。"""
    os.makedirs(TMP_DIR, exist_ok=True)
    
    # 1. List files in Inbox
    query = f"'{INBOX_FOLDER_ID}' in parents and trashed = false"
    data = _run_gws_command(["drive", "files", "list", "--params", json.dumps({"q": query}), "--format", "json"])
    
    if not data or "files" not in data or not data["files"]:
        return "Inbox は空です。処理するファイルはありません。"
    
    files = data["files"]
    processed_count = 0
    results = []

    for file in files:
        file_id = file["id"]
        file_name = file["name"]
        mime_type = file.get("mimeType", "")
        
        # 音声ファイル、または動画ファイル（mp4等）を対象にする
        if "audio" in mime_type or "video" in mime_type or file_name.endswith(('.wav', '.mp3', '.m4a', '.mp4')):
            logger.info(f"Processing: {file_name} ({file_id})")
            
            try:
                # 2. Download
                local_path = os.path.join(TMP_DIR, file_name)
                _run_gws_command([
                    "drive", "files", "get", 
                    "--params", json.dumps({"fileId": file_id, "alt": "media"}),
                    "--output", local_path
                ])
                
                # 3. Transcribe (OpenAI Whisper)
                transcript = _transcribe_openai(local_path)
                
                # 4. Summarize (Gemini)
                summary = _summarize_gemini(transcript, file_name)
                
                # 5. Save Summary to Drive (Same Inbox with suffix or specific summary folder)
                summary_filename = f"Summary_{os.path.splitext(file_name)[0]}.md"
                _save_to_drive(summary, summary_filename, INBOX_FOLDER_ID)
                
                # 6. Move Original to Processed
                _move_to_processed(file_id, file_name)
                
                # Cleanup local file
                os.remove(local_path)
                
                processed_count += 1
                results.append(f"✅ {file_name}: 処理完了")
            except Exception as e:
                logger.error(f"Error processing {file_name}: {e}")
                results.append(f"❌ {file_name}: エラー - {e}")

    return f"{processed_count} 件のファイルを処理しました。\n" + "\n".join(results)

def _transcribe_openai(file_path: str) -> str:
    """OpenAI Whisper API を使用して文字起こしを行う。"""
    api_key = os.environ.get("OPENAI_API_KEY")
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"model": "whisper-1", "language": "ja"}

    with open(file_path, "rb") as f:
        files = {"file": f}
        res = requests.post(url, headers=headers, files=files, data=data, timeout=300)
    res.raise_for_status()
    return res.json()["text"]

def _summarize_gemini(text: str, original_name: str) -> str:
    """Gemini API を使用して要約を生成する (リトライ付き)。"""
    from google import genai
    from google.genai.errors import APIError
    import time
    
    # 意図: 環境変数 GOOGLE_API_KEY からクライアントを作成し、最新の google-genai SDK を利用する
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
    
    # モデルの候補（安定性とクォータを考慮し、最新の 2.5-flash を最優先にする）
    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash"]
    
    prompt = f"""
    ファイル名: {original_name}
    以下の文字起こし内容を要約してください。
    
    【内容】
    {text}
    
    【形式】
    1. 概要
    2. 重要ポイント (3点)
    3. 次の課題・アクションアイテム
    """

    for model_name in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                return f"# {original_name} 要約\n\n日付: {datetime.date.today()}\n\n{response.text}\n\n---\n## 書き起こし\n{text}"
            except APIError as e:
                # 意図: クォータ超過 (429) の場合はウェイトを置いてリトライ。
                # それ以外のエラー (404等) はリトライせず、速やかに次のフォールバックモデルへ移行する。
                if e.code == 429:
                    wait_time = (attempt + 1) * 5
                    logger.warning(f"Gemini API 429: Retrying in {wait_time}s... (Attempt {attempt+1})")
                    time.sleep(wait_time)
                else:
                    logger.error(f"APIError with model {model_name}: {e.code} - {e.message}")
                    break
            except Exception as e:
                logger.error(f"Unexpected error with model {model_name}: {e}")
                break
    
    raise Exception("Gemini API で要約を生成できませんでした。リトライ回数を超過しました。")

def _save_to_drive(content: str, filename: str, folder_id: str):
    """テキスト内容を Markdown ファイルとして Drive に保存する。"""
    tmp_md = os.path.join(TMP_DIR, filename)
    with open(tmp_md, "w", encoding="utf-8") as f:
        f.write(content)
    
    _run_gws_command([
        "drive", "files", "create",
        "--params", json.dumps({
            "requestBody": {
                "name": filename,
                "mimeType": "text/markdown",
                "parents": [folder_id]
            }
        }),
        "--file", tmp_md
    ])
    os.remove(tmp_md)

def _move_to_processed(file_id: str, file_name: str):
    """ファイルを Inbox から Processed フォルダへ移動する。"""
    # parents の更新（修正は patch 相当だが、gws update や move を使用）
    # gws 0.7.0 の move コマンドがあればそれを使用、なければ update で parents を操作
    _run_gws_command([
        "drive", "files", "update",
        "--params", json.dumps({
            "fileId": file_id,
            "addParents": PROCESSED_FOLDER_ID,
            "removeParents": INBOX_FOLDER_ID
        })
    ])
