import sys
import os

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

print(f"Testing imports from: {parent_dir}")

try:
    print("Importing tool_web_search from agent_hello...")
    from agent_hello import tool_web_search
    print("  -> OK")
    
    print("Importing tool_create_note from agent_hello...")
    from agent_hello import tool_create_note
    print("  -> OK")
    
    print("Importing tool_analyze_crypto from agent_hello...")
    from agent_hello import tool_analyze_crypto
    print("  -> OK")
    
    print("SUCCESS: All modularized tools re-exported correctly.")

except Exception as e:
    print(f"FAILURE: {e}")
    import traceback
    traceback.print_exc()
