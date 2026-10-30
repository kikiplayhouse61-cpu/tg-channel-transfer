"""
Bot Handlers
────────────
All command and message handlers for the control bot.

State machine per user:
  None → awaiting_source → awaiting_dest → configured
"""

import logging
from typing import Optional, Dict, Any

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import PeerIdInvalid, UsernameNotOccupied

import config
from database import Database
from transfer_engine import TransferEngine

logger = logging.getLogger(__name__)

HELP_TEXT = """
**📡 Channel Transfer Bot**

**Setup**
/setsource — Set source channel
/setdest   — Set destination channel
/status    — Show current config & progress

**Operations**
/scan      — Scan & cache source channel content
/transfer  — Start transferring to destination
/pause     — Pause the running transfer
/resume    — Resume a paused transfer
/stop      — Stop and save progress
/retry     — Retry failed messages

**Configuration**
/setinterval [10|15] — Set seconds between batches
/newjob    — Start a completely new job

**Other**
/help — Show this message

─────────────────────────────
To set a channel, either:
• Forward any post from that channel to this bot
• Send the channel username: @channelname
• Send the channel ID: -1001234567890
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

        # Per-user state: "awaiting_source" | "awaiting_dest" | None
        self._user_state: Dict[int, Optional[str]] = {}
        # Temporary channel selections before job is created
        self._pending_source: Dict[int, Dict] = {}
        self._pending_dest: Dict[int, Dict] = {}
        # Maps user_id → notify chat_id (where to send progress)
        self._notify_chat: Dict[int, int] = {}

    def _is_authorized(self, user_id: int) -> bool:
        return user_id == config.AUTHORIZED_USER_ID

    def register_handlers(self):
        bot = self.bot
        auth = filters.user(config.AUTHORIZED_USER_ID)

        @bot.on_message(filters.command("start") & auth)
        async def cmd_start(_, msg: Message):
            await msg.reply(
                "👋 **Channel Transfer Bot is running.**\n\n"
                "Use /setsource and /setdest to configure your channels,\n"
                "then /scan and /transfer to get started.\n\n"
                "/help for full command list."
            )

        @bot.on_message(filters.command("help") & auth)
        async def cmd_help(_, msg: Message):
            await msg.reply(HELP_TEXT)

        # ── Channel selection ─────────────────────────────────────────────────

         async def _resolve_channel(self, msg: Message) -> Optional[Dict]:
    import re
    text = (msg.text or "").strip()

    # t.me/c/CHANNELID/MSGID  (private channel link)
    m = re.search(r't\.me/c/(\d+)/\d+', text)
    if m:
        chat_id = int("-100" + m.group(1))
        try:
            chat = await self.uc.get_chat(chat_id)
            return {"id": chat.id, "title": chat.title or str(chat.id)}
        except Exception as e:
            logger.warning(f"private link error: {e}")
            return None

    # t.me/username/MSGID  (public channel link)
    m = re.search(r't\.me/([a-zA-Z][a-zA-Z0-9_]+)/\d+', text)
    if m:
        try:
            chat = await self.uc.get_chat(m.group(1))
            return {"id": chat.id, "title": chat.title or str(chat.id)}
        except Exception as e:
            logger.warning(f"public link error: {e}")
            return None

    # forwarded message (backup)
    if msg.forward_from_chat:
        chat = msg.forward_from_chat
        return {"id": chat.id, "title": chat.title or str(chat.id)}

    # @username या numeric ID
    if not text:
        return None
    try:
        if text.lstrip("-").isdigit():
            chat = await self.uc.get_chat(int(text))
            return {"id": chat.id, "title": chat.title or str(chat.id)}
        slug = text.lstrip("@").strip()
        if slug:
            chat = await self.uc.get_chat(slug)
            return {"id": chat.id, "title": chat.title or str(chat.id)}
    except Exception as e:
        logger.warning(f"_resolve_channel error: {e}")

    return None

        
        # ── Scan ──────────────────────────────────────────────────────────────

        @bot.on_message(filters.command("scan") & auth)
        async def cmd_scan(_, msg: Message):
            uid = msg.from_user.id
            src = self._pending_source.get(uid)
            dst = self._pending_dest.get(uid)

            if not src or not dst:
                await msg.reply(
                    "❌ Configure both channels first.\n"
                    "/setsource then /setdest"
                )
                return

            if self.engine.is_running():
                await msg.reply("⚠️ A job is already running. Use /stop first.")
                return

            job_id = await self.db.create_job(
                src["id"], src["title"], dst["id"], dst["title"]
            )
            await self.db.set_setting("current_job_id", str(job_id))
            self._notify_chat[uid] = msg.chat.id

            self.engine.set_progress_callback(
                lambda jid, text: self._send_progress(uid, jid, text)
            )

            ok = await self.engine.start_scan(job_id)
            if ok:
                await msg.reply(
                    f"🔍 Scanning started (Job #{job_id})\n"
                    f"**Source:** {src['title']}\n"
                    f"**Destination:** {dst['title']}\n\n"
                    "Progress updates will appear here."
                )
            else:
                await msg.reply("❌ Failed to start scan.")

        # ── Transfer ──────────────────────────────────────────────────────────

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

            if job["status"] not in ("scan_complete", "paused", "failed"):
                await msg.reply(
                    f"⚠️ Job status is `{job['status']}`.\n"
                    "It must be scan_complete, paused, or failed to start transfer."
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
                    f"🚀 Transfer started (Job #{job_id})\n"
                    f"📦 Messages to transfer: {counts.get('pending', 0)}\n"
                    f"⏱ Interval: {config.TRANSFER_INTERVAL}s"
                )
            else:
                await msg.reply("❌ Failed to start transfer.")

        # ── Pause / Resume / Stop ─────────────────────────────────────────────

        @bot.on_message(filters.command("pause") & auth)
        async def cmd_pause(_, msg: Message):
            if not self.engine.is_running():
                await msg.reply("Nothing is running.")
                return
            self.engine.pause()
            await msg.reply("⏸ Transfer paused. Use /resume to continue.")

        @bot.on_message(filters.command("resume") & auth)
        async def cmd_resume(_, msg: Message):
            uid = msg.from_user.id
            job_id = await self._get_current_job_id()

            if self.engine.is_running():
                self.engine.resume()
                await msg.reply("▶️ Transfer resumed.")
                return

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
                await msg.reply(f"⚠️ Job status: `{job['status'] if job else 'unknown'}`")

        @bot.on_message(filters.command("stop") & auth)
        async def cmd_stop(_, msg: Message):
            self.engine.stop()
            await msg.reply(
                "🛑 Stop signal sent. Progress is saved.\n"
                "Use /resume to continue later."
            )

        # ── Retry ─────────────────────────────────────────────────────────────

        @bot.on_message(filters.command("retry") & auth)
        async def cmd_retry(_, msg: Message):
            uid = msg.from_user.id
            job_id = await self._get_current_job_id()
            if not job_id:
                await msg.reply("❌ No job found.")
                return

            if self.engine.is_running():
                await msg.reply("⚠️ Engine is running. /stop first.")
                return

            failed = await self.db.get_failed_messages(
                job_id, max_retries=config.MAX_RETRIES
            )
            if not failed:
                await msg.reply("✅ No retryable failed messages.")
                return

            # Reset their status to pending
            for row in failed:
                await self.db.db._conn.execute(
                    "UPDATE messages SET status = 'pending', error_text = NULL WHERE job_id = ? AND source_msg_id = ?",
                    (job_id, row["source_msg_id"]),
                )
            await self.db.db._conn.commit()

            self._notify_chat[uid] = msg.chat.id
            self.engine.set_progress_callback(
                lambda jid, text: self._send_progress(uid, jid, text)
            )
            ok = await self.engine.start_transfer(job_id)
            if ok:
                await msg.reply(f"🔄 Retrying {len(failed)} failed messages.")
            else:
                await msg.reply("❌ Couldn't start retry.")

        # ── Status ────────────────────────────────────────────────────────────

        @bot.on_message(filters.command("status") & auth)
        async def cmd_status(_, msg: Message):
            uid = msg.from_user.id
            src = self._pending_source.get(uid)
            dst = self._pending_dest.get(uid)
            job_id = await self._get_current_job_id()

            lines = ["**📊 Transfer Status**\n"]

            if src:
                lines.append(f"📥 Source: **{src['title']}** (`{src['id']}`)")
            else:
                lines.append("📥 Source: _not set_")

            if dst:
                lines.append(f"📤 Destination: **{dst['title']}** (`{dst['id']}`)")
            else:
                lines.append("📤 Destination: _not set_")

            if job_id:
                job = await self.db.get_job(job_id)
                counts = await self.db.get_message_counts(job_id)
                if job:
                    status_emoji = {
                        "idle": "⏸", "scanning": "🔍", "scan_complete": "✅",
                        "transferring": "🚀", "paused": "⏸", "complete": "🎉",
                        "error": "❌",
                    }.get(job["status"], "❓")
                    lines += [
                        f"\n**Job #{job_id}**",
                        f"Status: {status_emoji} `{job['status']}`",
                        f"📦 Total: {counts.get('total', 0)}",
                        f"✅ Transferred: {counts.get('transferred', 0)}",
                        f"⏳ Pending: {counts.get('pending', 0)}",
                        f"❌ Failed: {counts.get('failed', 0)}",
                        f"💾 Cached: {counts.get('cached', 0)}",
                    ]
                    if counts.get("total", 0) > 0:
                        pct = counts.get("transferred", 0) / counts["total"] * 100
                        lines.append(f"Progress: {pct:.1f}%")
            else:
                lines.append("\n_No active job. Use /scan to start._")

            lines.append(f"\n⏱ Interval: {config.TRANSFER_INTERVAL}s")
            lines.append(
                f"Engine: {'🟢 running' if self.engine.is_running() else '🔴 idle'}"
            )

            await msg.reply("\n".join(lines))

        # ── Configuration ─────────────────────────────────────────────────────

        @bot.on_message(filters.command("setinterval") & auth)
        async def cmd_setinterval(_, msg: Message):
            parts = msg.text.split()
            if len(parts) < 2 or parts[1] not in ("10", "15"):
                await msg.reply(
                    "Usage: /setinterval 10  or  /setinterval 15\n"
                    f"Current: {config.TRANSFER_INTERVAL}s"
                )
                return
            config.TRANSFER_INTERVAL = float(parts[1])
            await self.db.set_setting("transfer_interval", parts[1])
            await msg.reply(f"✅ Interval set to {parts[1]}s")

        @bot.on_message(filters.command("newjob") & auth)
        async def cmd_newjob(_, msg: Message):
            uid = msg.from_user.id
            if self.engine.is_running():
                await msg.reply("⚠️ Stop the current job first with /stop")
                return
            self._pending_source.pop(uid, None)
            self._pending_dest.pop(uid, None)
            await self.db.set_setting("current_job_id", "")
            await msg.reply(
                "🆕 Ready for a new job.\n"
                "Use /setsource and /setdest to configure."
            )

        # ── Unknown command guard for non-auth users ──────────────────────────

        @bot.on_message(~auth)
        async def unauthorized(_, msg: Message):
            await msg.reply("🚫 Unauthorized.")

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _resolve_channel(self, msg: Message) -> Optional[Dict]:
        """Try to identify a channel from a forwarded post, username, or ID."""
        # 1. Forwarded from a channel
        if msg.forward_from_chat and msg.forward_from_chat.type in ("channel",):
            chat = msg.forward_from_chat
            return {"id": chat.id, "title": chat.title or str(chat.id)}

        # 2. Text input
        text = (msg.text or "").strip()
        if not text:
            return None

        try:
            # Numeric ID
            if text.lstrip("-").isdigit():
                chat_id = int(text)
                chat = await self.uc.get_chat(chat_id)
                return {"id": chat.id, "title": chat.title or str(chat.id)}

            # Username
            if text.startswith("@") or not text.startswith("-"):
                slug = text.lstrip("@")
                chat = await self.uc.get_chat(slug)
                if chat.type in ("channel",):
                    return {"id": chat.id, "title": chat.title or slug}
                return None

        except (PeerIdInvalid, UsernameNotOccupied):
            return None
        except Exception as e:
            logger.warning(f"_resolve_channel error: {e}")
            return None

        return None

    async def _get_current_job_id(self) -> Optional[int]:
        val = await self.db.get_setting("current_job_id")
        if val and val.isdigit():
            return int(val)
        job = await self.db.get_latest_job()
        return job["id"] if job else None

    async def _send_progress(self, uid: int, job_id: int, text: str):
        chat_id = self._notify_chat.get(uid, uid)
        try:
            await self.bot.send_message(chat_id, text)
        except Exception as e:
            logger.warning(f"Progress notify failed: {e}")
