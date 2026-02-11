
import os
from dotenv import load_dotenv
import sys

# Force unbuffered output
sys.stdout.reconfigure(encoding='utf-8')

env_path = r'C:\AI_agent\.env'
load_dotenv(env_path)

app_id = os.getenv("DISCORD_APP_ID")
if app_id:
    print(f"DISCORD_APP_ID: {app_id}")
else:
    print("DISCORD_APP_ID is NOT set.")
