import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID") or "0")
STAFF_GUILD_ID = int(os.getenv("STAFF_GUILD_ID") or "0")
STAFF_CHANNEL_ID = int(os.getenv("STAFF_CHANNEL_ID") or "0")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID") or "0")
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID") or "0")
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID") or "0")
OWNER_ID = int(os.getenv("OWNER_ID") or "0")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS") or "300")
PROOF_CHANNEL_ID = int(os.getenv("PROOF_CHANNEL_ID") or "0")
CODES_CHANNEL_ID = int(os.getenv("CODES_CHANNEL_ID") or "0")
PROOF_ROLE_ID = int(os.getenv("PROOF_ROLE_ID") or "0")
BYPASS_ROLE_ID = int(os.getenv("BYPASS_ROLE_ID") or "0")
PORT = int(os.getenv("PORT") or "10000")
