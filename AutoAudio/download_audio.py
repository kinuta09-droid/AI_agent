import subprocess
import json

file_id = "1jla8CbApXoH66nmzgBUP5y4qDpxugKn-"
output_file = "sample_audio.wav"

# Using gws CLI via shell to download the file
# Note: --alt media is required to download the binary content
cmd = [
    "gws", "drive", "files", "get", 
    "--params", json.dumps({"fileId": file_id, "alt": "media"}),
    "--output", output_file
]

print(f"Running: {' '.join(cmd)}")
result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

if result.returncode == 0:
    print(f"Successfully downloaded to {output_file}")
else:
    print(f"Failed to download: {result.stderr}")
    print(f"Output: {result.stdout}")
