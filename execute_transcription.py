import os
import requests
import json
import time

FAL_KEY = os.environ.get("FAL_KEY")
AUDIO_FILE = "sample_audio.wav"

if not FAL_KEY:
    print("Error: FAL_KEY environment variable not set.")
    exit(1)

def upload_file(file_path):
    print(f"Uploading {file_path} to fal.ai...")
    # 1. Get upload token
    token_url = "https://rest.alpha.fal.ai/storage/auth/token?storage_type=fal-cdn-v3"
    headers = {"Authorization": f"Key {FAL_KEY}"}
    res = requests.post(token_url, headers=headers)
    res.raise_for_status()
    data = res.json()
    
    upload_url = f"{data['base_url']}/files/upload"
    token = data["token"]
    
    # 2. Upload file
    with open(file_path, "rb") as f:
        files = {"file": f}
        upload_res = requests.post(
            upload_url,
            headers={"Authorization": f"Bearer {token}"},
            files=files
        )
    upload_res.raise_for_status()
    return upload_res.json()["access_url"]

def transcribe_audio(audio_url):
    print(f"Submitting transcription job for {audio_url}...")
    # Using fal-ai/whisper
    model_id = "fal-ai/whisper"
    url = f"https://queue.fal.run/{model_id}"
    
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "audio_url": audio_url,
        "task": "transcribe",
        "language": "ja", # Forced Japanese for meeting summary
        "chunk_level": "segment"
    }
    
    # Submit to queue
    res = requests.post(url, headers=headers, json=payload)
    res.raise_for_status()
    request_id = res.json()["request_id"]
    print(f"Job submitted. Request ID: {request_id}")
    
    status_url = f"https://queue.fal.run/{model_id}/requests/{request_id}/status"
    result_url = f"https://queue.fal.run/{model_id}/requests/{request_id}"
    
    # Poll for completion
    while True:
        status_res = requests.get(status_url, headers=headers)
        status_data = status_res.json()
        status = status_data["status"]
        print(f"Status: {status}")
        
        if status == "COMPLETED":
            break
        elif status == "ERROR":
            print(f"Error occurred: {status_data}")
            exit(1)
        
        time.sleep(2)
    
    # Get result
    result_res = requests.get(result_url, headers=headers)
    result_res.raise_for_status()
    return result_res.json()

try:
    audio_url = upload_file(AUDIO_FILE)
    print(f"File uploaded to: {audio_url}")
    
    result = transcribe_audio(audio_url)
    print("Transcription complete!")
    
    transcript = result.get("text", "")
    with open("transcript.text", "w", encoding="utf-8") as f:
        f.write(transcript)
    
    # Also save full JSON for debugging
    with open("transcription_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
        
    print(f"Saved transcript to transcript.text")
except Exception as e:
    print(f"Error: {e}")
