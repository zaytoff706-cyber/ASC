import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
STAFF_GUILD_ID = int(os.getenv("STAFF_GUILD_ID", "0"))
STAFF_CHANNEL_ID = int(os.getenv("STAFF_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0"))

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "300"))
PORT = int(os.getenv("PORT", "8080"))