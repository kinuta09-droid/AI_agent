import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_hello import tool_create_note

# Mock dependencies if needed, or just rely on agent_hello being runnable
# We assume environment variables are set or defaults are handled gracefully in agent_hello imports

print("Testing tool_create_note with '30_Main/TestNote.md'...")
try:
    result = tool_create_note("30_Main/TestNote.md", "Test Content")
    print(f"Result: {result}")
    
    # Verify file existence
    expected_path = r"D:\マイドライブ\Obsidian\30_Main\TestNote.md"
    if os.path.exists(expected_path):
        print("✅ File created at correct path.")
    else:
        print(f"❌ File NOT found at {expected_path}")
        
    # Check for double folder
    double_path = r"D:\マイドライブ\Obsidian\30_Main\30_Main\TestNote.md"
    if os.path.exists(double_path):
        print(f"❌ Double folder detected at {double_path}")
    else:
        print("✅ Double folder NOT created.")

except Exception as e:
    print(f"Exception: {e}")
