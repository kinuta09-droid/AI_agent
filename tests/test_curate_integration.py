import sys
import os
import shutil
sys.path.insert(0, r'C:\AI_agent')

from tool_curator import search_and_save_topic

def test_curate_integration():
    print("=== TEST START: Curate Integration ===")
    
    # Use a dummy topic that should link to MOC_AI
    topic = "Test_Agent_Integration"
    
    # Note: search_and_save_topic actually performs web search.
    # To avoid API cost and wait time, we should ideally mock tavily_client and gemini.
    # But for now, we'll rely on the real execution being reasonably fast, OR we can mock it here.
    
    # --- MOCKING ---
    # Since we can't easily mock imported modules without unittest.mock patch, 
    # and we don't want to change the source code just for testing,
    # let's try a real run but with a very specific query that might be low cost?
    # Actually, a real run is safer to prove integration worked.
    
    print(f"Running search_and_save_topic for '{topic}'...")
    try:
        result = search_and_save_topic(topic, search_query="Agentic workflow design patterns")
        print(f"Result: {result}")
        
        # Check the file
        # Result string is like "Success: Created [[Knowledge_Base/yyyy-mm-dd_Test_Agent_Integration.md]]"
        match = result.split("[[")[1].split("]]")[0]
        # match is like "Knowledge_Base/2026-02-09_Test_Agent_Integration.md"
        
        filepath = os.path.join(r'D:\マイドライブ\Obsidian\30_Main', match)
        
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            print("\n--- Content Header ---")
            print(content[:500])
            print("----------------------\n")
            
            # Check for MOC Link
            if 'up: "[[MOC_AI]]"' in content or 'up: "[[MOC_PC]]"' in content: # MOC_PCも可能性あり
                print("✅ PASS: MOC Linked (AI/PC)")
            elif 'up: "[[00_Inbox]]"' in content:
                print("❌ FAIL: Defaulted to [[00_Inbox]]")
            else:
                # Depends on Gemini classification
                print(f"⚠️ Check manually: 'up' property value")

            # Check for Template Integration
            # The tool_curator generates ## References. It should be moved to ### 参照元
            if "### 参照元" in content and "Reference" not in content.split("### 参照元")[0]:
                 # Ideally "## References" should be gone from body
                 print("✅ PASS: References integrated")
            else:
                 print("❌ FAIL: References not integrated or original header remains")
            
            # Cleanup
            try:
                os.remove(filepath)
                print("Deleted test file.")
            except: pass
            
        else:
            print(f"❌ FAIL: File not found at {filepath}")
            
    except Exception as e:
        print(f"❌ FAIL: Execution Error: {e}")

if __name__ == "__main__":
    test_curate_integration()
