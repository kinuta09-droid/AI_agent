
import discord
import os
import asyncio
from dotenv import load_dotenv
import sys

# Force unbuffered output
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv(r'C:\AI_agent\.env')
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

if not TOKEN:
    print("Error: Token not found.")
    sys.exit(1)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    try:
        app_info = await client.application_info()
        print(f"Logged in as: {client.user}")
        print(f"App ID: {app_info.id}")
        
        # Administrator permissions (8)
        permissions = discord.Permissions(8)
        
        # Generate URL
        url = discord.utils.oauth_url(
            app_info.id, 
            permissions=permissions, 
            scopes=("bot", "applications.commands")
        )
        
        print(f"URL: {url}")
        
        # Write to file
        with open(r'C:\AI_agent\url.txt', 'w', encoding='utf-8') as f:
            f.write(url)
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await client.close()

client.run(TOKEN)
