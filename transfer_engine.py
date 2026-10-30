"""
Transfer Engine
───────────────
Handles scanning source channel history and copying messages
to the destination channel via the user client.

State machine:
  idle → scanning → scan_complete → transferring → complete
                                 ↘ paused → transferring
"""

import asyncio
import logging
import os
import time
from typing import Optional, Callable, Awaitable

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    ChannelPrivate,
    ChatAdminRequired,
    MessageIdInvalid,
    FileReferenceExpired,
    PeerIdInvalid,
    RPCError,
)
from pyrogram.types import Message

import config
from database import Database

logger = logging.getLogger(__name__)

# Telegram message types we handle
MEDIA_TYPES = {
    "photo", "video", "document", "audio", "voice",
    "video_note", "sticker", "animation", "contact",
    "location", "poll",
}


def classify_message(msg: Message) -> str:
    for t in MEDIA_TYPES:
        if getattr(msg, t, None):
            return t
    if msg.text:
        return "text"
    return "service"


class TransferEngine:
    def __init__(self, user_client: Client, db: Database):
        self.uc = user_client
        self.db = db

        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()   # not paused initially

        self._current_job_id: Optional[int] = None
        self._source_accessible: bool = True
        self._progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
        self._task: Optional[asyncio.Task] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def set_progress_callback(self, cb: Callable[[int, str], Awaitable[None]]):
        """Called with (job_id, message) on notable progress events."""
        self._progress_callback = cb

    async def start_scan(self, job_id: int) -> bool:
        job = await self.db.get_job(job_id)
        if not job:
            return False
        self._current_job_id = job_id
        await self.db.update_job_status(job_id, "scanning")
        self._task = asyncio.create_task(self._scan_loop(job))
        return True

    async def start_transfer(self, job_id: int) -> bool:
        job = await self.db.get_job(job_id)
        if not job:
            return False
        if job["status"] not in ("scan_complete", "paused", "failed"):
            return False
        self._current_job_id = job_id
        self._stop_event.clear()
        self._pause_event.set()
        await self.db.update_job_status(job_id, "transferring")
        self._task = asyncio.create_task(self._transfer_loop(job))
        return True

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()  # unblock if waiting

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── Scan ──────────────────────────────────────────────────────────────────

    async def _scan_loop(self, job: dict):
        job_id = job["id"]
        source_id = job["source_id"]
        scanned = 0

        await self._notify(job_id, "🔍 Scanning source channel…")
        logger.info(f"[Job {job_id}] Scan started — source {source_id}")

        try:
            async for msg in self.uc.get_chat_history(source_id):
                if self._stop_event.is_set():
                    break

                if msg.service or classify_message(msg) == "service":
                    continue

                await self.db.upsert_message(
                    job_id,
                    msg.id,
                    classify_message(msg),
                    msg.caption or msg.text,
                )
                scanned += 1

                if scanned % 100 == 0:
                    await self.db.update_job_counts(job_id, scanned=scanned)
                    await self._notify(job_id, f"🔍 Scanned {scanned} messages…")

                await asyncio.sleep(0.05)  # avoid hammering local

            await self.db.update_job_counts(
                job_id, total=scanned, scanned=scanned
            )
            await self.db.update_job_status(job_id, "scan_complete")
            await self._notify(
                job_id,
                f"✅ Scan complete — {scanned} messages found.\n"
                "Use /transfer to begin copying.",
            )
            logger.info(f"[Job {job_id}] Scan complete — {scanned} messages")

        except (ChannelPrivate, ChatAdminRequired, PeerIdInvalid) as e:
            self._source_accessible = False
            await self.db.update_job_status(job_id, "error")
            await self._notify(job_id, f"❌ Cannot access source channel: {e}")
            logger.error(f"[Job {job_id}] Source inaccessible: {e}")

        except Exception as e:
            await self.db.update_job_status(job_id, "error")
            await self._notify(job_id, f"❌ Scan error: {e}")
            logger.exception(f"[Job {job_id}] Scan crashed")

    # ── Transfer ──────────────────────────────────────────────────────────────

    async def _transfer_loop(self, job: dict):
        job_id = job["id"]
        source_id = job["source_id"]
        dest_id = job["dest_id"]
        interval = config.TRANSFER_INTERVAL
        batch_size = config.BATCH_SIZE

        await self._notify(
            job_id,
            f"🚀 Transfer started — {interval}s interval between batches.",
        )
        logger.info(f"[Job {job_id}] Transfer started")

        while not self._stop_event.is_set():
            await self._pause_event.wait()

            pending = await self.db.get_pending_messages(job_id, limit=batch_size)
            if not pending:
                # Check failed messages eligible for retry
                retryable = await self.db.get_failed_messages(
                    job_id, max_retries=config.MAX_RETRIES
                )
                if not retryable:
                    break  # all done
                pending = retryable[:batch_size]

            for row in pending:
                if self._stop_event.is_set():
                    break
                await self._copy_one(job_id, source_id, dest_id, row)
                await asyncio.sleep(0.5)

            counts = await self.db.get_message_counts(job_id)
            await self.db.update_job_counts(
                job_id,
                transferred=counts.get("transferred", 0),
                failed=counts.get("failed", 0),
            )

            remaining = counts.get("pending", 0)
            if remaining > 0:
                await self._notify(
                    job_id,
                    f"📊 Transferred: {counts.get('transferred', 0)} | "
                    f"Failed: {counts.get('failed', 0)} | "
                    f"Remaining: {remaining}",
                )
                await asyncio.sleep(interval)

        if not self._stop_event.is_set():
            counts = await self.db.get_message_counts(job_id)
            await self.db.update_job_status(job_id, "complete")
            await self._notify(
                job_id,
                f"🎉 Transfer complete!\n"
                f"✅ Transferred: {counts.get('transferred', 0)}\n"
                f"❌ Failed: {counts.get('failed', 0)}\n"
                f"📦 Total: {counts.get('total', 0)}",
            )
            logger.info(f"[Job {job_id}] Transfer complete")
        else:
            await self.db.update_job_status(job_id, "paused")
            await self._notify(job_id, "⏸ Transfer paused. Use /resume to continue.")

    async def _copy_one(
        self, job_id: int, source_id: int, dest_id: int, row: dict
    ):
        src_msg_id = row["source_msg_id"]

        try:
            # Primary path: copy_message (no local download)
            result = await self._retry_copy(source_id, dest_id, src_msg_id)
            await self.db.mark_message_transferred(job_id, src_msg_id, result.id)
            await self.db.increment_job_counter(job_id, "transferred_count")
            logger.debug(f"[Job {job_id}] Copied msg {src_msg_id} → {result.id}")

        except FileReferenceExpired:
            # Fallback: download locally then re-upload
            await self._fallback_upload(job_id, source_id, dest_id, row)

        except (ChannelPrivate, PeerIdInvalid, MessageIdInvalid):
            self._source_accessible = False
            # If we have a local cache, try uploading from it
            if row.get("local_path") and os.path.exists(row["local_path"]):
                await self._upload_from_cache(job_id, dest_id, row)
            else:
                await self.db.mark_message_failed(
                    job_id,
                    src_msg_id,
                    "Source inaccessible — no local cache available",
                )
                await self.db.increment_job_counter(job_id, "failed_count")

        except Exception as e:
            await self.db.mark_message_failed(job_id, src_msg_id, str(e))
            await self.db.increment_job_counter(job_id, "failed_count")
            logger.warning(f"[Job {job_id}] Failed msg {src_msg_id}: {e}")

    async def _retry_copy(
        self, source_id: int, dest_id: int, msg_id: int, attempts: int = 0
    ) -> Message:
        """copy_message with FloodWait backoff."""
        try:
            return await self.uc.copy_message(
                chat_id=dest_id,
                from_chat_id=source_id,
                message_id=msg_id,
            )
        except FloodWait as e:
            wait = e.value + 5
            logger.info(f"FloodWait {wait}s")
            if wait > config.FLOOD_SLEEP_THRESHOLD:
                raise RuntimeError(f"FloodWait {wait}s exceeds threshold")
            await asyncio.sleep(wait)
            if attempts < config.MAX_RETRIES:
                return await self._retry_copy(source_id, dest_id, msg_id, attempts + 1)
            raise

    async def _fallback_upload(
        self, job_id: int, source_id: int, dest_id: int, row: dict
    ):
        """Download from source then upload to dest."""
        src_msg_id = row["source_msg_id"]
        try:
            msgs = await self.uc.get_messages(source_id, src_msg_id)
            if not msgs or not msgs.media:
                # text only or empty
                if msgs and msgs.text:
                    sent = await self.uc.send_message(dest_id, msgs.text)
                    await self.db.mark_message_transferred(job_id, src_msg_id, sent.id)
                    return
                raise ValueError("No media and no text")

            os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
            local = await self.uc.download_media(
                msgs,
                file_name=os.path.join(
                    config.DOWNLOAD_DIR, f"{job_id}_{src_msg_id}"
                ),
            )
            await self.db.set_message_local_path(job_id, src_msg_id, local)

            sent = await self._upload_file(
                dest_id, local, row["msg_type"], row.get("caption") or ""
            )
            await self.db.mark_message_transferred(job_id, src_msg_id, sent.id)
            # Clean up local file
            try:
                os.remove(local)
            except OSError:
                pass

        except Exception as e:
            await self.db.mark_message_failed(job_id, src_msg_id, f"fallback: {e}")
            await self.db.increment_job_counter(job_id, "failed_count")

    async def _upload_from_cache(
        self, job_id: int, dest_id: int, row: dict
    ):
        """Upload a previously downloaded local file."""
        src_msg_id = row["source_msg_id"]
        local = row["local_path"]
        try:
            sent = await self._upload_file(
                dest_id, local, row["msg_type"], row.get("caption") or ""
            )
            await self.db.mark_message_transferred(job_id, src_msg_id, sent.id)
            try:
                os.remove(local)
            except OSError:
                pass
        except Exception as e:
            await self.db.mark_message_failed(job_id, src_msg_id, f"cache upload: {e}")
            await self.db.increment_job_counter(job_id, "failed_count")

    async def _upload_file(
        self, dest_id: int, local: str, msg_type: str, caption: str
    ) -> Message:
        kwargs = {"chat_id": dest_id, "caption": caption or None}
        if msg_type == "photo":
            return await self.uc.send_photo(photo=local, **kwargs)
        elif msg_type == "video":
            return await self.uc.send_video(video=local, **kwargs)
        elif msg_type == "audio":
            return await self.uc.send_audio(audio=local, **kwargs)
        elif msg_type == "voice":
            return await self.uc.send_voice(voice=local, **kwargs)
        elif msg_type == "video_note":
            return await self.uc.send_video_note(video_note=local, chat_id=dest_id)
        elif msg_type == "animation":
            return await self.uc.send_animation(animation=local, **kwargs)
        elif msg_type == "sticker":
            return await self.uc.send_sticker(sticker=local, chat_id=dest_id)
        else:
            return await self.uc.send_document(document=local, **kwargs)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _notify(self, job_id: int, message: str):
        if self._progress_callback:
            try:
                await self._progress_callback(job_id, message)
            except Exception:
                pass
        logger.info(f"[Job {job_id}] {message}")
