"""Application entry point for the Telegram channel transfer bot."""
import asyncio
import logging
import os
import signal
from pyrogram import Client
import config
from database import Database
from transfer_engine import TransferEngine
from bot_handlers import BotHandlers

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger("main")


async def main():
    config.validate()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    db = Database()
    await db.connect()
    saved_interval = await db.get_setting("transfer_interval")
    if saved_interval:
        config.TRANSFER_INTERVAL = float(saved_interval)

    user_client = Client(name="user", api_id=config.API_ID, api_hash=config.API_HASH,
                         session_string=config.SESSION_STRING, no_updates=True,
                         sleep_threshold=config.FLOOD_SLEEP_THRESHOLD)
    bot_client = Client(name="bot", api_id=config.API_ID, api_hash=config.API_HASH,
                        bot_token=config.BOT_TOKEN, sleep_threshold=config.FLOOD_SLEEP_THRESHOLD)
    engine = TransferEngine(user_client, db)
    handlers = BotHandlers(bot_client, user_client, db, engine)
    handlers.register_handlers()
    stop_event = asyncio.Event()

    def request_shutdown():
        logger.info("Shutdown signal received")
        engine.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            pass

    try:
        logger.info("Starting Telegram clients…")
        async with user_client, bot_client:
            me_user = await user_client.get_me()
            me_bot = await bot_client.get_me()
            logger.info("User client: %s (%s)", me_user.first_name, me_user.id)
            logger.info("Bot client: @%s", me_bot.username)
            logger.info("Ready — send /start to the bot")
            await stop_event.wait()
    finally:
        if engine.is_running():
            engine.stop()
            try:
                await asyncio.wait_for(engine.wait_until_done(), timeout=20)
            except Exception:
                logger.exception("Timed out waiting for transfer task shutdown")
        await db.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
