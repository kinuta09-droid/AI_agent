import sys
import os
import shutil
import re
sys.path.insert(0, r'C:\AI_agent')

from agent_hello import tool_create_note, find_file_in_vault

def test_template_integration():
    print("=== TEST START: Template Integration ===")
    
    filename = "Test_Integration.md"
    content = """This is the main content of the note.
It has some important information.
> Source: [[Lineage_Parent_Note]]

## References
- https://example.com/ref1
- https://example.com/ref2

## Sources
- [[Source_Note_A]]
- [[Source_Note_B]]

This part should remain in the content.
"""
    summary = "Testing template integration"

    # 1. Create Note
    print(f"\nCreating note: {filename}")
    result = tool_create_note(filename, content, summary)
    print(f"Result: {result}")
    
    # 2. Read File
    path = find_file_in_vault(filename)
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            file_content = f.read()
        
        print("\n--- Generated File Content ---")
        print(file_content)
        print("------------------------------\n")

        # 3. Verification
        errors = []
        
        # Check References in Template Section
        if "### 参照元\n- https://example.com/ref1" in file_content:
            print("✅ PASS: References moved to Template section")
        else:
            errors.append("❌ FAIL: References not found in Template section")

        # Check Main Content Preservation (Crucial for regex bug fix)
        if "This is the main content of the note." in file_content:
             print("✅ PASS: Main content preserved")
        else:
             errors.append("❌ FAIL: Main content was swallowed by regex!")

        # Check Sources in Template Section (Includes Lineage)
        if "### 関連ノート\n- [[Lineage_Parent_Note]]\n- [[Source_Note_A]]" in file_content or "- [[Lineage_Parent_Note]]" in file_content:
            print("✅ PASS: Sources (incl. Lineage) moved to Template section")
        else:
            errors.append("❌ FAIL: Sources/Lineage not found in Template section")

        # Check Lineage Removal from Body
        # We need to ensure "> Source: ..." is gone. 
        # But be careful not to match the template if we formatted it differently, 
        # though template uses "- [[...]]".
        if "> Source: [[Lineage_Parent_Note]]" in file_content:
             errors.append("❌ FAIL: Lineage link '> Source: ...' still exists in content")
        else:
             print("✅ PASS: Lineage link removed from content")


        # Check Removal from Body
        # Original headings should be gone from the body text part
        # Note: "This part should remain in the content" is after sources.
        # But our regex might remove everything till next header or EOF.
        # Let's check if the raw headings exist in the generated file OUTSIDE of the template sections.
        # Simple check: count occurrences. "### 参照元" is template, "## References" is content.
        if "## References" in file_content:
             errors.append("❌ FAIL: '## References' header still exists in content")
        else:
             print("✅ PASS: '## References' removed from content")

        if "## Sources" in file_content:
             errors.append("❌ FAIL: '## Sources' header still exists in content")
        else:
             print("✅ PASS: '## Sources' removed from content")
        
        if "This part should remain in the content." in file_content:
             print("✅ PASS: Footer content preserved")
        else:
             errors.append("❌ FAIL: Footer content was accidentally removed")

        if not errors:
            print("\n🎉 ALL TESTS PASSED!")
            # Cleanup
            try:
                os.remove(path)
                print("Deleted test file.")
            except: pass
        else:
            print("\n💀 ERRORS FOUND:")
            for e in errors:
                print(e)

    else:
         print("❌ FAIL: File not found.")

if __name__ == "__main__":
    test_template_integration()
