import subprocess
import json

def send(p, method, params, id):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id}) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    return json.loads(line)

def create_folders():
    p = subprocess.Popen(["gws", "mcp", "--services", "drive"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", bufsize=1, shell=True)
    send(p, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "setup", "version": "1"}}, 1)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
    p.stdin.flush()

    # Create root
    print("Creating root...")
    res = send(p, "tools/call", {
        "name": "drive_files_create", 
        "arguments": {
            "body": {"name": "Audio_Automation_Root", "mimeType": "application/vnd.google-apps.folder"}
        }
    }, 2)
    root_id = json.loads(res["result"]["content"][0]["text"])["id"]
    print(f"Root: {root_id}")

    # Create Inbox and Processed
    folder_ids = {}
    for sub in ["Inbox", "Processed"]:
        print(f"Creating {sub}...")
        res = send(p, "tools/call", {
            "name": "drive_files_create",
            "arguments": {
                "body": {"name": sub, "mimeType": "application/vnd.google-apps.folder", "parents": [root_id]}
            }
        }, 3)
        folder_ids[sub] = json.loads(res["result"]["content"][0]["text"])["id"]
        print(f"{sub}: {folder_ids[sub]}")

    p.terminate()
    return folder_ids

if __name__ == "__main__":
    ids = create_folders()
    print(f"\nFINAL_IDS: {json.dumps(ids)}")
