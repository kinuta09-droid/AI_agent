import subprocess
import json
import time

def send(p, method, params, id):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id}) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    if not line:
        return None
    return json.loads(line)

def setup_folders():
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

    # 1. Search for root folder
    print("Searching for 'Audio_Transcriptions' folder...")
    query = "name = 'Audio_Transcriptions' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    list_res = send(p, "tools/call", {
        "name": "drive_files_list",
        "arguments": {"params": {"q": query}}
    }, 2)
    
    root_id = None
    if list_res and not list_res.get("isError"):
        data = json.loads(list_res["result"]["content"][0]["text"])
        files = data.get("files", [])
        if files:
            root_id = files[0]["id"]
            print(f"Found existing root: {root_id}")

    if not root_id:
        print("Creating root folder 'Audio_Transcriptions'...")
        create_res = send(p, "tools/call", {
            "name": "drive_files_create",
            "arguments": {"params": {"requestBody": {"name": "Audio_Transcriptions", "mimeType": "application/vnd.google-apps.folder"}}}
        }, 3)
        if create_res and not create_res.get("isError"):
            data = json.loads(create_res["result"]["content"][0]["text"])
            root_id = data["id"]
            print(f"Created root: {root_id}")

    if not root_id:
        print("Failed to ensure root folder.")
        p.terminate()
        return

    # 2. Ensure Inbox and Processed
    for sub in ["Inbox", "Processed"]:
        print(f"Checking for subfolder '{sub}'...")
        query = f"name = '{sub}' and '{root_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        sub_list = send(p, "tools/call", {
            "name": "drive_files_list",
            "arguments": {"params": {"q": query}}
        }, 4)
        
        sub_id = None
        if sub_list and not sub_list.get("isError"):
            data = json.loads(sub_list["result"]["content"][0]["text"])
            files = data.get("files", [])
            if files:
                sub_id = files[0]["id"]
                print(f"Found existing {sub}: {sub_id}")
        
        if not sub_id:
            print(f"Creating subfolder '{sub}'...")
            sub_create = send(p, "tools/call", {
                "name": "drive_files_create",
                "arguments": {"params": {"requestBody": {"name": sub, "mimeType": "application/vnd.google-apps.folder", "parents": [root_id]}}}
            }, 5)
            if sub_create and not sub_create.get("isError"):
                data = json.loads(sub_create["result"]["content"][0]["text"])
                sub_id = data["id"]
                print(f"Created {sub}: {sub_id}")
    
    p.terminate()

if __name__ == "__main__":
    setup_folders()
