import subprocess
import json
import time

def send(method, params, id):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id}) + "\n")
    p.stdin.flush()
    line = p.stdout.readline()
    if not line:
        return None
    return json.loads(line)

p = subprocess.Popen(
    ["gws", "mcp", "--services", "drive"], 
    stdin=subprocess.PIPE, 
    stdout=subprocess.PIPE, 
    encoding="utf-8", 
    bufsize=1,
    shell=True
)

# Init
send("initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "test-client", "version": "1.0.0"}
}, 1)
p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
p.stdin.flush()

# List tools
res = send("tools/list", {}, 2)
if res and "result" in res:
    tools = res["result"]["tools"]
    for t in tools:
        if t["name"] == "drive_files_get":
            print(json.dumps(t, indent=2))
            break

p.terminate()
