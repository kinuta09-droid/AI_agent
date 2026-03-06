import subprocess
import json

def check_folder():
    folder_id = "1cs3wJtoDdfEfiql7D2aB_ZdQ1nFl5QrB"
    p = subprocess.Popen(
        ["gws", "mcp", "--services", "drive"], 
        stdin=subprocess.PIPE, 
        stdout=subprocess.PIPE, 
        encoding="utf-8", 
        bufsize=1,
        shell=True
    )

    def send(method, params, id):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": id}) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        return json.loads(line)

    send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, 1)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
    p.stdin.flush()

    res = send("tools/call", {"name": "drive_files_get", "arguments": {"params": {"fileId": folder_id}}}, 2)
    print(json.dumps(res, indent=2))
    p.terminate()

if __name__ == "__main__":
    check_folder()
