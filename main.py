"""
main.py — entry point
Starts the Pyrogram user client and bot client concurrently.
"""

import asyncio
import logging
import os
import signal

from pyrogram import Client

import config
from database import Database
from transfer_engine import TransferEngine
from bot_handlers import BotHandlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger("main")


async def main():
    # Validate env
    config.validate()

    # Ensure data dirs
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    # Database
    db = Database()
    await db.connect()

    # Restore interval from settings
    saved_interval = await db.get_setting("transfer_interval")
    if saved_interval:
        config.TRANSFER_INTERVAL = float(saved_interval)

    # User client (accesses private channels)
    user_client = Client(
        name="user",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.SESSION_STRING,
        no_updates=True,   # user client doesn't need incoming updates
    )

    # Bot client (receives commands)
    bot_client = Client(
        name="bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
    )

    engine = TransferEngine(user_client, db)
    handlers = BotHandlers(bot_client, user_client, db, engine)
    handlers.register_handlers()

    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        engine.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    logger.info("Starting clients…")
    async with user_client, bot_client:
        me_user = await user_client.get_me()
        me_bot = await bot_client.get_me()
        logger.info(f"User client: {me_user.first_name} ({me_user.id})")
        logger.info(f"Bot client:  @{me_bot.username}")
        logger.info("✅ Ready — send /start to the bot.")

        await stop_event.wait()

    await db.close()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
