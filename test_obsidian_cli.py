import sys
from lib.lib_obsidian_cli import run_obsidian_cli

def test_cli():
    try:
        print("Testing Obsidian CLI...")
        result = run_obsidian_cli(["--help"])
        print(f"Return code: {result.returncode}")
        print(f"Stdout:\n{result.stdout}")
        print(f"Stderr:\n{result.stderr}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_cli()
