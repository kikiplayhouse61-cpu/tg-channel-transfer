"""
Telegram Channel Transfer Bot - inline-button control panel.

The bot client receives commands and button presses. The logged-in user
client discovers channels from its own Telegram dialogs.
"""

import logging
from typing import Optional, Dict, List, Tuple

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

import config
from database import Database
from transfer_engine import TransferEngine

logger = logging.getLogger(__name__)

PAGE_SIZE = 8


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

        # Selected channels are held for the current running process.
        self._pending_source: Dict[int, Dict] = {}
        self._pending_dest: Dict[int, Dict] = {}
        self._notify_chat: Dict[int, int] = {}
        self._channel_cache: Dict[Tuple[int, str], List[Dict]] = {}
        self._user_account_id: Optional[int] = None

    def _main_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📥 Select SOURCE", callback_data="channels:src:0"),
                    InlineKeyboardButton("📤 Select DESTINATION", callback_data="channels:dst:0"),
                ],
                [
                    InlineKeyboardButton("🔍 Scan", callback_data="action:scan"),
                    InlineKeyboardButton("🚀 Start transfer", callback_data="action:transfer"),
                ],
                [
                    InlineKeyboardButton("📊 Status", callback_data="action:status"),
                    InlineKeyboardButton("⚙️ Settings", callback_data="action:settings"),
                ],
                [
                    InlineKeyboardButton("⏸ Pause", callback_data="action:pause"),
                    InlineKeyboardButton("▶️ Resume", callback_data="action:resume"),
                    InlineKeyboardButton("🛑 Stop", callback_data="action:stop"),
                ],
                [
                    InlineKeyboardButton("🔁 Retry failed", callback_data="action:retry"),
                    InlineKeyboardButton("🆕 New job", callback_data="action:newjob"),
                ],
                [InlineKeyboardButton("🔄 Refresh menu", callback_data="action:menu")],
            ]
        )

    async def _get_user_account_id(self) -> int:
        if self._user_account_id is None:
            me = await self.uc.get_me()
            self._user_account_id = me.id
        return self._user_account_id

    @staticmethod
    def _chat_type(chat) -> str:
        value = getattr(chat.type, "value", chat.type)
        return str(value).lower()

    async def _load_channels(self, uid: int, kind: str) -> List[Dict]:
        """
        Read channels from the logged-in user account's Telegram dialogs.
        Source: channels the account has joined.
        Destination: channels where the account is an admin/owner.
        """
        if kind not in ("src", "dst"):
            return []

        account_id = await self._get_user_account_id()
        found: List[Dict] = []

        async for dialog in self.uc.get_dialogs():
            chat = dialog.chat
            if self._chat_type(chat) != "channel":
                continue

            item = {
                "id": chat.id,
                "title": chat.title or str(chat.id),
                "username": getattr(chat, "username", None),
            }

            if kind == "dst":
                try:
                    member = await self.uc.get_chat_member(chat.id, account_id)
                    status = getattr(member.status, "value", member.status)
                    status = str(status).lower()
                    if status not in ("administrator", "owner", "creator"):
                        continue
                except Exception as exc:
                    logger.debug(
                        "Skipping non-admin/unavailable destination %s: %s",
                        chat.id,
                        exc,
                    )
                    continue

            found.append(item)

        found.sort(key=lambda x: x["title"].casefold())
        self._channel_cache[(uid, kind)] = found
        return found

    async def _channel_page(
        self, uid: int, kind: str, page: int
    ) -> Tuple[str, InlineKeyboardMarkup]:
        channels = await self._load_channels(uid, kind)
        title = "SOURCE" if kind == "src" else "DESTINATION (ADMIN/OWNER ONLY)"

        if not channels:
            if kind == "dst":
                text = (
                    "📤 **No destination channels found.**\n\n"
                    "Your logged-in Telegram account must be an administrator "
                    "or owner of a channel. Make sure the account in SESSION_STRING "
                    "has joined the channel."
                )
            else:
                text = (
                    "📥 **No joined channels found.**\n\n"
                    "The Telegram account used by SESSION_STRING must first join "
                    "the source channel."
                )
            return text, InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Refresh", callback_data=f"channels:{kind}:0")],
                 [InlineKeyboardButton("⬅️ Main menu", callback_data="action:menu")]]
            )

        pages = max(1, (len(channels) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * PAGE_SIZE
        visible = channels[start : start + PAGE_SIZE]

        rows = []
        for item in visible:
            label = item["title"][:48]
            if item.get("username"):
                label = f"{label} (@{item['username']})"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"📺 {label}",
                        callback_data=f"pick:{kind}:{item['id']}",
                    )
                ]
            )

        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "⬅️ Previous", callback_data=f"channels:{kind}:{page - 1}"
                )
            )
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton(
                    "Next ➡️", callback_data=f"channels:{kind}:{page + 1}"
                )
            )
        if nav:
            rows.append(nav)

        rows.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=f"channels:{kind}:{page}"),
                InlineKeyboardButton("⬅️ Main menu", callback_data="action:menu"),
            ]
        )

        text = (
            f"**Select {title}**\n\n"
            f"Found: **{len(channels)}** channel(s)\n"
            f"Page: **{page + 1}/{pages}**\n\n"
            "Tap a channel button below. No channel link or ID needs to be sent."
        )
        return text, InlineKeyboardMarkup(rows)

    async def _status_text(self, uid: int) -> str:
        src = self._pending_source.get(uid)
        dst = self._pending_dest.get(uid)
        job_id = await self._get_current_job_id()
        lines = ["**📊 CHANNEL TRANSFER STATUS**", ""]
        lines.append(
            f"📥 Source: **{src['title']}** (`{src['id']}`)"
            if src else "📥 Source: _not selected in this session_"
        )
        lines.append(
            f"📤 Destination: **{dst['title']}** (`{dst['id']}`)"
            if dst else "📤 Destination: _not selected in this session_"
        )

        if job_id:
            job = await self.db.get_job(job_id)
            counts = await self.db.get_message_counts(job_id)
            if job:
                total = counts.get("total") or 0
                transferred = counts.get("transferred") or 0
                pct = (transferred / total * 100) if total else 0
                lines.extend(
                    [
                        "",
                        f"**Job #{job_id}**",
                        f"Status: `{job['status']}`",
                        f"📦 Total: {total}",
                        f"✅ Transferred: {transferred}",
                        f"⏳ Pending: {counts.get('pending', 0)}",
                        f"❌ Failed: {counts.get('failed', 0)}",
                        f"Progress: {pct:.1f}%",
                    ]
                )
        else:
            lines.extend(["", "No job created yet. Select channels, then Scan."])

        lines.extend(
            [
                "",
                f"⏱ Batch interval: {config.TRANSFER_INTERVAL}s",
                f"Engine: {'🟢 Running' if self.engine.is_running() else '⚪ Idle'}",
            ]
        )
        return "\n".join(lines)

    async def _get_current_job_id(self) -> Optional[int]:
        val = await self.db.get_setting("current_job_id")
        if val and str(val).isdigit():
            return int(val)
        job = await self.db.get_latest_job()
        return job["id"] if job else None

    async def _scan(self, uid: int, chat_id: int) -> str:
        src = self._pending_source.get(uid)
        dst = self._pending_dest.get(uid)
        if not src or not dst:
            return "❌ Select both SOURCE and DESTINATION first."

        if self.engine.is_running():
            return "⚠️ A job is already running. Stop it before starting another scan."

        job_id = await self.db.create_job(
            src["id"], src["title"], dst["id"], dst["title"]
        )
        await self.db.set_setting("current_job_id", str(job_id))
        self._notify_chat[uid] = chat_id
        self.engine.set_progress_callback(
            lambda jid, text: self._send_progress(uid, jid, text)
        )
        ok = await self.engine.start_scan(job_id)
        if not ok:
            return "❌ Failed to start scan."
        return (
            f"🔍 **Scan started — Job #{job_id}**\n"
            f"Source: {src['title']}\nDestination: {dst['title']}\n\n"
            "I will send progress updates here."
        )

    async def _transfer(self, uid: int, chat_id: int) -> str:
        job_id = await self._get_current_job_id()
        if not job_id:
            return "❌ No job found. Select channels and run Scan first."

        job = await self.db.get_job(job_id)
        if not job:
            return "❌ Job not found."

        if job["status"] not in ("scan_complete", "paused", "failed"):
            return f"⚠️ Job status is `{job['status']}`. Scan must be complete first."

        if self.engine.is_running():
            return "⚠️ Transfer engine is already running."

        self._notify_chat[uid] = chat_id
        self.engine.set_progress_callback(
            lambda jid, text: self._send_progress(uid, jid, text)
        )
        ok = await self.engine.start_transfer(job_id)
        if not ok:
            return "❌ Failed to start transfer."

        counts = await self.db.get_message_counts(job_id)
        return (
            f"🚀 **Transfer started — Job #{job_id}**\n"
            f"📦 Pending messages: {counts.get('pending', 0)}\n"
            f"⏱ Batch interval: {config.TRANSFER_INTERVAL}s"
        )

    async def _retry(self, uid: int, chat_id: int) -> str:
        job_id = await self._get_current_job_id()
        if not job_id:
            return "❌ No job found."
        if self.engine.is_running():
            return "⚠️ Engine is running. Stop it first."

        failed = await self.db.get_failed_messages(
            job_id, max_retries=config.MAX_RETRIES
        )
        if not failed:
            return "✅ No retryable failed messages."

        # Reset failed rows using the Database connection (not db.db).
        async with self.db._lock:
            for row in failed:
                await self.db._conn.execute(
                    "UPDATE messages SET status = 'pending', error_text = NULL "
                    "WHERE job_id = ? AND source_msg_id = ?",
                    (job_id, row["source_msg_id"]),
                )
            await self.db._conn.commit()

        self._notify_chat[uid] = chat_id
        self.engine.set_progress_callback(
            lambda jid, text: self._send_progress(uid, jid, text)
        )
        ok = await self.engine.start_transfer(job_id)
        return (
            f"🔁 Retrying {len(failed)} failed message(s)."
            if ok else "❌ Could not start retry."
        )

    async def _send_progress(self, uid: int, job_id: int, text: str):
        chat_id = self._notify_chat.get(uid, uid)
        try:
            await self.bot.send_message(chat_id, text)
        except Exception as exc:
            logger.warning("Progress notification failed: %s", exc)

    def register_handlers(self):
        bot = self.bot
        auth = filters.user(config.AUTHORIZED_USER_ID)

        @bot.on_message(filters.command("start") & auth)
        async def cmd_start(_, msg: Message):
            await msg.reply(
                "👋 **Channel Transfer Control Panel**\n\n"
                "Choose channels from your logged-in Telegram account. "
                "Source lists joined channels; destination lists channels "
                "where your account is an admin or owner.",
                reply_markup=self._main_keyboard(),
            )

        @bot.on_message(filters.command("help") & auth)
        async def cmd_help(_, msg: Message):
            await msg.reply(
                "**Commands**\n"
                "/start — Open button panel\n"
                "/setsource — Show joined source channels\n"
                "/setdest — Show admin/owner destination channels\n"
                "/scan — Scan source history\n"
                "/transfer — Start transfer\n"
                "/status — Show progress\n"
                "/pause /resume /stop — Control transfer\n"
                "/retry — Retry failed messages\n"
                "/setinterval 10 or /setinterval 15 — Set batch interval\n"
                "/newjob — Clear selected channels for a new job",
                reply_markup=self._main_keyboard(),
            )

        @bot.on_message(filters.command("setsource") & auth)
        async def cmd_setsource(_, msg: Message):
            text, keyboard = await self._channel_page(msg.from_user.id, "src", 0)
            await msg.reply(text, reply_markup=keyboard)

        @bot.on_message(filters.command("setdest") & auth)
        async def cmd_setdest(_, msg: Message):
            text, keyboard = await self._channel_page(msg.from_user.id, "dst", 0)
            await msg.reply(text, reply_markup=keyboard)

        @bot.on_message(filters.command("scan") & auth)
        async def cmd_scan(_, msg: Message):
            await msg.reply(
                await self._scan(msg.from_user.id, msg.chat.id),
                reply_markup=self._main_keyboard(),
            )

        @bot.on_message(filters.command("transfer") & auth)
        async def cmd_transfer(_, msg: Message):
            await msg.reply(
                await self._transfer(msg.from_user.id, msg.chat.id),
                reply_markup=self._main_keyboard(),
            )

        @bot.on_message(filters.command("pause") & auth)
        async def cmd_pause(_, msg: Message):
            if not self.engine.is_running():
                result = "Nothing is running."
            else:
                self.engine.pause()
                result = "⏸ Transfer paused. Use Resume to continue."
            await msg.reply(result, reply_markup=self._main_keyboard())

        @bot.on_message(filters.command("resume") & auth)
        async def cmd_resume(_, msg: Message):
            if self.engine.is_running():
                self.engine.resume()
                result = "▶️ Transfer resumed."
            else:
                job_id = await self._get_current_job_id()
                job = await self.db.get_job(job_id) if job_id else None
                if job and job["status"] == "paused":
                    result = await self._transfer(msg.from_user.id, msg.chat.id)
                else:
                    result = "⚠️ There is no paused job to resume."
            await msg.reply(result, reply_markup=self._main_keyboard())

        @bot.on_message(filters.command("stop") & auth)
        async def cmd_stop(_, msg: Message):
            self.engine.stop()
            await msg.reply(
                "🛑 Stop signal sent. Progress will be saved.",
                reply_markup=self._main_keyboard(),
            )

        @bot.on_message(filters.command("retry") & auth)
        async def cmd_retry(_, msg: Message):
            await msg.reply(
                await self._retry(msg.from_user.id, msg.chat.id),
                reply_markup=self._main_keyboard(),
            )

        @bot.on_message(filters.command("status") & auth)
        async def cmd_status(_, msg: Message):
            await msg.reply(
                await self._status_text(msg.from_user.id),
                reply_markup=self._main_keyboard(),
            )

        @bot.on_message(filters.command("setinterval") & auth)
        async def cmd_setinterval(_, msg: Message):
            parts = (msg.text or "").split()
            if len(parts) < 2 or parts[1] not in ("10", "15"):
                result = f"Usage: /setinterval 10 or /setinterval 15\nCurrent: {config.TRANSFER_INTERVAL}s"
            else:
                config.TRANSFER_INTERVAL = float(parts[1])
                await self.db.set_setting("transfer_interval", parts[1])
                result = f"✅ Interval set to {parts[1]} seconds."
            await msg.reply(result, reply_markup=self._main_keyboard())

        @bot.on_message(filters.command("newjob") & auth)
        async def cmd_newjob(_, msg: Message):
            if self.engine.is_running():
                result = "⚠️ Stop the current job first."
            else:
                uid = msg.from_user.id
                self._pending_source.pop(uid, None)
                self._pending_dest.pop(uid, None)
                await self.db.set_setting("current_job_id", "")
                result = "🆕 New job ready. Select SOURCE and DESTINATION."
            await msg.reply(result, reply_markup=self._main_keyboard())

        @bot.on_callback_query(auth)
        async def callbacks(_, query: CallbackQuery):
            uid = query.from_user.id
            data = query.data or ""

            try:
                if data.startswith("channels:"):
                    _, kind, page_text = data.split(":", 2)
                    text, keyboard = await self._channel_page(uid, kind, int(page_text))
                    await query.message.edit_text(text, reply_markup=keyboard)
                    await query.answer()
                    return

                if data.startswith("pick:"):
                    _, kind, chat_id_text = data.split(":", 2)
                    chat_id = int(chat_id_text)
                    cache = self._channel_cache.get((uid, kind), [])
                    selected = next(
                        (item for item in cache if item["id"] == chat_id), None
                    )
                    if not selected:
                        await query.answer("Channel list expired. Refresh it.", show_alert=True)
                        return

                    if kind == "src":
                        self._pending_source[uid] = selected
                        text = (
                            f"✅ **SOURCE selected**\n{selected['title']}\n"
                            f"`{selected['id']}`\n\nNow select DESTINATION."
                        )
                        keyboard = InlineKeyboardMarkup(
                            [
                                [InlineKeyboardButton("📤 Choose destination", callback_data="channels:dst:0")],
                                [InlineKeyboardButton("🏠 Main menu", callback_data="action:menu")],
                            ]
                        )
                    elif kind == "dst":
                        # Re-check admin rights at selection time.
                        account_id = await self._get_user_account_id()
                        member = await self.uc.get_chat_member(chat_id, account_id)
                        status = getattr(member.status, "value", member.status)
                        if str(status).lower() not in ("administrator", "owner", "creator"):
                            await query.answer("Admin/owner permission is required.", show_alert=True)
                            return
                        self._pending_dest[uid] = selected
                        src = self._pending_source.get(uid)
                        text = (
                            f"✅ **DESTINATION selected**\n{selected['title']}\n"
                            f"`{selected['id']}`\n\n"
                            + (f"Source: **{src['title']}**\n\n" if src else "")
                            + "Press Scan when you are ready."
                        )
                        keyboard = self._main_keyboard()
                    else:
                        await query.answer("Invalid selection.", show_alert=True)
                        return

                    await query.message.edit_text(text, reply_markup=keyboard)
                    await query.answer("Channel selected.")
                    return

                if data == "action:menu":
                    await query.message.edit_text(
                        "📡 **Channel Transfer Control Panel**\n\nChoose an action:",
                        reply_markup=self._main_keyboard(),
                    )
                    await query.answer()
                elif data == "action:scan":
                    result = await self._scan(uid, query.message.chat.id)
                    await query.message.reply(result, reply_markup=self._main_keyboard())
                    await query.answer("Scan command processed.")
                elif data == "action:transfer":
                    result = await self._transfer(uid, query.message.chat.id)
                    await query.message.reply(result, reply_markup=self._main_keyboard())
                    await query.answer("Transfer command processed.")
                elif data == "action:status":
                    await query.message.reply(
                        await self._status_text(uid),
                        reply_markup=self._main_keyboard(),
                    )
                    await query.answer()
                elif data == "action:pause":
                    if self.engine.is_running():
                        self.engine.pause()
                        result = "⏸ Transfer paused."
                    else:
                        result = "Nothing is running."
                    await query.message.reply(result, reply_markup=self._main_keyboard())
                    await query.answer()
                elif data == "action:resume":
                    if self.engine.is_running():
                        self.engine.resume()
                        result = "▶️ Transfer resumed."
                    else:
                        job_id = await self._get_current_job_id()
                        job = await self.db.get_job(job_id) if job_id else None
                        result = (
                            await self._transfer(uid, query.message.chat.id)
                            if job and job["status"] == "paused"
                            else "No paused job found."
                        )
                    await query.message.reply(result, reply_markup=self._main_keyboard())
                    await query.answer()
                elif data == "action:stop":
                    self.engine.stop()
                    await query.message.reply(
                        "🛑 Stop signal sent.", reply_markup=self._main_keyboard()
                    )
                    await query.answer()
                elif data == "action:retry":
                    await query.message.reply(
                        await self._retry(uid, query.message.chat.id),
                        reply_markup=self._main_keyboard(),
                    )
                    await query.answer()
                elif data == "action:settings":
                    await query.message.reply(
                        f"⚙️ **Settings**\n\nBatch interval: {config.TRANSFER_INTERVAL}s\n"
                        "Use /setinterval 10 or /setinterval 15.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton("10 seconds", callback_data="action:interval:10"),
                                    InlineKeyboardButton("15 seconds", callback_data="action:interval:15"),
                                ],
                                [InlineKeyboardButton("⬅️ Main menu", callback_data="action:menu")],
                            ]
                        ),
                    )
                    await query.answer()
                elif data in ("action:interval:10", "action:interval:15"):
                    value = data.rsplit(":", 1)[1]
                    config.TRANSFER_INTERVAL = float(value)
                    await self.db.set_setting("transfer_interval", value)
                    await query.message.reply(
                        f"✅ Batch interval set to {value} seconds.",
                        reply_markup=self._main_keyboard(),
                    )
                    await query.answer()
                elif data == "action:newjob":
                    if self.engine.is_running():
                        result = "⚠️ Stop the current job first."
                    else:
                        self._pending_source.pop(uid, None)
                        self._pending_dest.pop(uid, None)
                        await self.db.set_setting("current_job_id", "")
                        result = "🆕 New job ready. Select SOURCE and DESTINATION."
                    await query.message.reply(result, reply_markup=self._main_keyboard())
                    await query.answer()
                else:
                    await query.answer("Unknown button.", show_alert=True)

            except Exception as exc:
                logger.exception("Callback handling failed: %s", exc)
                try:
                    await query.answer("Something went wrong. Check Railway logs.", show_alert=True)
                except Exception:
                    pass

        @bot.on_message(filters.private & ~auth)
        async def unauthorized(_, msg: Message):
            await msg.reply("🚫 Unauthorized.")
