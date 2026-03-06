import subprocess
import json

def send(p, method, params, id):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id}) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    if not line: return None
    return json.loads(line)

def setup():
    p = subprocess.Popen(["gws", "mcp", "--services", "drive"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", bufsize=1, shell=True)
    send(p, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "setup", "version": "1"}}, 1)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
    p.stdin.flush()

    # Create root
    print("Creating root...")
    res = send(p, "tools/call", {"name": "drive_files_create", "arguments": {"params": {"requestBody": {"name": "Audio_Automation", "mimeType": "application/vnd.google-apps.folder"}}}}, 2)
    root_data = json.loads(res["result"]["content"][0]["text"])
    root_id = root_data["id"]
    print(f"Root: {root_id}")

    # Create Inbox and Processed
    for sub in ["Inbox", "Processed"]:
        print(f"Creating {sub}...")
        res = send(p, "tools/call", {"name": "drive_files_create", "arguments": {"params": {"requestBody": {"name": sub, "mimeType": "application/vnd.google-apps.folder", "parents": [root_id]}}}}, 3)
        sub_data = json.loads(res["result"]["content"][0]["text"])
        print(f"{sub}: {sub_data['id']}")

    p.terminate()

if __name__ == "__main__":
    setup()
