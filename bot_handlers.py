"""
Bot Handlers - Complete Version
Pyrogram User Client + Bot Client
"""

import logging
import re
from typing import Optional, Dict

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import PeerIdInvalid, UsernameNotOccupied

import config
from database import Database
from transfer_engine import TransferEngine

logger = logging.getLogger(__name__)


HELP_TEXT = """
📡 Channel Transfer Bot

SETUP
/setsource - Set source channel
/setdest   - Set destination channel
/status    - Show current status

OPERATIONS
/scan      - Scan source channel
/transfer  - Start transfer
/pause     - Pause transfer
/resume    - Resume transfer
/stop      - Stop transfer
/retry     - Retry failed messages

CONFIGURATION
/setinterval 10
/setinterval 15
/newjob
/help
"""


class BotHandlers:

    def __init__(
        self,
        bot: Client,
        user_client: Client,
        db: Database,
        engine: TransferEngine,
    ):
        self.bot = bot
        self.uc = user_client
        self.db = db
        self.engine = engine

        self._user_state: Dict[int, Optional[str]] = {}
        self._pending_source: Dict[int, Dict] = {}
        self._pending_dest: Dict[int, Dict] = {}
        self._notify_chat: Dict[int, int] = {}

    def _is_channel(self, chat) -> bool:
        if not chat:
            return False

        chat_type = str(getattr(chat, "type", "")).lower()

        return chat_type == "channel" or chat_type.endswith(".channel")

    def _is_authorized(self, user_id: int) -> bool:
        return user_id == config.AUTHORIZED_USER_ID

    def register_handlers(self):

        bot = self.bot
        auth = filters.user(config.AUTHORIZED_USER_ID)

        # START
        @bot.on_message(filters.command("start") & auth)
        async def cmd_start(_, msg: Message):

            await msg.reply(
                "👋 **Channel Transfer Bot is running.**\n\n"
                "Use /setsource and /setdest to configure channels.\n"
                "Then use /scan and /transfer.\n\n"
                "/help for commands."
            )

        # HELP
        @bot.on_message(filters.command("help") & auth)
        async def cmd_help(_, msg: Message):

            await msg.reply(HELP_TEXT)

        # SET SOURCE
        @bot.on_message(filters.command("setsource") & auth)
        async def cmd_setsource(_, msg: Message):

            self._user_state[msg.from_user.id] = "awaiting_source"

            await msg.reply(
                "📥 **Set Source Channel**\n\n"
                "Send any one of these:\n"
                "• Forward a channel post\n"
                "• @username\n"
                "• Numeric channel ID\n"
                "• Telegram channel link"
            )

        # SET DESTINATION
        @bot.on_message(filters.command("setdest") & auth)
        async def cmd_setdest(_, msg: Message):

            self._user_state[msg.from_user.id] = "awaiting_dest"

            await msg.reply(
                "📤 **Set Destination Channel**\n\n"
                "Send any one of these:\n"
                "• Forward a channel post\n"
                "• @username\n"
                "• Numeric channel ID\n"
                "• Telegram channel link"
            )

        # CHANNEL INPUT
        @bot.on_message(auth & ~filters.command([]))
        async def handle_input(_, msg: Message):

            if not msg.from_user:
                return

            uid = msg.from_user.id
            state = self._user_state.get(uid)

            if state not in ("awaiting_source", "awaiting_dest"):
                return

            channel_info = await self._resolve_channel(msg)

            if not channel_info:

                await msg.reply(
                    "❌ Couldn't identify this channel.\n\n"
                    "Check that your USER account is a member "
                    "of the channel.\n\n"
                    "Try its @username, channel ID, or forward a post."
                )
                return

            if state == "awaiting_source":

                self._pending_source[uid] = channel_info
                self._user_state[uid] = None

                await msg.reply(
                    f"✅ **Source Channel Set**\n\n"
                    f"Name: {channel_info['title']}\n"
                    f"ID: `{channel_info['id']}`\n\n"
                    "Now use /setdest."
                )

            else:

                self._pending_dest[uid] = channel_info
                self._user_state[uid] = None

                src = self._pending_source.get(uid)

                if src:

                    if src["id"] == channel_info["id"]:
                        await msg.reply(
                            "❌ Source and destination cannot be the same channel."
                        )
                        self._pending_dest.pop(uid, None)
                        return

                    await msg.reply(
                        f"✅ **Destination Channel Set**\n\n"
                        f"Name: {channel_info['title']}\n"
                        f"ID: `{channel_info['id']}`\n\n"
                        f"Source: {src['title']}\n"
                        f"Destination: {channel_info['title']}\n\n"
                        "Use /scan to scan the source."
                    )

                else:

                    await msg.reply(
                        "✅ Destination saved.\n"
                        "Now use /setsource."
                    )

        # SCAN
        @bot.on_message(filters.command("scan") & auth)
        async def cmd_scan(_, msg: Message):

            uid = msg.from_user.id

            src = self._pending_source.get(uid)
            dst = self._pending_dest.get(uid)

            if not src or not dst:

                await msg.reply(
                    "❌ Configure both channels first.\n"
                    "/setsource\n"
                    "/setdest"
                )
                return

            if self.engine.is_running():

                await msg.reply(
                    "⚠️ Another job is already running. Use /stop first."
                )
                return

            job_id = await self.db.create_job(
                src["id"],
                src["title"],
                dst["id"],
                dst["title"]
            )

            await self.db.set_setting(
                "current_job_id",
                str(job_id)
            )

            self._notify_chat[uid] = msg.chat.id

            self.engine.set_progress_callback(
                lambda jid, text: self._send_progress(uid, jid, text)
            )

            ok = await self.engine.start_scan(job_id)

            if ok:

                await msg.reply(
                    f"🔍 **Scanning Started**\n"
                    f"Job: #{job_id}\n"
                    f"Source: {src['title']}\n"
                    f"Destination: {dst['title']}"
                )

            else:

                await msg.reply("❌ Failed to start scan.")

        # TRANSFER
        @bot.on_message(filters.command("transfer") & auth)
        async def cmd_transfer(_, msg: Message):

            uid = msg.from_user.id

            job_id = await self._get_current_job_id()

            if not job_id:

                await msg.reply("❌ No job found. Use /scan first.")
                return

            job = await self.db.get_job(job_id)

            if not job:

                await msg.reply("❌ Job not found.")
                return

            if job["status"] not in (
                "scan_complete",
                "paused",
                "failed"
            ):

                await msg.reply(
                    f"⚠️ Job status: `{job['status']}`"
                )
                return

            if self.engine.is_running():

                await msg.reply("⚠️ Engine is already running.")
                return

            self._notify_chat[uid] = msg.chat.id

            self.engine.set_progress_callback(
                lambda jid, text: self._send_progress(uid, jid, text)
            )

            ok = await self.engine.start_transfer(job_id)

            if ok:

                counts = await self.db.get_message_counts(job_id)

                await msg.reply(
                    f"🚀 **Transfer Started**\n\n"
                    f"Job: #{job_id}\n"
                    f"Pending: {counts.get('pending', 0)}\n"
                    f"Interval: {config.TRANSFER_INTERVAL}s"
                )

            else:

                await msg.reply("❌ Failed to start transfer.")

        # PAUSE
        @bot.on_message(filters.command("pause") & auth)
        async def cmd_pause(_, msg: Message):

            if not self.engine.is_running():

                await msg.reply("Nothing is running.")
                return

            self.engine.pause()

            await msg.reply("⏸ Transfer paused.")

        # RESUME
        @bot.on_message(filters.command("resume") & auth)
        async def cmd_resume(_, msg: Message):

            uid = msg.from_user.id

            if self.engine.is_running():

                self.engine.resume()

                await msg.reply("▶️ Transfer resumed.")
                return

            job_id = await self._get_current_job_id()

            if not job_id:

                await msg.reply("❌ No job to resume.")
                return

            job = await self.db.get_job(job_id)

            if job and job["status"] == "paused":

                self._notify_chat[uid] = msg.chat.id

                self.engine.set_progress_callback(
                    lambda jid, text: self._send_progress(uid, jid, text)
                )

                ok = await self.engine.start_transfer(job_id)

                if ok:
                    await msg.reply("▶️ Transfer resumed.")
                else:
                    await msg.reply("❌ Could not resume.")

            else:

                await msg.reply(
                    f"⚠️ Job status: {job['status'] if job else 'unknown'}"
                )

        # STOP
        @bot.on_message(filters.command("stop") & auth)
        async def cmd_stop(_, msg: Message):

            self.engine.stop()

            await msg.reply(
                "🛑 Stop signal sent.\n"
                "Progress is saved."
            )

        # RETRY
        @bot.on_message(filters.command("retry") & auth)
        async def cmd_retry(_, msg: Message):

            uid = msg.from_user.id

            job_id = await self._get_current_job_id()

            if not job_id:

                await msg.reply("❌ No job found.")
                return

            if self.engine.is_running():

                await msg.reply("⚠️ Stop the current job first.")
                return

            failed = await self.db.get_failed_messages(
                job_id,
                max_retries=config.MAX_RETRIES
            )

            if not failed:

                await msg.reply("✅ No retryable failed messages.")
                return

            for row in failed:

                await self.db.db._conn.execute(
                    """
                    UPDATE messages
                    SET status = 'pending', error_text = NULL
                    WHERE job_id = ? AND source_msg_id = ?
                    """,
                    (job_id, row["source_msg_id"])
                )

            await self.db.db._conn.commit()

            self._notify_chat[uid] = msg.chat.id

            self.engine.set_progress_callback(
                lambda jid, text: self._send_progress(uid, jid, text)
            )

            ok = await self.engine.start_transfer(job_id)

            if ok:

                await msg.reply(
                    f"🔄 Retrying {len(failed)} messages."
                )

            else:

                await msg.reply("❌ Couldn't start retry.")

        # STATUS
        @bot.on_message(filters.command("status") & auth)
        async def cmd_status(_, msg: Message):

            uid = msg.from_user.id

            src = self._pending_source.get(uid)
            dst = self._pending_dest.get(uid)

            job_id = await self._get_current_job_id()

            lines = ["📊 **Transfer Status**\n"]

            if src:
                lines.append(
                    f"📥 Source: {src['title']} (`{src['id']}`)"
                )
            else:
                lines.append("📥 Source: Not set")

            if dst:
                lines.append(
                    f"📤 Destination: {dst['title']} (`{dst['id']}`)"
                )
            else:
                lines.append("📤 Destination: Not set")

            if job_id:

                job = await self.db.get_job(job_id)

                counts = await self.db.get_message_counts(job_id)

                if job:

                    lines.extend([
                        f"\nJob: #{job_id}",
                        f"Status: `{job['status']}`",
                        f"Total: {counts.get('total', 0)}",
                        f"Transferred: {counts.get('transferred', 0)}",
                        f"Pending: {counts.get('pending', 0)}",
                        f"Failed: {counts.get('failed', 0)}",
                        f"Cached: {counts.get('cached', 0)}"
                    ])

                    total = counts.get("total", 0)

                    if total:
                        percentage = (
                            counts.get("transferred", 0) / total
                        ) * 100

                        lines.append(
                            f"Progress: {percentage:.1f}%"
                        )

            else:

                lines.append("\nNo active job.")

            lines.append(
                f"\nInterval: {config.TRANSFER_INTERVAL}s"
            )

            lines.append(
                f"Engine: {'🟢 Running' if self.engine.is_running() else '🔴 Idle'}"
            )

            await msg.reply("\n".join(lines))

        # INTERVAL
        @bot.on_message(filters.command("setinterval") & auth)
        async def cmd_setinterval(_, msg: Message):

            parts = (msg.text or "").split()

            if len(parts) < 2 or parts[1] not in ("10", "15"):

                await msg.reply(
                    "Usage:\n"
                    "/setinterval 10\n"
                    "/setinterval 15\n\n"
                    f"Current: {config.TRANSFER_INTERVAL}s"
                )
                return

            config.TRANSFER_INTERVAL = float(parts[1])

            await self.db.set_setting(
                "transfer_interval",
                parts[1]
            )

            await msg.reply(
                f"✅ Interval set to {parts[1]} seconds."
            )

        # NEW JOB
        @bot.on_message(filters.command("newjob") & auth)
        async def cmd_newjob(_, msg: Message):

            uid = msg.from_user.id

            if self.engine.is_running():

                await msg.reply(
                    "⚠️ Stop the current job first."
                )
                return

            self._pending_source.pop(uid, None)
            self._pending_dest.pop(uid, None)

            await self.db.set_setting(
                "current_job_id",
                ""
            )

            await msg.reply(
                "🆕 New job ready.\n"
                "Use /setsource and /setdest."
            )

        # UNAUTHORIZED USERS
        @bot.on_message(~auth)
        async def unauthorized(_, msg: Message):

            await msg.reply("🚫 Unauthorized.")

    # =====================================================
    # CHANNEL RESOLVER
    # =====================================================

    async def _resolve_channel(
        self,
        msg: Message
    ) -> Optional[Dict]:

        """
        Resolve channel from:
        - Forwarded channel post
        - Public username
        - Private numeric ID
        - t.me/c/PRIVATE_ID/MESSAGE_ID
        - t.me/USERNAME/MESSAGE_ID

        Uses the logged-in USER CLIENT.
        """

        # 1. Forwarded channel post
        forwarded_chat = getattr(
            msg,
            "forward_from_chat",
            None
        )

        if self._is_channel(forwarded_chat):

            return {
                "id": forwarded_chat.id,
                "title": forwarded_chat.title or str(forwarded_chat.id)
            }

        text = (msg.text or "").strip()

        if not text:
            return None

        # Remove query parameters and trailing slash
        clean_text = text.split("?", 1)[0].rstrip("/")

        # 2. Parse Telegram channel links
        match = re.fullmatch(
            r"(?:https?://)?(?:www\.)?t\.me/(c/)?([A-Za-z0-9_]+)(?:/\d+)?",
            clean_text,
            flags=re.IGNORECASE
        )

        if match:

            is_private = bool(match.group(1))
            value = match.group(2)

            if is_private and value.isdigit():

                text = "-100" + value

            else:

                text = "@" + value

        # 3. Numeric ID
        if re.fullmatch(r"-?\d+", text):

            chat_id = int(text)

            # Accept a positive -100 channel ID too
            if text.startswith("100") and len(text) >= 12:
                chat_id = -int(text)

            try:

                chat = await self.uc.get_chat(chat_id)

                if self._is_channel(chat):

                    return {
                        "id": chat.id,
                        "title": chat.title or str(chat.id)
                    }

            except (PeerIdInvalid, UsernameNotOccupied):
                pass

            except Exception as e:
                logger.warning(
                    "Direct ID lookup failed: %s",
                    e
                )

            # Search channels in the user's existing dialogs
            try:

                async for dialog in self.uc.get_dialogs():

                    dialog_chat = dialog.chat

                    if dialog_chat.id == chat_id:

                        if self._is_channel(dialog_chat):

                            return {
                                "id": dialog_chat.id,
                                "title": dialog_chat.title or str(chat_id)
                            }

                        return None

            except Exception as e:

                logger.warning(
                    "Dialog search failed: %s",
                    e
                )

            logger.warning(
                "Channel ID not accessible: %s",
                chat_id
            )

            return None

        # 4. Public username
        username = text.lstrip("@").strip()

        if not username:
            return None

        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return None

        try:

            chat = await self.uc.get_chat(username)

            if self._is_channel(chat):

                return {
                    "id": chat.id,
                    "title": chat.title or username
                }

        except Exception as e:

            logger.warning(
                "Username resolution failed for %s: %s",
                username,
                e
            )

        return None

    # =====================================================
    # DATABASE HELPERS
    # =====================================================

    async def _get_current_job_id(self) -> Optional[int]:

        val = await self.db.get_setting("current_job_id")

        if val and str(val).isdigit():

            return int(val)

        job = await self.db.get_latest_job()

        return job["id"] if job else None

    # =====================================================
    # PROGRESS NOTIFICATIONS
    # =====================================================

    async def _send_progress(
        self,
        uid: int,
        job_id: int,
       
