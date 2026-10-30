"""Asynchronous Telegram channel scanner and transfer engine."""
import asyncio
import logging
import os
from typing import Optional, Callable, Awaitable

from pyrogram import Client
from pyrogram.errors import FloodWait, FileReferenceExpired, ChannelPrivate, ChatAdminRequired, MessageIdInvalid, PeerIdInvalid, RPCError
from pyrogram.types import Message

import config
from database import Database

logger = logging.getLogger(__name__)
MEDIA_TYPES = {"photo", "video", "document", "audio", "voice", "video_note", "sticker", "animation"}


def classify_message(msg: Message) -> str:
    for kind in MEDIA_TYPES | {"contact", "location", "poll"}:
        if getattr(msg, kind, None):
            return kind
    if getattr(msg, "text", None):
        return "text"
    return "service"


class TransferEngine:
    def __init__(self, user_client: Client, db: Database):
        self.uc = user_client
        self.db = db
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._current_job_id: Optional[int] = None
        self._progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
        self._task: Optional[asyncio.Task] = None

    def set_progress_callback(self, cb):
        self._progress_callback = cb

    async def start_scan(self, job_id: int) -> bool:
        job = await self.db.get_job(job_id)
        if not job or self.is_running():
            return False
        self._stop_event.clear()
        self._pause_event.set()
        self._current_job_id = job_id
        await self.db.update_job_status(job_id, "scanning")
        self._task = asyncio.create_task(self._scan_loop(job))
        return True

    async def start_transfer(self, job_id: int) -> bool:
        job = await self.db.get_job(job_id)
        if not job or self.is_running() or job["status"] not in ("scan_complete", "paused", "failed", "complete", "transferring"):
            return False
        counts = await self.db.get_message_counts(job_id)
        if not (counts.get("pending") or counts.get("failed")):
            return False
        self._stop_event.clear()
        self._pause_event.set()
        self._current_job_id = job_id
        await self.db.update_job_status(job_id, "transferring")
        self._task = asyncio.create_task(self._transfer_loop(job))
        return True

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def wait_until_done(self):
        if self._task:
            await self._task

    async def _scan_loop(self, job: dict):
        job_id, source_id = job["id"], job["source_id"]
        visited = 0
        batch = []
        offset_id = 0
        await self._notify(job_id, "Full channel history scan started (newest to oldest).")
        try:
            # Re-open history from the last processed message if Telegram returns FloodWait.
            # offset_id is exclusive in get_chat_history, preventing a gap at the resume point.
            while not self._stop_event.is_set():
                try:
                    async for msg in self.uc.get_chat_history(source_id, offset_id=offset_id):
                        if self._stop_event.is_set():
                            break
                        offset_id = msg.id
                        if getattr(msg, "service", False) or classify_message(msg) == "service":
                            continue
                        batch.append((job_id, msg.id, classify_message(msg), msg.caption or msg.text))
                        visited += 1
                        if len(batch) >= max(1, int(config.SCAN_BATCH_SIZE)):
                            await self.db.upsert_messages(batch)
                            batch.clear()
                            if visited % 500 == 0:
                                await self._notify(job_id, f"Scanned {visited:,} messages in this run…")
                    break
                except FloodWait as exc:
                    wait_seconds = int(getattr(exc, "value", 1)) + 1
                    await self._notify(job_id, f"Telegram scan rate limit. Waiting {wait_seconds}s, then continuing from the last message…")
                    await asyncio.sleep(wait_seconds)
            if batch:
                await self.db.upsert_messages(batch)
            counts = await self.db.get_message_counts(job_id)
            total = counts.get("total") or 0
            if self._stop_event.is_set():
                await self.db.update_job_counts(job_id, total=total, scanned=total)
                await self.db.update_job_status(job_id, "scan_paused")
                await self._notify(job_id, f"Scan stopped safely. {total:,} unique messages are saved. Press SCAN FULL HISTORY to continue.")
                return
            await self.db.update_job_counts(job_id, total=total, scanned=total)
            await self.db.update_job_status(job_id, "scan_complete")
            await self._notify(job_id, f"Full history scan complete. {total:,} unique messages queued. Use START TRANSFER.")
            logger.info("Job %s scan complete: %s messages", job_id, total)
        except (ChannelPrivate, ChatAdminRequired, PeerIdInvalid) as exc:
            await self.db.update_job_status(job_id, "error")
            await self._notify(job_id, f"Cannot access source channel: {exc}")
            logger.exception("Job %s source access failed", job_id)
        except Exception as exc:
            await self.db.update_job_status(job_id, "error")
            await self._notify(job_id, f"Scan error: {exc}")
            logger.exception("Job %s scan crashed", job_id)

    async def _transfer_loop(self, job: dict):
        job_id, source_id, dest_id = job["id"], job["source_id"], job["dest_id"]
        interval = max(0.0, float(config.TRANSFER_INTERVAL))
        batch_size = max(1, int(config.BATCH_SIZE))
        await self._notify(job_id, f"Transfer started. Batch size {batch_size}; batch wait {interval:g}s. Telegram rate limits are respected.")
        try:
            while not self._stop_event.is_set():
                await self._pause_event.wait()
                if self._stop_event.is_set():
                    break
                pending = await self.db.get_pending_messages(job_id, limit=batch_size)
                if not pending:
                    retryable = await self.db.get_failed_messages(job_id, max_retries=config.MAX_RETRIES)
                    if not retryable:
                        break
                    pending = retryable[:batch_size]
                for row in pending:
                    await self._pause_event.wait()
                    if self._stop_event.is_set():
                        break
                    await self._copy_one(job_id, source_id, dest_id, row)
                    await asyncio.sleep(max(0.0, config.PER_MESSAGE_DELAY))
                counts = await self.db.get_message_counts(job_id)
                await self.db.update_job_counts(job_id, transferred=counts.get("transferred", 0), failed=counts.get("failed", 0))
                await self._notify(job_id, f"Progress · Done {counts.get('transferred', 0)} · Pending {counts.get('pending', 0)} · Failed {counts.get('failed', 0)}")
                if counts.get("pending", 0) > 0 or await self.db.get_failed_messages(job_id, max_retries=config.MAX_RETRIES):
                    await asyncio.sleep(interval)
            if self._stop_event.is_set():
                await self.db.update_job_status(job_id, "paused")
                await self._notify(job_id, "Transfer stopped. Progress saved; use RESUME to continue.")
            else:
                counts = await self.db.get_message_counts(job_id)
                final_status = "complete" if counts.get("pending", 0) == 0 and counts.get("failed", 0) == 0 else "failed"
                await self.db.update_job_status(job_id, final_status)
                await self._notify(job_id, f"Transfer finished · Status {final_status}\nTransferred: {counts.get('transferred', 0)}\nFailed: {counts.get('failed', 0)}\nPending: {counts.get('pending', 0)}")
        except Exception as exc:
            await self.db.update_job_status(job_id, "failed")
            await self._notify(job_id, f"Transfer engine error: {exc}")
            logger.exception("Job %s transfer crashed", job_id)

    async def _copy_one(self, job_id: int, source_id: int, dest_id: int, row: dict):
        msg_id = row["source_msg_id"]
        try:
            result = await self._retry_copy(source_id, dest_id, msg_id)
            await self.db.mark_message_transferred(job_id, msg_id, result.id)
        except FileReferenceExpired:
            await self._fallback_upload(job_id, source_id, dest_id, row)
        except (ChannelPrivate, PeerIdInvalid, MessageIdInvalid) as exc:
            local = row.get("local_path")
            if local and os.path.exists(local):
                await self._upload_from_cache(job_id, dest_id, row)
            else:
                await self.db.mark_message_failed(job_id, msg_id, f"Source unavailable: {exc}")
        except Exception as exc:
            await self.db.mark_message_failed(job_id, msg_id, str(exc))
            logger.warning("Job %s message %s failed: %s", job_id, msg_id, exc)

    async def _retry_copy(self, source_id: int, dest_id: int, msg_id: int) -> Message:
        attempts = 0
        while True:
            try:
                return await self.uc.copy_message(chat_id=dest_id, from_chat_id=source_id, message_id=msg_id)
            except FloodWait as exc:
                attempts += 1
                wait_seconds = int(getattr(exc, "value", 1)) + 1
                logger.warning("Telegram FloodWait: sleeping %ss", wait_seconds)
                await asyncio.sleep(wait_seconds)
                if attempts > config.MAX_RETRIES:
                    raise

    async def _fallback_upload(self, job_id: int, source_id: int, dest_id: int, row: dict):
        msg_id = row["source_msg_id"]
        local = None
        try:
            source_msg = await self.uc.get_messages(source_id, msg_id)
            if not source_msg or classify_message(source_msg) not in MEDIA_TYPES:
                raise RuntimeError("Expired reference fallback supports file media only; original copy failed")
            os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
            local = await self.uc.download_media(source_msg, file_name=os.path.join(config.DOWNLOAD_DIR, f"{job_id}_{msg_id}"))
            if not local:
                raise RuntimeError("Telegram media download returned no file")
            await self.db.set_message_local_path(job_id, msg_id, local)
            sent = await self._upload_file(dest_id, local, row["msg_type"], row.get("caption") or "")
            await self.db.mark_message_transferred(job_id, msg_id, sent.id)
            try:
                os.remove(local)
            except OSError:
                pass
        except Exception as exc:
            await self.db.mark_message_failed(job_id, msg_id, f"Media fallback failed: {exc}")
            logger.warning("Fallback failed for message %s: %s", msg_id, exc)

    async def _upload_from_cache(self, job_id: int, dest_id: int, row: dict):
        local = row.get("local_path")
        try:
            sent = await self._upload_file(dest_id, local, row["msg_type"], row.get("caption") or "")
            await self.db.mark_message_transferred(job_id, row["source_msg_id"], sent.id)
            try:
                os.remove(local)
            except OSError:
                pass
        except Exception as exc:
            await self.db.mark_message_failed(job_id, row["source_msg_id"], f"Cached upload failed: {exc}")

    async def _upload_file(self, dest_id: int, local: str, msg_type: str, caption: str):
        common = {"chat_id": dest_id, "caption": caption or None}
        if msg_type == "photo": return await self.uc.send_photo(photo=local, **common)
        if msg_type == "video": return await self.uc.send_video(video=local, **common)
        if msg_type == "audio": return await self.uc.send_audio(audio=local, **common)
        if msg_type == "voice": return await self.uc.send_voice(voice=local, **common)
        if msg_type == "animation": return await self.uc.send_animation(animation=local, **common)
        if msg_type == "sticker": return await self.uc.send_sticker(sticker=local, chat_id=dest_id)
        if msg_type == "video_note": return await self.uc.send_video_note(video_note=local, chat_id=dest_id)
        return await self.uc.send_document(document=local, **common)

    async def _notify(self, job_id: int, text: str):
        if self._progress_callback:
            try:
                await self._progress_callback(job_id, text)
            except Exception as exc:
                logger.debug("Progress callback failed: %s", exc)
        logger.info("[Job %s] %s", job_id, text.replace("\n", " | "))
