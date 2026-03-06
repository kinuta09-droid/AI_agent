import subprocess
import json
import sys

def create_folder(name, parent_id=None):
    request_body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent_id:
        request_body["parents"] = [parent_id]
        
    params = {"requestBody": request_body}
    
    cmd = [
        "gws", "drive", "files", "create",
        "--params", json.dumps(params),
        "--format", "json"
    ]
    
    print(f"Creating folder: {name}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding='utf-8')
    if result.returncode == 0:
        data = json.loads(result.stdout)
        print(f"Success: {name} ID = {data['id']} (Mime: {data.get('mimeType')})")
        return data["id"]
    else:
        print(f"Failed to create {name}: {result.stderr}")
        return None

if __name__ == "__main__":
    root_id = create_folder("Audio_Automation_Final")
    if root_id:
        inbox_id = create_folder("Inbox", root_id)
        processed_id = create_folder("Processed", root_id)
        
        print("\nSUMMARY:")
        print(f"INBOX_ID: {inbox_id}")
        print(f"PROCESSED_ID: {processed_id}")
