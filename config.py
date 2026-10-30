import os
from dotenv import load_dotenv

load_dotenv()

API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
SESSION_STRING: str = os.getenv("SESSION_STRING", "")
AUTHORIZED_USER_ID: int = int(os.getenv("AUTHORIZED_USER_ID", "0"))

# Fast preset: Telegram may impose longer FloodWait delays dynamically.
TRANSFER_INTERVAL: float = float(os.getenv("TRANSFER_INTERVAL", "2"))
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "5"))
FLOOD_SLEEP_THRESHOLD: int = int(os.getenv("FLOOD_SLEEP_THRESHOLD", "120"))
BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "10"))
PER_MESSAGE_DELAY: float = float(os.getenv("PER_MESSAGE_DELAY", "0.15"))
SCAN_BATCH_SIZE: int = int(os.getenv("SCAN_BATCH_SIZE", "100"))

DATA_DIR: str = os.getenv("DATA_DIR", "/app/data")
DB_PATH: str = os.path.join(DATA_DIR, "transfer.db")
DOWNLOAD_DIR: str = os.path.join(DATA_DIR, "downloads")


def validate():
    missing = []
    if not API_ID: missing.append("API_ID")
    if not API_HASH: missing.append("API_HASH")
    if not BOT_TOKEN: missing.append("BOT_TOKEN")
    if not SESSION_STRING: missing.append("SESSION_STRING")
    if not AUTHORIZED_USER_ID: missing.append("AUTHORIZED_USER_ID")
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")
