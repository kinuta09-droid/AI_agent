import subprocess
import json
import time

def test_gws_mcp():
    print("Starting gws mcp...")
    # Start the gws MCP server prioritizing calendar and drive to avoid massive tool lists at first
    p = subprocess.Popen(
        ["gws", "mcp", "--services", "calendar,drive,sheets,gmail,docs"], 
        stdin=subprocess.PIPE, 
        stdout=subprocess.PIPE, 
        encoding="utf-8", 
        text=True,
        shell=True
    )
    
    def send(method, params=None, req_id=1):
        msg = {
            "jsonrpc": "2.0", 
            "id": req_id, 
            "method": method, 
            "params": params or {}
        }
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        
        while True:
            line = p.stdout.readline()
            if not line:
                return None
            try:
                data = json.loads(line)
                if "id" in data and data["id"] == req_id:
                    return data
            except json.JSONDecodeError:
                # Some logs might be printed to stdout directly, ignore them
                print(f"DEBUG: {line.strip()}")
                pass
                
    print("Initializing JSON-RPC session...")
    init_res = send("initialize", {
        "protocolVersion": "2024-11-05", 
        "capabilities": {}, 
        "clientInfo": {"name": "antigravity-test", "version": "1.0"}
    }, 1)
    print(f"Init Response: {json.dumps(init_res, indent=2)}")
    
    # Notify initialized
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
    p.stdin.flush()
    
    print("\nCalling drive_files_get...")
    file_id = "1jla8CbApXoH66nmzgBUP5y4qDpxugKn-"
    get_res = send("tools/call", {
        "name": "drive_files_get", 
        "arguments": {"params": {"fileId": file_id}}
    }, 2)
    
    print(f"Get Response: {json.dumps(get_res, indent=2)}")
    
    if get_res and "result" in get_res:
        content = get_res["result"].get("content", [])
        if content:
             print(f"\nSuccess! File info: {content[0].get('text')}")
    else:
        print(f"\nFailed to get file: {json.dumps(get_res, indent=2)}")
        
    p.terminate()

if __name__ == "__main__":
    test_gws_mcp()
