import os
from dotenv import load_dotenv

load_dotenv()

BALE_TOKEN = os.getenv("BALE_TOKEN", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bot.db")
