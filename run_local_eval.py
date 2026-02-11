import subprocess
import os

PROMPT = "Analyze the current directory systematically. 1. List all source files. 2. For each file, check for logical contradictions and potential bugs. 3. Output a summary list of issues in markdown format. Take your time and be thorough."
OUTPUT_FILE = "Self_Evaluation_Report.md"

def try_command(cmd_list):
    print(f"Trying command: {' '.join(cmd_list)}...")
    try:
        result = subprocess.run(
            cmd_list + [PROMPT],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            shell=True 
        )
        if result.returncode == 0 and result.stdout.strip():
            print("✅ Success!")
            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                f.write(result.stdout)
            return True
        else:
            print(f"❌ Failed (Code {result.returncode})")
            print(f"Stderr: {result.stderr[:200]}")
            return False
    except FileNotFoundError:
        print("❌ Command not found.")
        return False
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

# Attempt 1: As requested
if try_command(["claude", "--model", "qwen2.5-coder:7b"]):
    exit(0)

# Attempt 2: Direct Ollama usage
print("\nFalling back to direct 'ollama run'...")
if try_command(["ollama", "run", "qwen2.5-coder:7b"]):
    exit(0)

print("\nAll attempts failed.")
