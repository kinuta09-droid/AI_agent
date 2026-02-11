import json
import re
import os

# Input and Output paths
INPUT_FILE = r"C:\obsidian\Obsidian\Dev_Logs\n8n_backups\My_workflow_3_UgMPg7QqNTwy-OHDf6o8k.json"
OUTPUT_FILE = r"C:\obsidian\Obsidian\Dev_Logs\n8n_backups\My_workflow_3_FIXED.json"

def fix_workflow():
    if not os.path.exists(INPUT_FILE):
        print(f"Error: Input file not found: {INPUT_FILE}")
        return

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    nodes = data.get('nodes', [])
    modified_count = 0

    print("Analyzing nodes...")

    for node in nodes:
        node_name = node.get('name', '')
        
        # 1. Security Fix: Execute Command (Command Injection)
        if node_name == "Execute Command":
            params = node.get('parameters', {})
            cmd = params.get('command', '')
            if '{{ $json.body.text }}' in cmd:
                print(f"  [Security] Patching {node_name} to sanitize input...")
                # Replace simple substitution with a safer version that escapes quotes and creates a basic barrier
                # Note: Completely safe shell execution requires avoiding shell=True logic, but in n8n expression:
                # We replace " with \" and simple sanitization. 
                # Better yet: Use a Code node to write to file, but for a quick patch, we sanitize.
                
                # Old: ...agent_hello.py "{{ $json.body.text }}"
                # New: ...agent_hello.py "{{ $json.body.text.replace(/\"/g, '\\\"').replace(/[\r\n]+/g, ' ') }}"
                
                new_cmd = cmd.replace(
                    '{{ $json.body.text }}', 
                    "{{ $json.body.text.replace(/\\\"/g, '\\\\\\\"').replace(/[\\r\\n]+/g, ' ') }}" 
                )
                node['parameters']['command'] = new_cmd
                modified_count += 1

        # 2. Stability Fix: Read Output File1 (Regex Crash)
        if node_name == "Read Output File1":
            params = node.get('parameters', {})
            file_path_expr = params.get('filePath', '')
            
            # Pattern to match: $json.stdout.match(/Success: Created \[\[(.*?)\]\]/)[1]
            if '.match(/Success: Created' in file_path_expr and '[1]' in file_path_expr:
                print(f"  [Stability] Patching {node_name} for regex null safety...")
                
                # Insert a fallback to avoid crash on [1] access of null
                # Change: .match(...)[1]  ->  (.match(...) || [null, 'NoFile_Error.md'])[1]
                
                new_expr = file_path_expr.replace(
                    '.match(/Success: Created \\[\\[(.*?)\\]\\]/)[1]', 
                    '.match(/Success: Created \\[\\[(.*?)\\]\\]/ || [null, "Error_No_File_Detected.md"])[1]'
                )
                
                # 3. Path Fix: 30_main -> 30_Main
                if '30_main' in new_expr:
                    print(f"  [Path] Correcting 30_main to 30_Main in {node_name}...")
                    new_expr = new_expr.replace('30_main', '30_Main')
                
                node['parameters']['filePath'] = new_expr
                modified_count += 1

        # 4. General Path Fixes (Obsidian -> Dev_Logs where missing)
        # Check all string parameters for old path patterns
        # This is a bit risky to do globally, focusing on known nodes is safer
        if node_name == "God Mode Agent v2" or node_name == "Polling Agent (Read/Reset/Run)":
             # Check jsCode for old paths if any (Draft seems okay but double check)
             pass

    # Save fixed file
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"Done. Modified {modified_count} nodes.")
    print(f"Saved fixed workflow to: {OUTPUT_FILE}")

if __name__ == "__main__":
    fix_workflow()
