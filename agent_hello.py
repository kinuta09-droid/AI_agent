# -*- coding: utf-8 -*-
"""
Antigravity Agent Core Module (Refactored)
Legacy entry point and tool definitions.
New modules: lib_shared, tool_crypto, engine_plan_execute, tool_web, tool_obsidian
"""

import sys
import os


# Re-exports for Backward Compatibility (実使用のあるものだけ残す — 2026-08-04)
from tools.tool_crypto import tool_analyze_crypto, tool_scan_crypto_market
from engine_plan_execute import run_plan_and_execute
from tools.tool_web import tool_web_search, tool_fetch_daily_news
from tools.tool_obsidian import tool_create_note


# ---------------------------------------------------------
# Main (Legacy - redirects to new engine)
# ---------------------------------------------------------
def main():
    print("⚠️ agent_hello.py is deprecated as entry point. Use main.py instead.")
    # Fallback to interactive mode if run directly
    print("\n[Legacy Interactive Mode]")
    while True:
        try:
            q = input("Goal: ")
            if q.lower() in ("exit", "quit"): break
            result = run_plan_and_execute(q)
            print(result)
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    main()
