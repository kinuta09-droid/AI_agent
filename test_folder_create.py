import subprocess
import json

def test_create(params):
    cmd = [
        "gws", "drive", "files", "create",
        "--params", json.dumps(params),
        "--format", "json"
    ]
    print(f"Testing with params: {json.dumps(params)}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding='utf-8')
    if result.returncode == 0:
        print(f"Success: {result.stdout}")
        return json.loads(result.stdout)["id"]
    else:
        print(f"Failed: {result.stderr}")
        return None

if __name__ == "__main__":
    # Test 1: Top-level name/mimeType
    id1 = test_create({"name": "TestFolder_TopLevel", "mimeType": "application/vnd.google-apps.folder"})
    
    # Test 2: Inside requestBody
    id2 = test_create({"requestBody": {"name": "TestFolder_RequestBody", "mimeType": "application/vnd.google-apps.folder"}})
    
    if id1:
        print(f"ID1 metadata: {subprocess.run(['gws', 'drive', 'files', 'get', '--params', json.dumps({'fileId': id1}), '--format', 'json'], shell=True, capture_output=True, text=True).stdout}")
    if id2:
        print(f"ID2 metadata: {subprocess.run(['gws', 'drive', 'files', 'get', '--params', json.dumps({'fileId': id2}), '--format', 'json'], shell=True, capture_output=True, text=True).stdout}")
