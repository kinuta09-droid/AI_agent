import subprocess
import json
import sys

def upload_test_file():
    inbox_id = "1cs3wJtoDdfEfiql7D2aB_ZdQ1nFl5QrB"
    file_name = "サンプル会議音声（テスト用）.wav"
    local_file = "sample_audio.wav"
    
    if not os.path.exists(local_file):
        print(f"Error: {local_file} not found.")
        return

    params = {
        "requestBody": {
            "name": file_name,
            "parents": [inbox_id]
        }
    }
    
    # Use list arguments to avoid shell quoting issues
    cmd = [
        "gws", "drive", "files", "create",
        "--params", json.dumps(params),
        "--file", local_file
    ]
    
    print(f"Executing: {' '.join(cmd)}")
    # Note: On Windows, we might still need shell=True for 'gws' (npm global bin)
    # but we can pass the cmd as list to Popen if shell=True is used with proper care
    # or just use subprocess.run with list and shell=True which handles escaping on Windows better.
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding='utf-8')
    
    if result.returncode == 0:
        print("Upload successful!")
        print(result.stdout)
    else:
        print(f"Upload failed (exit code {result.returncode})")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")

if __name__ == "__main__":
    import os
    upload_test_file()
