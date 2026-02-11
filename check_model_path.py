import os

target_path = r"C:\Users\amami\.lmstudio\models\unsloth\gpt-oss-20b-GGUF"

print(f"Checking Path: {target_path}")

try:
    if os.path.exists(target_path):
        print("--- Directory Inventory ---")
        for root, dirs, files in os.walk(target_path):
            level = root.replace(target_path, '').count(os.sep)
            indent = ' ' * 4 * (level)
            print(f"{indent}{os.path.basename(root)}/")
            subindent = ' ' * 4 * (level + 1)
            for f in files:
                print(f"{subindent}{f}")
    else:
        print("Path DOES NOT EXIST.")

except Exception as e:
    print(f"Error: {e}")
