import subprocess
import json

def send(p, method, params, id):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id}) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    return json.loads(line)

def test_mcp_create():
    p = subprocess.Popen(["gws", "mcp", "--services", "drive"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", bufsize=1, shell=True)
    send(p, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, 1)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
    p.stdin.flush()

    # Get schema of drive_files_create
    res = send(p, "tools/list", {}, 2)
    for t in res["result"]["tools"]:
        if t["name"] == "drive_files_create":
            print(f"Tool Schema: {json.dumps(t, indent=2)}")
            break

    # Try top-level arguments
    print("\nTrying top-level arguments...")
    res1 = send(p, "tools/call", {
        "name": "drive_files_create",
        "arguments": {"name": "Test_TopLevel", "mimeType": "application/vnd.google-apps.folder"}
    }, 3)
    print(f"Res1: {json.dumps(res1, indent=2)}")

    p.terminate()

if __name__ == "__main__":
    test_mcp_create()
