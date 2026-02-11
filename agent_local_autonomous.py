import os
import subprocess
import datetime
import argparse

# Default Configuration
DEFAULT_MODEL = "qwen2.5-coder:7b"
REPORT_FILE = "Self_Evaluation_Report.md"

def run_ollama(model, prompt):
    try:
        # Use simple subprocess run
        # Note: Ensure 'ollama' is in PATH or use full path if needed
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            shell=True
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {e}"

def tool_list_files(directory="."):
    """Lists source files recursively."""
    target_exts = {'.py', '.md', '.json', '.js', '.ts'}
    ignore_dirs = {'.git', '__pycache__', 'node_modules', 'antigravity_env', '.gemini', '.obsidian'}
    
    file_list = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for f in files:
            if os.path.splitext(f)[1] in target_exts:
                file_list.append(os.path.join(root, f))
    return file_list

def tool_read_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

def tool_run_check(filepath):
    ext = os.path.splitext(filepath)[1]
    if ext == '.py':
        try:
            subprocess.check_output(['python', '-m', 'py_compile', filepath], stderr=subprocess.STDOUT)
            return "Syntax Check: OK"
        except subprocess.CalledProcessError as e:
            return f"Syntax Error: {e.output.decode('utf-8')}"
    return "Check Skipped"

def agent_step(model, filepath):
    content = tool_read_file(filepath)
    static_check = tool_run_check(filepath)
    
    prompt = f"""
You are an expert code auditor. Analyze this file.

File: {filepath}
Static Check Result: {static_check}

Content:
```
{content[:6000]}
```

Task:
1. Identify logical errors, bugs, or dangerous patterns.
2. If the Static Check failed, explain why.
3. If no issues, reply "NO_ISSUES".
4. If issues found, reply with a markdown summary starting with "ISSUES_FOUND".

Reply concise.
"""
    return run_ollama(model, prompt)

def main():
    parser = argparse.ArgumentParser(description="Autonomous Local Agent")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help="Ollama model name")
    args = parser.parse_args()
    
    model_name = args.model
    print(f"🚀 Starting Autonomous Agent Patrol using model: {model_name}")
    
    files = tool_list_files()
    print(f"Found {len(files)} files to audit.")
    
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write(f"# 🛡️ Autonomous Evaluation Report\nDate: {datetime.datetime.now()}\nModel: {model_name}\n\n")

    for i, f in enumerate(files):
        print(f"[{i+1}/{len(files)}] Auditing {f}...")
        result = agent_step(model_name, f)
        
        if "ISSUES_FOUND" in result or "Syntax Error" in result:
            print(f"  ⚠️ Issues detected.")
            with open(REPORT_FILE, 'a', encoding='utf-8') as rep:
                rep.write(f"## 📄 {f}\n{result}\n\n---\n\n")
        else:
            print(f"  ✅ Clean.")

    print(f"\n✅ Patrol Complete. Report saved to {REPORT_FILE}")

if __name__ == "__main__":
    main()
