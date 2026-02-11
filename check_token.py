
import sys
import os

# Force unbuffered output
sys.stdout.reconfigure(encoding='utf-8')

print("Starting check_token.py...")

try:
    from dotenv import load_dotenv
    print("Module 'dotenv' imported successfully.")
except ImportError as e:
    print(f"Error: Could not import dotenv: {e}")
    sys.exit(0)  # Exit 0 so we see the output

env_path = r'C:\AI_agent\.env'
print(f"Checking for .env file at: {env_path}")

if not os.path.exists(env_path):
    print(f"Error: .env file DOES NOT EXIST at {env_path}")
    # List directory to debug
    dir_path = os.path.dirname(env_path)
    if os.path.exists(dir_path):
        print(f"Listing {dir_path}:")
        for f in os.listdir(dir_path):
            print(f" - {f}")
    else:
        print(f"Directory {dir_path} does not exist either.")
    sys.exit(0)

print(".env file exists. Loading...")
load_dotenv(env_path)
token = os.getenv("DISCORD_BOT_TOKEN")

if not token:
    print("Error: DISCORD_BOT_TOKEN is missing or empty in environment variables!")
else:
    print(f"Success: DISCORD_BOT_TOKEN found (length: {len(token)})")

print("Finished check_token.py")
