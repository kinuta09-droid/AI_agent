from youtube_transcript_api import YouTubeTranscriptApi
import traceback
import sys

try:
    print("Testing instance-based usage with traceback...")
    yt = YouTubeTranscriptApi()
    
    # Rick Roll - Definitely has captions
    t_list = yt.list("dQw4w9WgXcQ")
    print(f"List retrieved. Type: {type(t_list)}")
    
    print("Calling find_transcript(['en'])...")
    try:
        t = t_list.find_transcript(['en']) 
        print(f"Transcript found: {t}")
        data = t.fetch()
        print(f"First item: {data[0]}")
    except Exception:
        print("EXCEPTION IN FIND_TRANSCRIPT:")
        traceback.print_exc()

except Exception as e:
    print(f"Outer Error: {e}")
    traceback.print_exc()
