import youtube_transcript_api
from youtube_transcript_api import YouTubeTranscriptApi

cls = YouTubeTranscriptApi
print(f"Docs: {cls.__doc__}")
attrs = dir(cls)
print("--- Class Attributes ---")
for a in attrs:
    if not a.startswith('__'):
        print(a)
print("------------------------")


