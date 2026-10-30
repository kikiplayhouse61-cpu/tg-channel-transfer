import aiosqlite
import asyncio
import logging
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
import config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL,
    source_title    TEXT,
    dest_id         INTEGER NOT NULL,
    dest_title      TEXT,
    status          TEXT    DEFAULT 'idle',
    total_messages  INTEGER DEFAULT 0,
    scanned_count   INTEGER DEFAULT 0,
    transferred_count INTEGER DEFAULT 0,
    failed_count    INTEGER DEFAULT 0,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL,
    source_msg_id   INTEGER NOT NULL,
    dest_msg_id     INTEGER,
    msg_type        TEXT    DEFAULT 'unknown',
    caption         TEXT,
    local_path      TEXT,
    status          TEXT    DEFAULT 'pending',
    error_text      TEXT,
    retry_count     INTEGER DEFAULT 0,
    created_at      TEXT    DEFAULT (datetime('now')),
    transferred_at  TEXT,
    UNIQUE(job_id, source_msg_id),
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_job_status
    ON messages(job_id, status);

CREATE INDEX IF NOT EXISTS idx_messages_job_source
    ON messages(job_id, source_msg_id);
"""


class Database:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        import os
        os.makedirs(config.DATA_DIR, exist_ok=True)
        self._conn = await aiosqlite.connect(config.DB_PATH)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info(f"Database connected: {config.DB_PATH}")

    async def close(self):
        if self._conn:
            await self._conn.close()

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def create_job(
        self,
        source_id: int,
        source_title: str,
        dest_id: int,
        dest_title: str,
    ) -> int:
        async with self._lock:
            cur = await self._conn.execute(
                """
                INSERT INTO jobs (source_id, source_title, dest_id, dest_title, status)
                VALUES (?, ?, ?, ?, 'idle')
                """,
                (source_id, source_title, dest_id, dest_title),
            )
            await self._conn.commit()
            return cur.lastrowid

    async def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        async with self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_latest_job(self) -> Optional[Dict[str, Any]]:
        async with self._conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_job_status(self, job_id: int, status: str):
        async with self._lock:
            await self._conn.execute(
                "UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, job_id),
            )
            await self._conn.commit()

    async def update_job_counts(
        self,
        job_id: int,
        total: Optional[int] = None,
        scanned: Optional[int] = None,
        transferred: Optional[int] = None,
        failed: Optional[int] = None,
    ):
        async with self._lock:
            parts = ["updated_at = datetime('now')"]
            vals: List[Any] = []
            if total is not None:
                parts.append("total_messages = ?")
                vals.append(total)
            if scanned is not None:
                parts.append("scanned_count = ?")
                vals.append(scanned)
            if transferred is not None:
                parts.append("transferred_count = ?")
                vals.append(transferred)
            if failed is not None:
                parts.append("failed_count = ?")
                vals.append(failed)
            vals.append(job_id)
            await self._conn.execute(
                f"UPDATE jobs SET {', '.join(parts)} WHERE id = ?", vals
            )
            await self._conn.commit()

    async def increment_job_counter(self, job_id: int, field: str):
        async with self._lock:
            await self._conn.execute(
                f"UPDATE jobs SET {field} = {field} + 1, updated_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
            await self._conn.commit()

    # ── Messages ──────────────────────────────────────────────────────────────

    async def upsert_messages(self, rows):
        """Insert a batch of scanned messages with one transaction/commit."""
        if not rows:
            return
        async with self._lock:
            await self._conn.executemany(
                """
                INSERT INTO messages (job_id, source_msg_id, msg_type, caption, status)
                VALUES (?, ?, ?, ?, 'pending')
                ON CONFLICT(job_id, source_msg_id) DO NOTHING
                """,
                rows,
            )
            await self._conn.commit()

    async def upsert_message(self, job_id: int, source_msg_id: int, msg_type: str, caption: Optional[str] = None):
        await self.upsert_messages([(job_id, source_msg_id, msg_type, caption)])

    async def reset_failed_messages(self, job_id: int) -> int:
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE messages SET status = 'pending', error_text = NULL, retry_count = 0 WHERE job_id = ? AND status = 'failed'",
                (job_id,),
            )
            await self._conn.commit()
            return cur.rowcount or 0

    async def mark_message_transferred(
        self, job_id: int, source_msg_id: int, dest_msg_id: int
    ):
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE messages
                SET status = 'transferred',
                    dest_msg_id = ?,
                    transferred_at = datetime('now')
                WHERE job_id = ? AND source_msg_id = ?
                """,
                (dest_msg_id, job_id, source_msg_id),
            )
            await self._conn.commit()

    async def mark_message_failed(
        self, job_id: int, source_msg_id: int, error: str
    ):
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE messages
                SET status = 'failed',
                    error_text = ?,
                    retry_count = retry_count + 1
                WHERE job_id = ? AND source_msg_id = ?
                """,
                (error, job_id, source_msg_id),
            )
            await self._conn.commit()

    async def set_message_local_path(
        self, job_id: int, source_msg_id: int, local_path: str
    ):
        async with self._lock:
            await self._conn.execute(
                "UPDATE messages SET local_path = ?, status = 'cached' WHERE job_id = ? AND source_msg_id = ?",
                (local_path, job_id, source_msg_id),
            )
            await self._conn.commit()

    async def get_pending_messages(
        self, job_id: int, limit: int = 50
    ) -> List[Dict[str, Any]]:
        async with self._conn.execute(
            """
            SELECT * FROM messages
            WHERE job_id = ? AND status IN ('pending', 'cached')
            ORDER BY source_msg_id ASC
            LIMIT ?
            """,
            (job_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_failed_messages(
        self, job_id: int, max_retries: int = 5
    ) -> List[Dict[str, Any]]:
        async with self._conn.execute(
            """
            SELECT * FROM messages
            WHERE job_id = ? AND status = 'failed' AND retry_count < ?
            ORDER BY source_msg_id ASC
            """,
            (job_id, max_retries),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def is_message_transferred(
        self, job_id: int, source_msg_id: int
    ) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM messages WHERE job_id = ? AND source_msg_id = ? AND status = 'transferred'",
            (job_id, source_msg_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_message_counts(self, job_id: int) -> Dict[str, int]:
        async with self._conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'transferred' THEN 1 ELSE 0 END) as transferred,
                SUM(CASE WHEN status IN ('pending','cached') THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'cached' THEN 1 ELSE 0 END) as cached
            FROM messages WHERE job_id = ?
            """,
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}

    # ── Settings ──────────────────────────────────────────────────────────────

    async def set_setting(self, key: str, value: str):
        async with self._lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                (key, value),
            )
            await self._conn.commit()

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else default
