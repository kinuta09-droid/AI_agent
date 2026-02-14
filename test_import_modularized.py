import sys
import os

# Add current directory to path if necessary
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

print(f"Testing imports from: {current_dir}")

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
