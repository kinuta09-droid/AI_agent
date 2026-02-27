import sys
import os
import shutil
sys.path.insert(0, r'C:\AI_agent')

from agent_hello import tool_create_note, find_file_in_vault

def test_moc_linking():
    print("=== TEST START: MOC Auto-Linking ===")
    
    test_cases = [
        {
            "filename": "Test_AI_Report.md",
            "content": "Recent advancements in Large Language Models (LLMs) such as Gemini and GPT-4 have revolutionized natural language processing. Neural networks are becoming more efficient.",
            "summary": "AI progress report",
            "expected_moc": "MOC_AI"
        },
        {
            "filename": "Test_Election_Analysis.md",
            "content": "The recent election results show a shift in voter demographics. The ruling party lost significant seats in the diet.",
            "summary": "Election analysis",
            "expected_moc": "MOC_Politics"
        }
    ]

    for case in test_cases:
        print(f"\nRunning case: {case['filename']}")
        # 1. Create Note
        result = tool_create_note(case['filename'], case['content'], case['summary'])
        print(f"Creation Result: {result}")
        
        # 2. Read File to check 'up' property
        path = find_file_in_vault(case['filename'])
        if path and os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Check for up: "[[MOC_...]]"
            if f'up: "[[{case["expected_moc"]}]]"' in content:
                print(f"✅ PASS: Linked to [[{case['expected_moc']}]]")
            elif 'up: "[[00_Inbox]]"' in content:
                print(f"❌ FAIL: Defaulted to [[00_Inbox]] (Expected {case['expected_moc']})")
            else:
                print(f"❌ FAIL: Unexpected property value. Content preamble:\n{content[:100]}")
            
            # Cleanup
            try:
                os.remove(path)
                print("Deleted test file.")
            except: pass
        else:
             print("❌ FAIL: File not found.")

if __name__ == "__main__":
    test_moc_linking()
