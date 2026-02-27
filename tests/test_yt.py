# Set up path to import from parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_hello import tool_get_youtube_transcript

# Test with a known video (e.g. Python official channel or similar stable video)
# Using a PyCon talk or similar tech talk is usually safe.
# Example: "Python 3.13: The Fast and the Furious" - https://www.youtube.com/watch?v=s5R-6QjkqG4 (just an example ID, let's use a real one)
# Let's use Rick Roll which definitely has captions: https://www.youtube.com/watch?v=dQw4w9WgXcQ

test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

print(f"Testing transcript fetch for: {test_url}")

# Monkey patch for the test script if it imports the function which is now fixed
# But for clarity, let's just use the updated function from agent_hello
from agent_hello import tool_get_youtube_transcript
result = tool_get_youtube_transcript(test_url)

print("\n--- Result Sample ---")
print(result[:500])
print("...")
print("---------------------")

if "Error" in result:
    print("❌ Test Failed")
    sys.exit(1)
if "【YouTube動画内容（文字起こし）】" in result:
    print("✅ Test Passed")
    sys.exit(0)
    
print("❓ Unexpected Result")
sys.exit(1)
