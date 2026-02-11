import os
import subprocess
import datetime

# Configuration
MODEL = "qwen2.5-coder:7b"
TARGET_EXTS = [".py", ".md"]
IGNORE_DIRS = [".git", "__pycache__", "antigravity_env", "node_modules", ".gemini", ".obsidian"]
REPORT_FILE = "Self_Evaluation_Report.md"

def get_files(start_dir):
    files = []
    for root, dirs, filenames in os.walk(start_dir):
        # Ignore directories
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in filenames:
            if any(f.endswith(ext) for ext in TARGET_EXTS):
                files.append(os.path.join(root, f))
    return files

def analyze_file(filepath):
    print(f"🔍 Analyzing {filepath}...")
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        if not content.strip():
            return None

        prompt = f"""
Analyze the following code/text for logical errors, potential bugs, or inconsistencies.
File: {os.path.basename(filepath)}

Content:
```
{content[:8000]} 
```
(Content truncated if too long)

Instructions:
1. Identify any logic errors or bugs.
2. If none found, reply "No issues found."
3. Be concise.
"""
        # Call Ollama
        result = subprocess.run(
            ["ollama", "run", MODEL, prompt],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            shell=True
        )
        
        if result.returncode != 0:
            print(f"❌ Error running model: {result.stderr}")
            return None

        response = result.stdout.strip()
        if "No issues found" in response or len(response) < 10:
            return None
            
        return response

    except Exception as e:
        print(f"❌ Exception analyzing {filepath}: {e}")
        return None

def main():
    print(f"🚀 Starting Local Patrol with {MODEL}...")
    files = get_files(".")
    print(f"found {len(files)} files to analyze.")
    
    report_content = f"# 🛡️ Local Self-Evaluation Report\nDate: {datetime.datetime.now()}\nModel: {MODEL}\n\n"
    
    issues_found = 0
    for f in files:
        analysis = analyze_file(f)
        if analysis:
            issues_found += 1
            rel_path = os.path.relpath(f)
            report_content += f"## 📄 {rel_path}\n{analysis}\n\n---\n\n"
            print(f"  ⚠️ Issues found in {rel_path}")
        else:
            print(f"  ✅ {os.path.basename(f)} - Clean")

    if issues_found == 0:
        report_content += "No critical issues found in scanned files."
        
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write(report_content)
        
    print(f"\n📊 Analysis Complete. Report saved to {REPORT_FILE}")

if __name__ == "__main__":
    main()
