
import os
from dotenv import load_dotenv
import sys

# Force unbuffered output
sys.stdout.reconfigure(encoding='utf-8')

env_path = r'C:\AI_agent\.env'
load_dotenv(env_path)

guild_id = os.getenv("DISCORD_GUILD_ID")
if guild_id:
    print(f"DISCORD_GUILD_ID is set (Length: {len(guild_id)})")
else:
    print("DISCORD_GUILD_ID is NOT set. Bot will attempt Global Sync.")
