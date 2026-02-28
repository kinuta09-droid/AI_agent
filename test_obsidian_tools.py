import sys
import time
import os

# Ensure the parent directory is in the path to import tools
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tools.tool_obsidian import tool_create_note, tool_update_note

def run_test():
    test_filename = "CLI_Integration_Test_Note"
    test_content = "This is a test note generated to verify the Obsidian v1.12 CLI integration."
    test_summary = "Testing Obsidian CLI..."
    
    print("1. Creating test note...")
    result_create = tool_create_note(test_filename, test_content, test_summary)
    print(f"Result: {result_create}")
    
    print("Waiting 5 seconds to let you observe the creation in Obsidian...")
    time.sleep(5)
    
    print("2. Updating test note...")
    append_content = "This line was appended to verify the update functionality via CLI."
    result_update = tool_update_note(f"{test_filename}.md", append_content)
    print(f"Result: {result_update}")
    
    print("Test complete. Please check Obsidian!")

if __name__ == "__main__":
    run_test()
