import subprocess
import json

def search_folders():
    query = "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    cmd = [
        "gws", "drive", "files", "list",
        "--params", json.dumps({"q": query}),
        "--format", "json"
    ]
    
    print(f"Searching for folders with query: {query}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding='utf-8')
    if result.returncode == 0:
        data = json.loads(result.stdout)
        files = data.get("files", [])
        print(f"Found {len(files)} folders.")
        for f in files:
            print(f"- {f['name']} ({f['id']})")
    else:
        print(f"Failed to search: {result.stderr}")

if __name__ == "__main__":
    search_folders()
