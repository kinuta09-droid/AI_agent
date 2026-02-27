import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_hello import tool_scan_crypto_market
import datetime

print(f"[{datetime.datetime.now()}] Testing tool_scan_crypto_market...")

try:
    result = tool_scan_crypto_market()
    print(f"✅ Success! Result snippet:\n{result[:300]}")
except Exception as e:
    print(f"❌ Failed with Exception: {e}")
    import traceback
    traceback.print_exc()
