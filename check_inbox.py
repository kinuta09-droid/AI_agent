import subprocess
import json

def check_inbox():
    inbox_id = "1cs3wJtoDdfEfiql7D2aB_ZdQ1nFl5QrB"
    query = f"'{inbox_id}' in parents and trashed = false"
    
    cmd = [
        "gws", "drive", "files", "list",
        "--params", json.dumps({"q": query}),
        "--format", "json"
    ]
    
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        data = json.loads(result.stdout)
        files = data.get("files", [])
        print(f"Inbox contains {len(files)} files.")
        for f in files:
            print(f"- {f['name']} ({f['id']})")
    else:
        print(f"Failed to list: {result.stderr}")

if __name__ == "__main__":
    check_inbox()
