# -*- coding: utf-8 -*-
"""
Antigravity Agent Core Module (Refactored)
Legacy entry point and tool definitions.
New modules: lib_shared, tool_crypto, engine_plan_execute, tool_web, tool_obsidian
"""

import sys
import os


# Re-exports for Backward Compatibility
from tools.tool_crypto import (
    tool_analyze_crypto, tool_scan_crypto_market, tool_audit_predictions,
    SCAN_SYMBOLS
)
from engine_plan_execute import (
    run_plan_and_execute, extract_patterns, generate_performance_dashboard,
    run_react_loop, log_execution, generate_plan, execute_plan, critique_results
)
from tools.tool_web import (
    tool_web_search, tool_get_youtube_transcript, tool_fetch_url, tool_fetch_daily_news
)
from tools.tool_obsidian import (
    tool_vault_search, tool_create_note, tool_read_note, tool_update_note, tool_merge_weekly_reports
)
from tools.tool_briefing_manager import tool_generate_briefing, add_provider
from tools.tool_gmail_manager import get_gmail_summary as tool_gmail_check


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
