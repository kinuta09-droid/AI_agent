import json
import os

# Input and Output paths
# Targeting the FIXED workflow as the base, or the timestamped one if we know it.
# Ideally we find the latest active one, but for safety let's target the known FIXED one and user can import.
# Or better, target the one likely to be active: My_workflow_3_UgMPg7QqNTwy-OHDf6o8k.json
INPUT_FILE = r"C:\obsidian\Obsidian\Dev_Logs\n8n_backups\My_workflow_3_UgMPg7QqNTwy-OHDf6o8k.json"
OUTPUT_FILE = r"C:\obsidian\Obsidian\Dev_Logs\n8n_backups\My_workflow_3_UPDATED.json"

if not os.path.exists(INPUT_FILE):
    print(f"Error: {INPUT_FILE} not found.")
    exit(1)

with open(INPUT_FILE, 'r', encoding='utf-8') as f:
    workflow = json.load(f)

# The new expression should NOT prepend '30_Main'.
# Old: "={{ \"C:\\\\obsidian\\\\Obsidian\\\\30_Main\\\\\" + $json.stdout.match(...)[1].trim() }}"
# New: "={{ \"C:\\\\obsidian\\\\Obsidian\\\\\" + $json.stdout.match(...)[1].trim() }}"
# because the agent now outputs "30_Main/File.md" or "02_News/File.md"

TARGET_NODE_NAME = "Read Output File3"

patched = False
for node in workflow.get('nodes', []):
    if node.get('name') == TARGET_NODE_NAME:
        params = node.get('parameters', {})
        old_expr = params.get('filePath', '')
        
        # We want to remove '30_Main\\\\' from the string concatenation.
        # But we must be careful with the exact string structure.
        # Let's simply set the new expression.
        
        new_expr = "={{ \"C:\\\\obsidian\\\\Obsidian\\\\\" + $json.stdout.match(/Success: Created \\[\\[(.*?)\\]\\]/ || [null, \"Error_No_File_Detected.md\"])[1].trim() }}"
        
        if old_expr != new_expr:
            params['filePath'] = new_expr
            print(f"Updated {TARGET_NODE_NAME} filePath expression.")
            patched = True
        else:
            print(f"{TARGET_NODE_NAME} already has the desired expression.")

# Also update activeVersion if present
active_version = workflow.get('activeVersion', {})
for node in active_version.get('nodes', []):
    if node.get('name') == TARGET_NODE_NAME:
         params = node.get('parameters', {})
         new_expr = "={{ \"C:\\\\obsidian\\\\Obsidian\\\\\" + $json.stdout.match(/Success: Created \\[\\[(.*?)\\]\\]/ || [null, \"Error_No_File_Detected.md\"])[1].trim() }}"
         params['filePath'] = new_expr
         print(f"Updated {TARGET_NODE_NAME} in activeVersion.")
         patched = True

if patched:
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(workflow, f, indent=2, ensure_ascii=False)
    print(f"Saved updated workflow to {OUTPUT_FILE}")
else:
    print("No changes needed or node not found.")
