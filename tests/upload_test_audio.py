import subprocess
import json
import base64
import os

def send(p, method, params, id):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id}) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    if not line:
        return None
    return json.loads(line)

def upload_test_file():
    p = subprocess.Popen(
        ["gws", "mcp", "--services", "drive"], 
        stdin=subprocess.PIPE, 
        stdout=subprocess.PIPE, 
        encoding="utf-8", 
        bufsize=1,
        shell=True
    )

    # Init
    send(p, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "setup-script", "version": "1.0.0"}
    }, 1)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
    p.stdin.flush()

    # Upload via binary content if supported, or via local file argument if accessible
    print("Uploading sample_audio.wav to Inbox...")
    
    # Note: drive_files_create usually supports --file flag in CLI, 
    # but in MCP tool, we might need to use the local path if the tool supports it.
    # Looking at inspect results, it takes params.
    # Let's try to just use gws CLI with simpler quoting or via Python subprocess list
    p.terminate()
    
    inbox_id = "1cs3wJtoDdfEfiql7D2aB_ZdQ1nFl5QrB"
    cmd = [
        "gws", "drive", "files", "create",
        "--params", json.dumps({
            "requestBody": {
                "name": "サンプル会議音声（テスト用）.wav",
                "parents": [inbox_id]
            }
        }),
        "--file", "sample_audio.wav"
    ]
    
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        print("Upload successful!")
    else:
        print(f"Upload failed: {result.stderr}")

if __name__ == "__main__":
    upload_test_file()
