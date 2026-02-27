import subprocess
import json
import time

def get_notebooklm_info():
    print("Starting notebooklm-mcp...")
    p = subprocess.Popen(["notebooklm-mcp.exe"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", text=True)
    
    def send(method, params=None, req_id=1):
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line: return None
            try:
                data = json.loads(line)
                if "id" in data and data["id"] == req_id:
                    return data
            except:
                pass
                
    print("Initializing...")
    init = send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}, 1)
    
    # notify initialized (no id response)
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
    p.stdin.flush()
    
    print("Calling source_get_content...")
    res_source = send("tools/call", {"name": "source_get_content", "arguments": {"source_id": "dd923e3e-80c0-4e52-a605-eb3966262c6f"}}, 3)
    
    with open("mcp_out.json", "w", encoding="utf-8") as f:
        json.dump({"source_content": res_source}, f, ensure_ascii=False, indent=2)
    print("Output written to mcp_out.json")
    p.terminate()

if __name__ == "__main__":
    get_notebooklm_info()
