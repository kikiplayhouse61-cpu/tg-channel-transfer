"""Telegram Channel Transfer Bot - button-driven control panel."""
import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import config
from database import Database
from transfer_engine import TransferEngine

logger = logging.getLogger(__name__)
PAGE_SIZE = 8
CHANNEL_CACHE_SECONDS = 120


class BotHandlers:
    def __init__(self, bot: Client, user_client: Client, db: Database, engine: TransferEngine):
        self.bot = bot
        self.uc = user_client
        self.db = db
        self.engine = engine
        self._source: Dict[int, Dict] = {}
        self._dest: Dict[int, Dict] = {}
        self._notify_chat: Dict[int, int] = {}
        self._selection_loaded = set()
        self._account_id: Optional[int] = None
        self._dialogs: List[Dict] = []
        self._dialogs_loaded_at = 0.0
        self._channel_cache: Dict[Tuple[int, str], Tuple[float, List[Dict]]] = {}
        self._channel_lock = asyncio.Lock()

    # ---------- UI ----------
    def _menu_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📥  SOURCE CHANNEL", callback_data="channels:src:0"),
             InlineKeyboardButton("📤  DESTINATION", callback_data="channels:dst:0")],
            [InlineKeyboardButton("🔎  SCAN FULL HISTORY", callback_data="action:scan")],
            [InlineKeyboardButton("🚀  START TRANSFER", callback_data="action:transfer")],
            [InlineKeyboardButton("📊  STATUS", callback_data="action:status"),
             InlineKeyboardButton("⚙️  SETTINGS", callback_data="action:settings")],
            [InlineKeyboardButton("⏸ PAUSE", callback_data="action:pause"),
             InlineKeyboardButton("▶️ RESUME", callback_data="action:resume"),
             InlineKeyboardButton("⏹ STOP", callback_data="action:stop")],
            [InlineKeyboardButton("↻ RETRY FAILED", callback_data="action:retry"),
             InlineKeyboardButton("＋ NEW JOB", callback_data="action:newjob")],
        ])

    @staticmethod
    def _back_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("‹  MAIN MENU", callback_data="action:menu")]])

    async def _ensure_selection(self, uid: int):
        if uid in self._selection_loaded:
            return
        src_id = await self.db.get_setting(f"source:{uid}:id")
        dst_id = await self.db.get_setting(f"dest:{uid}:id")
        if src_id and src_id.lstrip("-").isdigit():
            self._source[uid] = {"id": int(src_id), "title": await self.db.get_setting(f"source:{uid}:title", src_id)}
        if dst_id and dst_id.lstrip("-").isdigit():
            self._dest[uid] = {"id": int(dst_id), "title": await self.db.get_setting(f"dest:{uid}:title", dst_id)}
        self._selection_loaded.add(uid)

    async def _save_selection(self, uid: int, kind: str, item: Dict):
        prefix = "source" if kind == "src" else "dest"
        await self.db.set_setting(f"{prefix}:{uid}:id", str(item["id"]))
        await self.db.set_setting(f"{prefix}:{uid}:title", item["title"])

    async def _menu_text(self, uid: int) -> str:
        await self._ensure_selection(uid)
        src, dst = self._source.get(uid), self._dest.get(uid)
        job_id = await self._get_current_job_id()
        job = await self.db.get_job(job_id) if job_id else None
        return (
            "**CHANNEL TRANSFER**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📥 Source: {src['title'] if src else 'Not selected'}\n"
            f"📤 Destination: {dst['title'] if dst else 'Not selected'}\n"
            f"🧾 Job: #{job_id} · {job['status'] if job else 'Not started'}\n\n"
            "Choose an action below. Channel lists are loaded from your logged-in Telegram account."
        )

    @staticmethod
    def _chat_type(chat) -> str:
        value = getattr(chat.type, "value", None) or getattr(chat.type, "name", None) or chat.type
        return str(value).lower().split(".")[-1]

    @staticmethod
    def _member_status(member) -> str:
        status = getattr(member, "status", "")
        value = getattr(status, "value", None) or getattr(status, "name", None) or status
        return str(value).lower().split(".")[-1]

    async def _get_dialog_channels(self, force: bool = False) -> List[Dict]:
        async with self._channel_lock:
            now = time.monotonic()
            if not force and self._dialogs and now - self._dialogs_loaded_at < CHANNEL_CACHE_SECONDS:
                return self._dialogs
            channels = []
            async for dialog in self.uc.get_dialogs():
                chat = dialog.chat
                if self._chat_type(chat) != "channel":
                    continue
                channels.append({
                    "id": int(chat.id),
                    "title": (getattr(chat, "title", None) or str(chat.id)).strip(),
                    "username": getattr(chat, "username", None),
                })
            channels.sort(key=lambda item: item["title"].casefold())
            self._dialogs = channels
            self._dialogs_loaded_at = time.monotonic()
            self._channel_cache.clear()
            logger.info("Loaded %d joined channels from Telegram dialogs", len(channels))
            return channels

    async def _load_channels(self, uid: int, kind: str, force: bool = False) -> List[Dict]:
        if kind not in ("src", "dst"):
            return []
        cache_key = (uid, kind)
        cached = self._channel_cache.get(cache_key)
        now = time.monotonic()
        if not force and cached and now - cached[0] < CHANNEL_CACHE_SECONDS:
            return cached[1]
        channels = await self._get_dialog_channels(force=force)
        if kind == "src":
            result = channels
        else:
            me = await self.uc.get_me()
            semaphore = asyncio.Semaphore(8)

            async def check_admin(item):
                async with semaphore:
                    try:
                        member = await self.uc.get_chat_member(item["id"], me.id)
                        status = self._member_status(member)
                        if status not in ("administrator", "owner", "creator"):
                            return None
                        # For channels, posting permission is required for a useful destination.
                        privileges = getattr(member, "privileges", None)
                        can_post = getattr(privileges, "can_post_messages", None) if privileges else None
                        if can_post is False and status != "owner":
                            return None
                        return item
                    except Exception as exc:
                        logger.debug("Destination check skipped %s: %s", item["id"], exc)
                        return None

            checked = await asyncio.gather(*(check_admin(item) for item in channels))
            result = [item for item in checked if item]
        self._channel_cache[cache_key] = (time.monotonic(), result)
        return result

    async def _channel_page(self, uid: int, kind: str, page: int, force: bool = False):
        channels = await self._load_channels(uid, kind, force=force)
        label = "SOURCE · JOINED CHANNELS" if kind == "src" else "DESTINATION · ADMIN CHANNELS"
        if not channels:
            hint = ("Your logged-in account has no channels visible in its Telegram dialogs. Join the source channel first."
                    if kind == "src" else "No writable admin/owner channels found. Check that the SESSION_STRING account is an admin and can post.")
            return f"**{label}**\n\n{hint}", InlineKeyboardMarkup([
                [InlineKeyboardButton("↻ REFRESH", callback_data=f"channels:{kind}:0:refresh")],
                [InlineKeyboardButton("‹ MAIN MENU", callback_data="action:menu")],
            ])
        pages = max(1, (len(channels) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        shown = channels[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        rows = []
        for item in shown:
            title = item["title"][:35]
            if item.get("username"):
                title += f" · @{item['username']}"
            rows.append([InlineKeyboardButton(f"▣  {title}", callback_data=f"pick:{kind}:{item['id']}")])
        nav = []
        if page:
            nav.append(InlineKeyboardButton("‹ PREV", callback_data=f"channels:{kind}:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("NEXT ›", callback_data=f"channels:{kind}:{page+1}"))
        rows.append(nav)
        rows.append([
            InlineKeyboardButton("↻ REFRESH", callback_data=f"channels:{kind}:{page}:refresh"),
            InlineKeyboardButton("‹ MENU", callback_data="action:menu"),
        ])
        text = f"**{label}**\n{len(channels)} channel(s) found\n\nTap a channel to select it. No link or ID needed."
        return text, InlineKeyboardMarkup(rows)

    # ---------- Jobs ----------
    async def _get_current_job_id(self) -> Optional[int]:
        value = await self.db.get_setting("current_job_id")
        if value == "0":
            return None
        if value and str(value).isdigit():
            return int(value)
        job = await self.db.get_latest_job()
        return int(job["id"]) if job else None

    async def _set_notify(self, uid: int, chat_id: int):
        self._notify_chat[uid] = chat_id
        self.engine.set_progress_callback(lambda job_id, text: self._send_progress(uid, job_id, text))

    async def _send_progress(self, uid: int, job_id: int, text: str):
        chat_id = self._notify_chat.get(uid, uid)
        try:
            await self.bot.send_message(chat_id, f"**JOB #{job_id}**\n{text}")
        except Exception as exc:
            logger.warning("Progress notification failed: %s", exc)

    async def _scan(self, uid: int, chat_id: int) -> str:
        await self._ensure_selection(uid)
        src, dst = self._source.get(uid), self._dest.get(uid)
        if not src or not dst:
            return "Select both SOURCE and DESTINATION first."
        if self.engine.is_running():
            return "A task is already running. Use STOP before starting another task."
        current_id = await self._get_current_job_id()
        current = await self.db.get_job(current_id) if current_id else None
        if current and current["source_id"] == src["id"] and current["dest_id"] == dst["id"] and current["status"] in ("scan_paused", "scanning", "error"):
            job_id = current_id
        elif current and current["source_id"] == src["id"] and current["dest_id"] == dst["id"] and current["status"] == "scan_complete":
            return f"Scan already complete for Job #{current_id}. Use START TRANSFER."
        else:
            job_id = await self.db.create_job(src["id"], src["title"], dst["id"], dst["title"])
        await self.db.set_setting("current_job_id", str(job_id))
        await self._set_notify(uid, chat_id)
        ok = await self.engine.start_scan(job_id)
        return f"Full history scan started for Job #{job_id}. You will receive progress updates." if ok else "Could not start scan. Check the job status and Railway logs."

    async def _transfer(self, uid: int, chat_id: int) -> str:
        job_id = await self._get_current_job_id()
        if not job_id:
            return "No job found. Select channels and scan first."
        job = await self.db.get_job(job_id)
        if not job:
            return "Current job was not found in the database."
        if self.engine.is_running():
            return "A task is already running."
        counts = await self.db.get_message_counts(job_id)
        if (counts.get("total") or 0) == 0:
            return "No scanned messages found. Run FULL HISTORY SCAN first."
        await self._set_notify(uid, chat_id)
        ok = await self.engine.start_transfer(job_id)
        return (f"Transfer started · Job #{job_id}\n"
                f"Messages queued: {counts.get('pending', 0)}\n"
                f"Batch size: {config.BATCH_SIZE} · Batch wait: {config.TRANSFER_INTERVAL:g}s") if ok else f"Cannot start transfer from status `{job['status']}`. Scan must be complete or job paused/failed."

    async def _status_text(self, uid: int) -> str:
        await self._ensure_selection(uid)
        src, dst = self._source.get(uid), self._dest.get(uid)
        job_id = await self._get_current_job_id()
        job = await self.db.get_job(job_id) if job_id else None
        lines = ["**TRANSFER STATUS**", "━━━━━━━━━━━━━━━━━━",
                 f"📥 Source: {src['title'] if src else 'Not selected'}",
                 f"📤 Destination: {dst['title'] if dst else 'Not selected'}"]
        if job:
            counts = await self.db.get_message_counts(job_id)
            total = counts.get("total") or 0
            done = counts.get("transferred") or 0
            pct = done * 100 / total if total else 0
            lines += ["", f"Job: **#{job_id}** · `{job['status']}`",
                      f"Scanned: {job.get('scanned_count', 0)} / {total}",
                      f"Transferred: {done}", f"Pending: {counts.get('pending', 0)}",
                      f"Failed: {counts.get('failed', 0)}", f"Progress: {pct:.1f}%"]
        else:
            lines.append("\nNo job created yet.")
        lines += ["", f"Batch size: {config.BATCH_SIZE}", f"Batch wait: {config.TRANSFER_INTERVAL:g}s",
                  f"Engine: {'RUNNING' if self.engine.is_running() else 'IDLE'}"]
        return "\n".join(lines)

    async def _retry(self, uid: int, chat_id: int) -> str:
        job_id = await self._get_current_job_id()
        if not job_id:
            return "No job found."
        if self.engine.is_running():
            return "Stop the running task before retrying."
        reset = await self.db.reset_failed_messages(job_id)
        if reset <= 0:
            return "No retryable failed messages were found."
        await self.db.update_job_status(job_id, "scan_complete")
        await self._set_notify(uid, chat_id)
        ok = await self.engine.start_transfer(job_id)
        return f"Retry started for {reset} failed message(s)." if ok else "Could not start retry."

    # ---------- Registration ----------
    def register_handlers(self):
        bot = self.bot
        auth = filters.user(config.AUTHORIZED_USER_ID)

        @bot.on_message(filters.private & filters.command("start") & auth)
        async def start_handler(_, msg: Message):
            uid = msg.from_user.id
            await self._ensure_selection(uid)
            await msg.reply_text(await self._menu_text(uid), reply_markup=self._menu_keyboard())

        @bot.on_message(filters.private & filters.command("help") & auth)
        async def help_handler(_, msg: Message):
            await msg.reply_text("Use the buttons below to select channels, scan full history, transfer, pause, resume and check status.", reply_markup=self._menu_keyboard())

        @bot.on_message(filters.private & filters.command("status") & auth)
        async def status_handler(_, msg: Message):
            await msg.reply_text(await self._status_text(msg.from_user.id), reply_markup=self._menu_keyboard())

        @bot.on_message(filters.private & filters.command("scan") & auth)
        async def scan_handler(_, msg: Message):
            await msg.reply_text(await self._scan(msg.from_user.id, msg.chat.id), reply_markup=self._menu_keyboard())

        @bot.on_message(filters.private & filters.command("transfer") & auth)
        async def transfer_handler(_, msg: Message):
            await msg.reply_text(await self._transfer(msg.from_user.id, msg.chat.id), reply_markup=self._menu_keyboard())

        @bot.on_message(filters.private & filters.command("setsource") & auth)
        async def source_handler(_, msg: Message):
            text, keyboard = await self._channel_page(msg.from_user.id, "src", 0)
            await msg.reply_text(text, reply_markup=keyboard)

        @bot.on_message(filters.private & filters.command("setdest") & auth)
        async def dest_handler(_, msg: Message):
            text, keyboard = await self._channel_page(msg.from_user.id, "dst", 0)
            await msg.reply_text(text, reply_markup=keyboard)

        @bot.on_callback_query(auth)
        async def callback_handler(_, query: CallbackQuery):
            data = query.data or ""
            uid = query.from_user.id
            try:
                if data == "noop":
                    await query.answer()
                    return
                if data.startswith("channels:"):
                    parts = data.split(":")
                    kind, page = parts[1], int(parts[2])
                    force = len(parts) > 3 and parts[3] == "refresh"
                    await query.answer("Loading channels…")
                    text, keyboard = await self._channel_page(uid, kind, page, force=force)
                    await query.message.edit_text(text, reply_markup=keyboard)
                    return
                if data.startswith("pick:"):
                    _, kind, id_text = data.split(":", 2)
                    chat_id = int(id_text)
                    await query.answer("Checking channel…")
                    channels = await self._load_channels(uid, kind)
                    item = next((x for x in channels if x["id"] == chat_id), None)
                    if not item:
                        await query.message.edit_text("Channel list expired or access changed. Refresh and select again.", reply_markup=self._back_keyboard())
                        return
                    if kind == "dst":
                        me = await self.uc.get_me()
                        member = await self.uc.get_chat_member(chat_id, me.id)
                        if self._member_status(member) not in ("administrator", "owner", "creator"):
                            await query.answer("Admin/owner access is required.", show_alert=True)
                            return
                        self._dest[uid] = item
                        await self._save_selection(uid, kind, item)
                        await query.message.edit_text(f"**DESTINATION SELECTED**\n\n{item['title']}\n`{item['id']}`\n\nChoose SOURCE if not selected, then scan.", reply_markup=self._menu_keyboard())
                    else:
                        self._source[uid] = item
                        await self._save_selection(uid, kind, item)
                        await query.message.edit_text(f"**SOURCE SELECTED**\n\n{item['title']}\n`{item['id']}`\n\nNow choose DESTINATION.", reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("📤 SELECT DESTINATION", callback_data="channels:dst:0")],
                            [InlineKeyboardButton("‹ MAIN MENU", callback_data="action:menu")],
                        ]))
                    return
                await query.answer()
                if data == "action:menu":
                    await self._ensure_selection(uid)
                    await query.message.edit_text(await self._menu_text(uid), reply_markup=self._menu_keyboard())
                elif data == "action:scan":
                    await query.message.reply_text(await self._scan(uid, query.message.chat.id), reply_markup=self._menu_keyboard())
                elif data == "action:transfer":
                    await query.message.reply_text(await self._transfer(uid, query.message.chat.id), reply_markup=self._menu_keyboard())
                elif data == "action:status":
                    await query.message.reply_text(await self._status_text(uid), reply_markup=self._menu_keyboard())
                elif data == "action:pause":
                    if self.engine.is_running():
                        self.engine.pause()
                        result = "Pause requested. The current message will finish first."
                    else:
                        result = "No active task."
                    await query.message.reply_text(result, reply_markup=self._menu_keyboard())
                elif data == "action:resume":
                    if self.engine.is_running():
                        self.engine.resume()
                        result = "Task resumed."
                    else:
                        job_id = await self._get_current_job_id()
                        job = await self.db.get_job(job_id) if job_id else None
                        if job and job["status"] == "paused":
                            result = await self._transfer(uid, query.message.chat.id)
                        elif job and job["status"] == "scan_paused":
                            self.engine.set_progress_callback(lambda jid, text: self._send_progress(uid, jid, text))
                            result = "Scan can be continued with SCAN FULL HISTORY."
                        else:
                            result = "No paused transfer found."
                    await query.message.reply_text(result, reply_markup=self._menu_keyboard())
                elif data == "action:stop":
                    if self.engine.is_running():
                        self.engine.stop()
                        result = "Stop requested. Current operation will finish and progress will be saved."
                    else:
                        result = "No active task."
                    await query.message.reply_text(result, reply_markup=self._menu_keyboard())
                elif data == "action:retry":
                    await query.message.reply_text(await self._retry(uid, query.message.chat.id), reply_markup=self._menu_keyboard())
                elif data == "action:newjob":
                    if self.engine.is_running():
                        result = "Stop the active task first."
                    else:
                        await self.db.set_setting("current_job_id", "0")
                        result = "New job ready. Select channels and scan. Previous jobs remain in the database."
                    await query.message.reply_text(result, reply_markup=self._menu_keyboard())
                elif data == "action:settings":
                    await query.message.reply_text(
                        f"**SETTINGS**\nBatch size: `{config.BATCH_SIZE}`\nBatch wait: `{config.TRANSFER_INTERVAL:g}s`\n\nTelegram may temporarily slow transfers using FloodWait. The bot waits and retries instead of bypassing limits.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("2s", callback_data="interval:2"), InlineKeyboardButton("5s", callback_data="interval:5"), InlineKeyboardButton("10s", callback_data="interval:10")],
                            [InlineKeyboardButton("‹ MAIN MENU", callback_data="action:menu")],
                        ])
                    )
                elif data.startswith("interval:"):
                    value = int(data.split(":", 1)[1])
                    if value not in (2, 5, 10):
                        raise ValueError("Unsupported interval")
                    config.TRANSFER_INTERVAL = float(value)
                    await self.db.set_setting("transfer_interval", str(value))
                    await query.message.reply_text(f"Batch wait updated to {value}s.", reply_markup=self._menu_keyboard())
            except Exception as exc:
                logger.exception("Callback failed: %s", exc)
                try:
                    await query.answer("Action failed. Check Railway logs.", show_alert=True)
                except Exception:
                    pass

        @bot.on_message(filters.private & ~auth)
        async def unauthorized(_, msg: Message):
            await msg.reply_text("🚫 Unauthorized.")
