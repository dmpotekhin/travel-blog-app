"""Async SQLite persistence layer — the source of truth (section 4 / 42-51).

All DB access goes through this repository. UI and business logic never touch
SQL directly. Uses aiosqlite with WAL mode (concurrent readers), foreign keys
enforced, and an asyncio lock for the shared connection.

Duplicate protection / idempotency is enforced by UNIQUE constraints and
``IntegrityError`` handling, so a city or publication is never created twice.
"""
from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

import aiosqlite

from . import models as m
from .config import get_settings
from .exceptions import DatabaseError, DuplicateError, NotFoundError
from .models import (
    CityStatus,
    DraftStatus,
    PublicationStatus,
    _CITY_TRANSITIONS,
    _DRAFT_TRANSITIONS,
    _PUBLICATION_TRANSITIONS,
    check_transition,
    vibecoding_transition,
)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "travel_blog.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '',
    year INTEGER,
    latitude REAL,
    longitude REAL,
    folder_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(name, country, year)
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    modified_at TEXT,
    sha256 TEXT NOT NULL DEFAULT '',
    taken_at TEXT,
    latitude REAL,
    longitude REAL,
    country TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    year INTEGER,
    scan_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(path),
    FOREIGN KEY(city_id) REFERENCES cities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    photos_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    content_version INTEGER NOT NULL DEFAULT 1,
    ai_provider TEXT NOT NULL DEFAULT '',
    ai_model TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(city_id, platform),
    FOREIGN KEY(city_id) REFERENCES cities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS published (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    scheduled_at TEXT,
    published_at TEXT,
    content_version INTEGER NOT NULL DEFAULT 1,
    error_message TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    UNIQUE(city_id, platform),
    FOREIGN KEY(city_id) REFERENCES cities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pending_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id INTEGER,
    task_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS gemini_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    requests_count INTEGER NOT NULL DEFAULT 0,
    successful_requests INTEGER NOT NULL DEFAULT 0,
    failed_requests INTEGER NOT NULL DEFAULT 0,
    rate_limit_errors INTEGER NOT NULL DEFAULT 0,
    server_errors INTEGER NOT NULL DEFAULT 0,
    UNIQUE(date)
);

CREATE TABLE IF NOT EXISTS ai_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    UNIQUE(image_hash, provider, model, prompt_hash)
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL DEFAULT '',
    city_id INTEGER,
    task_id INTEGER,
    operation TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    duration REAL,
    status TEXT NOT NULL DEFAULT 'started',
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS vibecoding_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    prompt_text TEXT NOT NULL DEFAULT '',
    image_prompt TEXT NOT NULL DEFAULT '',
    generated_text TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    platform_status TEXT NOT NULL DEFAULT '{}',
    created_at TEXT,
    published_at TEXT
);
"""

# Whitelist of counter columns for gemini_stats (prevents SQL injection).
_GEMINI_COUNTERS = frozenset(
    {
        "requests_count",
        "successful_requests",
        "failed_requests",
        "rate_limit_errors",
        "server_errors",
    }
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Database:
    """Async repository over a single SQLite file (WAL). """

    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = str(path or DEFAULT_DB_PATH)
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = None  # asyncio.Lock created lazily inside an event loop

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Open the connection and create the schema if needed."""
        if self._conn is not None:
            return
        try:
            conn = await aiosqlite.connect(self.path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.executescript(_SCHEMA)
            await conn.commit()
            self._conn = conn
            if self._lock is None:
                # Lock bound to the running event loop.
                import asyncio

                self._lock = asyncio.Lock()
        except Exception as exc:  # pragma: no cover - defensive
            raise DatabaseError(f"Failed to connect to DB {self.path}: {exc}", cause=exc) from exc

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def __await__(self):
        # Allow ``db = await Database(path)`` style usage.
        async def _init():
            db = Database(self.path)
            await db.connect()
            return db

        return _init().__await__()

    # -- low-level helpers ------------------------------------------------

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("Database not connected. Call connect() first.")
        return self._conn

    async def _fetchone(self, sql: str, params: Iterator = ()) -> Optional[dict]:
        async with self._lock:
            cur = await self._conn.execute(sql, tuple(params))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _fetchall(self, sql: str, params: Iterator = ()) -> List[dict]:
        async with self._lock:
            cur = await self._conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    @contextlib.asynccontextmanager
    async def transaction(self):
        """Run a block atomically; commits on success, rolls back on error."""
        async with self._lock:
            try:
                yield self._conn
            except Exception:
                await self._conn.rollback()
                raise
            else:
                await self._conn.commit()

    async def _insert(self, sql: str, params: Iterator = ()) -> int:
        """Insert and return lastrowid. Raises DuplicateError on conflict."""
        async with self.transaction() as conn:
            try:
                cur = await conn.execute(sql, tuple(params))
            except aiosqlite.IntegrityError as exc:
                raise DuplicateError(f"Duplicate record: {exc}") from exc
            return cur.lastrowid

    # -- cities -----------------------------------------------------------

    async def add_city(self, city: m.City) -> m.City:
        now = _iso(utcnow())
        row_id = await self._insert(
            """
            INSERT INTO cities (name, country, year, latitude, longitude,
                                folder_path, status, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                city.name,
                city.country,
                city.year,
                city.latitude,
                city.longitude,
                city.folder_path,
                city.status.value,
                city.priority,
                now,
                now,
            ),
        )
        return await self.get_city(row_id)

    async def get_city(self, city_id: int) -> Optional[m.City]:
        row = await self._fetchone("SELECT * FROM cities WHERE id = ?", (city_id,))
        return _city_from_row(row)

    async def get_city_by_name(self, name: str, year: Optional[int] = None) -> Optional[m.City]:
        params: List[Any] = [name]
        sql = "SELECT * FROM cities WHERE name = ?"
        if year is not None:
            sql += " AND year = ?"
            params.append(year)
        sql += " ORDER BY id LIMIT 1"
        return _city_from_row(await self._fetchone(sql, params))

    async def get_all_cities(self) -> List[m.City]:
        rows = await self._fetchall("SELECT * FROM cities ORDER BY priority DESC, name")
        return [_city_from_row(r) for r in rows]

    async def get_queue(self, limit: Optional[int] = None) -> List[m.City]:
        sql = "SELECT * FROM cities WHERE status = 'queued' ORDER BY priority DESC, name"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = await self._fetchall(sql)
        return [_city_from_row(r) for r in rows]

    async def update_city_status(self, city_id: int, status: str) -> Optional[m.City]:
        current = await self.get_city(city_id)
        if current is None:
            raise NotFoundError(f"City {city_id} not found")
        check_transition(
            current.status.value, status, _CITY_TRANSITIONS, entity="city"
        )
        now = _iso(utcnow())
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE cities SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, city_id),
            )
        return await self.get_city(city_id)

    async def update_city_priority(self, city_id: int, priority: int) -> Optional[m.City]:
        now = _iso(utcnow())
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE cities SET priority = ?, updated_at = ? WHERE id = ?",
                (priority, now, city_id),
            )
        return await self.get_city(city_id)

    async def set_city_coordinates(
        self, city_id: int, latitude: float, longitude: float
    ) -> Optional[m.City]:
        now = _iso(utcnow())
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE cities SET latitude = ?, longitude = ?, updated_at = ? WHERE id = ?",
                (latitude, longitude, now, city_id),
            )
        return await self.get_city(city_id)

    async def delete_city(self, city_id: int) -> bool:
        async with self.transaction() as conn:
            cur = await conn.execute("DELETE FROM cities WHERE id = ?", (city_id,))
        return cur.rowcount > 0

    async def get_cities_by_status(
        self, status: str, limit: Optional[int] = None, offset: int = 0
    ) -> List[m.City]:
        """Select cities by status, ordered by priority DESC then year ASC."""
        sql = "SELECT * FROM cities WHERE status = ? ORDER BY priority DESC, year IS NULL, year ASC, id ASC"
        params: list = [status]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [m.City(**r) for r in await self._fetchall(sql, tuple(params))]

    async def get_queued_cities(
        self, limit: Optional[int] = None, offset: int = 0, require_photos: bool = True
    ) -> List[m.City]:
        """Cities ready to process: status=queued and (optionally) ≥1 scanned photo.

        Ordered by priority DESC, then oldest year first (older trips first),
        then insertion id.
        """
        sql = (
            "SELECT c.* FROM cities c WHERE c.status = ?"
        )
        params: list = ["queued"]
        if require_photos:
            sql += (
                " AND EXISTS (SELECT 1 FROM photos p "
                "             WHERE p.city_id = c.id AND p.scan_status = 'scanned')"
            )
        sql += " ORDER BY c.priority DESC, c.year IS NULL, c.year ASC, c.id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [m.City(**r) for r in await self._fetchall(sql, tuple(params))]

    async def count_cities_by_status(self, status: str) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS c FROM cities WHERE status = ?", (status,))
        return int(row["c"]) if row else 0

    # -- photos -----------------------------------------------------------

    async def add_photo(self, photo: m.Photo) -> m.Photo:
        now = _iso(utcnow())
        row_id = await self._insert(
            """
            INSERT INTO photos (city_id, path, filename, size, modified_at, sha256,
                                taken_at, latitude, longitude, country, city, year,
                                scan_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                photo.city_id,
                photo.path,
                photo.filename,
                photo.size,
                photo.modified_at,
                photo.sha256,
                photo.taken_at,
                photo.latitude,
                photo.longitude,
                photo.country,
                photo.city,
                photo.year,
                photo.scan_status.value,
                now,
                now,
            ),
        )
        return await self.get_photo(row_id)

    async def get_photo(self, photo_id: int) -> Optional[m.Photo]:
        return _photo_from_row(
            await self._fetchone("SELECT * FROM photos WHERE id = ?", (photo_id,))
        )

    async def get_photos_by_city(self, city_id: int) -> List[m.Photo]:
        rows = await self._fetchall(
            "SELECT * FROM photos WHERE city_id = ? ORDER BY taken_at, id", (city_id,)
        )
        return [_photo_from_row(r) for r in rows]

    async def get_photo_by_path(self, path: str) -> Optional[m.Photo]:
        return _photo_from_row(await self._fetchone("SELECT * FROM photos WHERE path = ?", (path,)))

    async def get_photo_by_sha(self, sha256: str) -> Optional[m.Photo]:
        if not sha256:
            return None
        return _photo_from_row(await self._fetchone("SELECT * FROM photos WHERE sha256 = ?", (sha256,)))

    async def get_all_photo_paths(self) -> List[tuple]:
        """Return [(id, path)] for every photo — used for deletion detection."""
        rows = await self._fetchall("SELECT id, path FROM photos")
        return [(r["id"], r["path"]) for r in rows]

    async def update_photo(self, photo_id: int, **fields) -> Optional[m.Photo]:
        allowed = {
            "city_id", "path", "filename", "size", "modified_at", "sha256",
            "taken_at", "latitude", "longitude", "country", "city", "year",
            "scan_status",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_photo(photo_id)
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [_iso(utcnow()), photo_id]
        async with self.transaction() as conn:
            await conn.execute(f"UPDATE photos SET {sets}, updated_at = ? WHERE id = ?", values)
        return await self.get_photo(photo_id)

    async def count_photos(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS c FROM photos")
        return int(row["c"]) if row else 0

    # -- drafts -----------------------------------------------------------

    async def add_draft(self, draft: m.Draft) -> m.Draft:
        now = _iso(utcnow())
        row_id = await self._insert(
            """
            INSERT INTO drafts (city_id, platform, title, content, photos_json,
                                status, content_version, ai_provider, ai_model,
                                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.city_id,
                draft.platform,
                draft.title,
                draft.content,
                draft.photos_json,
                draft.status.value,
                draft.content_version,
                draft.ai_provider,
                draft.ai_model,
                now,
                now,
            ),
        )
        return await self.get_draft(row_id)

    async def get_draft(self, draft_id: int) -> Optional[m.Draft]:
        return _draft_from_row(
            await self._fetchone("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        )

    async def get_drafts(
        self,
        city_id: Optional[int] = None,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        draft_id: Optional[int] = None,
    ) -> List[m.Draft]:
        sql = "SELECT * FROM drafts WHERE 1=1"
        params: List[Any] = []
        if draft_id is not None:
            sql += " AND id = ?"
            params.append(draft_id)
        if city_id is not None:
            sql += " AND city_id = ?"
            params.append(city_id)
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC"
        rows = await self._fetchall(sql, params)
        return [_draft_from_row(r) for r in rows]

    async def update_draft_status(self, draft_id: int, status: str) -> Optional[m.Draft]:
        current = await self.get_draft(draft_id)
        if current is None:
            raise NotFoundError(f"Draft {draft_id} not found")
        check_transition(current.status.value, status, _DRAFT_TRANSITIONS, entity="draft")
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?",
                (status, _iso(utcnow()), draft_id),
            )
        return await self.get_draft(draft_id)

    async def update_draft_content(
        self,
        draft_id: int,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        photos_json: Optional[str] = None,
    ) -> Optional[m.Draft]:
        sets: List[str] = []
        values: List[Any] = []
        if title is not None:
            sets.append("title = ?")
            values.append(title)
        if content is not None:
            sets.append("content = ?")
            values.append(content)
        if photos_json is not None:
            sets.append("photos_json = ?")
            values.append(photos_json)
        sets.append("content_version = content_version + 1")
        sets.append("updated_at = ?")
        values.append(_iso(utcnow()))
        values.append(draft_id)
        async with self.transaction() as conn:
            await conn.execute(
                f"UPDATE drafts SET {', '.join(sets)} WHERE id = ?", values
            )
        return await self.get_draft(draft_id)

    # -- published --------------------------------------------------------

    async def save_published(self, pub: m.Publication) -> m.Publication:
        """Insert a publication record. Idempotent: returns existing on duplicate."""
        existing = await self.get_publication_by_platform(pub.city_id, pub.platform)
        if existing is not None:
            return existing
        now = _iso(utcnow())
        row_id = await self._insert(
            """
            INSERT INTO published (city_id, platform, external_id, url, status,
                                   scheduled_at, published_at, content_version,
                                   error_message, retry_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pub.city_id,
                pub.platform,
                pub.external_id,
                pub.url,
                pub.status.value,
                _iso(pub.scheduled_at),
                _iso(pub.published_at),
                pub.content_version,
                pub.error_message,
                pub.retry_count,
                now,
            ),
        )
        return await self.get_published(row_id)

    async def get_published(self, publication_id: int) -> Optional[m.Publication]:
        return _publication_from_row(
            await self._fetchone("SELECT * FROM published WHERE id = ?", (publication_id,))
        )

    async def get_publication_by_platform(
        self, city_id: int, platform: str
    ) -> Optional[m.Publication]:
        return _publication_from_row(
            await self._fetchone(
                "SELECT * FROM published WHERE city_id = ? AND platform = ?",
                (city_id, platform),
            )
        )

    async def get_all_publications(self) -> List[m.Publication]:
        rows = await self._fetchall("SELECT * FROM published ORDER BY id DESC")
        return [_publication_from_row(r) for r in rows]

    async def update_publication(self, publication_id: int, **fields) -> Optional[m.Publication]:
        allowed = {
            "external_id", "url", "status", "scheduled_at", "published_at",
            "content_version", "error_message", "retry_count",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_published(publication_id)
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [publication_id]
        async with self.transaction() as conn:
            await conn.execute(f"UPDATE published SET {sets} WHERE id = ?", values)
        return await self.get_published(publication_id)

    async def update_publication_status(self, publication_id: int, status: str) -> Optional[m.Publication]:
        current = await self.get_published(publication_id)
        if current is None:
            raise NotFoundError(f"Publication {publication_id} not found")
        check_transition(current.status.value, status, _PUBLICATION_TRANSITIONS, entity="publication")
        return await self.update_publication(publication_id, status=status)

    async def get_publications_by_status(
        self, status: str, limit: Optional[int] = None
    ) -> List[m.Publication]:
        sql = "SELECT * FROM published WHERE status = ? ORDER BY id ASC"
        params: List[Any] = [status]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = await self._fetchall(sql, params)
        return [_publication_from_row(r) for r in rows]

    async def get_due_publications(
        self, now: datetime, limit: Optional[int] = None
    ) -> List[m.Publication]:
        """Publications that are currently waiting and whose scheduled_at has passed."""
        sql = (
            "SELECT * FROM published WHERE status IN ('scheduled','pending') "
            "AND scheduled_at IS NOT NULL AND scheduled_at <= ? "
            "ORDER BY scheduled_at ASC"
        )
        params: List[Any] = [_iso(now)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = await self._fetchall(sql, params)
        return [_publication_from_row(r) for r in rows]

    # -- vibecoding posts -------------------------------------------------

    async def add_vibecoding_post(
        self,
        title: str,
        topic: str,
        prompt_text: str = "",
        image_prompt: str = "",
    ) -> m.VibeCodingPost:
        """Create a new VibeCoding post in ``draft`` status; return the row."""
        now = _iso(utcnow())
        row_id = await self._insert(
            """
            INSERT INTO vibecoding_posts (title, topic, prompt_text, image_prompt,
                                          generated_text, image_url, status, platform_status,
                                          created_at)
            VALUES (?, ?, ?, ?, '', '', 'draft', '{}', ?)
            """,
            (title, topic, prompt_text, image_prompt, now),
        )
        return await self.get_vibecoding_post(row_id)

    async def get_vibecoding_post(self, post_id: int) -> Optional[m.VibeCodingPost]:
        return _vibecoding_from_row(
            await self._fetchone("SELECT * FROM vibecoding_posts WHERE id = ?", (post_id,))
        )

    async def get_vibecoding_posts(
        self, status: Optional[str] = None, limit: int = 20
    ) -> List[m.VibeCodingPost]:
        """List VibeCoding posts, newest-first, optionally filtered by status."""
        sql = "SELECT * FROM vibecoding_posts WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = await self._fetchall(sql, params)
        return [_vibecoding_from_row(r) for r in rows]

    async def update_vibecoding_post(
        self, post_id: int, **fields
    ) -> Optional[m.VibeCodingPost]:
        allowed = {
            "title", "topic", "prompt_text", "image_prompt", "generated_text",
            "image_url", "status", "platform_status", "published_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_vibecoding_post(post_id)
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [post_id]
        async with self.transaction() as conn:
            await conn.execute(f"UPDATE vibecoding_posts SET {sets} WHERE id = ?", values)
        return await self.get_vibecoding_post(post_id)

    async def update_vibecoding_status(
        self, post_id: int, status: str
    ) -> Optional[m.VibeCodingPost]:
        """Update a VibeCoding post status through the validated state machine."""
        current = await self.get_vibecoding_post(post_id)
        if current is None:
            raise NotFoundError(f"VibeCoding post {post_id} not found")
        vibecoding_transition(current.status.value, status)
        return await self.update_vibecoding_post(post_id, status=status)

    async def delete_vibecoding_post(self, post_id: int) -> bool:
        async with self.transaction() as conn:
            cur = await conn.execute("DELETE FROM vibecoding_posts WHERE id = ?", (post_id,))
        return cur.rowcount > 0

    # -- pending tasks ----------------------------------------------------

    async def add_pending_task(self, task: m.PendingTask) -> m.PendingTask:
        now = _iso(utcnow())
        row_id = await self._insert(
            """
            INSERT INTO pending_tasks (city_id, task_type, payload_json, status,
                                       retry_count, next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.city_id,
                task.task_type,
                task.payload_json,
                task.status,
                task.retry_count,
                _iso(task.next_attempt_at),
                now,
                now,
            ),
        )
        return await self.get_pending_task(row_id)

    async def get_pending_task(self, task_id: int) -> Optional[m.PendingTask]:
        return _task_from_row(
            await self._fetchone("SELECT * FROM pending_tasks WHERE id = ?", (task_id,))
        )

    async def get_pending_tasks(
        self, status: Optional[str] = None, due_only: bool = False
    ) -> List[m.PendingTask]:
        sql = "SELECT * FROM pending_tasks WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if due_only:
            sql += " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            params.append(_iso(utcnow()))
        sql += " ORDER BY next_attempt_at NULLS FIRST, id"
        rows = await self._fetchall(sql, params)
        return [_task_from_row(r) for r in rows]

    async def update_pending_task(self, task_id: int, **fields) -> Optional[m.PendingTask]:
        allowed = {"city_id", "task_type", "payload_json", "status", "retry_count", "next_attempt_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self.get_pending_task(task_id)
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [_iso(utcnow()), task_id]
        async with self.transaction() as conn:
            await conn.execute(f"UPDATE pending_tasks SET {sets}, updated_at = ? WHERE id = ?", values)
        return await self.get_pending_task(task_id)

    async def delete_pending_task(self, task_id: int) -> bool:
        async with self.transaction() as conn:
            cur = await conn.execute("DELETE FROM pending_tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0

    # -- gemini stats -----------------------------------------------------

    async def get_gemini_stats(self, date: Optional[str] = None) -> Optional[m.GeminiStats]:
        date = date or _date_str()
        return _stats_from_row(
            await self._fetchone("SELECT * FROM gemini_stats WHERE date = ?", (date,))
        )

    async def increment_gemini_counter(self, date: Optional[str], field: str, delta: int = 1) -> None:
        date = date or _date_str()
        if field not in _GEMINI_COUNTERS:
            raise ValueError(f"Invalid gemini counter: {field!r}")
        async with self.transaction() as conn:
            cur = await conn.execute(
                f"""
                INSERT INTO gemini_stats (date, {field})
                VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET {field} = {field} + ?
                """,
                (date, delta, delta),
            )

    async def reset_gemini_counter(self, date: Optional[str]) -> None:
        date = date or _date_str()
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO gemini_stats (date, requests_count, successful_requests,
                                          failed_requests, rate_limit_errors, server_errors)
                VALUES (?, 0, 0, 0, 0, 0)
                ON CONFLICT(date) DO UPDATE SET
                    requests_count = 0, successful_requests = 0, failed_requests = 0,
                    rate_limit_errors = 0, server_errors = 0
                """,
                (date,),
            )

    # -- AI cache ---------------------------------------------------------

    async def get_ai_cache(
        self, image_hash: str, provider: str, model: str, prompt_hash: str
    ) -> Optional[m.AICacheEntry]:
        return _cache_from_row(
            await self._fetchone(
                """
                SELECT * FROM ai_cache
                WHERE image_hash = ? AND provider = ? AND model = ? AND prompt_hash = ?
                """,
                (image_hash, provider, model, prompt_hash),
            )
        )

    async def save_ai_cache(self, entry: m.AICacheEntry) -> m.AICacheEntry:
        now = _iso(utcnow())
        async with self.transaction() as conn:
            cur = await conn.execute(
                """
                INSERT INTO ai_cache (image_hash, provider, model, prompt_hash, response, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_hash, provider, model, prompt_hash)
                DO UPDATE SET response = excluded.response, created_at = excluded.created_at
                """,
                (
                    entry.image_hash,
                    entry.provider,
                    entry.model,
                    entry.prompt_hash,
                    entry.response,
                    now,
                ),
            )
        return await self.get_ai_cache(entry.image_hash, entry.provider, entry.model, entry.prompt_hash)

    # -- operation logs ---------------------------------------------------

    async def log_operation(self, entry: m.OperationLog) -> m.OperationLog:
        row_id = await self._insert(
            """
            INSERT INTO operation_logs (correlation_id, city_id, task_id, operation,
                                        start_time, end_time, duration, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.correlation_id,
                entry.city_id,
                entry.task_id,
                entry.operation,
                _iso(entry.start_time),
                _iso(entry.end_time),
                entry.duration,
                entry.status,
                entry.error_message,
            ),
        )
        return await self.get_operation_log(row_id)

    async def get_operation_log(self, log_id: int) -> Optional[m.OperationLog]:
        return _log_from_row(
            await self._fetchone("SELECT * FROM operation_logs WHERE id = ?", (log_id,))
        )

    async def get_operation_logs(
        self, limit: int = 50, city_id: Optional[int] = None
    ) -> List[m.OperationLog]:
        sql = "SELECT * FROM operation_logs"
        params: List[Any] = []
        if city_id is not None:
            sql += " WHERE city_id = ?"
            params.append(city_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = await self._fetchall(sql, params)
        return [_log_from_row(r) for r in rows]


# --------------------------------------------------------------------------
# Row -> model converters
# --------------------------------------------------------------------------


def _city_from_row(row: Optional[dict]) -> Optional[m.City]:
    if row is None:
        return None
    return m.City(**row)


def _photo_from_row(row: Optional[dict]) -> Optional[m.Photo]:
    if row is None:
        return None
    return m.Photo(**row)


def _draft_from_row(row: Optional[dict]) -> Optional[m.Draft]:
    if row is None:
        return None
    return m.Draft(**row)


def _publication_from_row(row: Optional[dict]) -> Optional[m.Publication]:
    if row is None:
        return None
    return m.Publication(**row)


def _vibecoding_from_row(row: Optional[dict]) -> Optional[m.VibeCodingPost]:
    if row is None:
        return None
    return m.VibeCodingPost(**row)


def _task_from_row(row: Optional[dict]) -> Optional[m.PendingTask]:
    if row is None:
        return None
    return m.PendingTask(**row)


def _stats_from_row(row: Optional[dict]) -> Optional[m.GeminiStats]:
    if row is None:
        return None
    return m.GeminiStats(**row)


def _cache_from_row(row: Optional[dict]) -> Optional[m.AICacheEntry]:
    if row is None:
        return None
    return m.AICacheEntry(**row)


def _log_from_row(row: Optional[dict]) -> Optional[m.OperationLog]:
    if row is None:
        return None
    return m.OperationLog(**row)


# --------------------------------------------------------------------------
# Default instance + module-level API (the async API from section 51)
# --------------------------------------------------------------------------

_default_db: Optional[Database] = None


def get_db(path: Optional[Union[str, Path]] = None) -> Database:
    """Return the process-wide default Database instance."""
    global _default_db
    if _default_db is None or path is not None:
        db = Database(path)
        if path is None:
            _default_db = db
        return db
    return _default_db


def set_default_db(db: Database) -> None:
    """Replace the default instance (used by tests/app bootstrap)."""
    global _default_db
    _default_db = db


async def init_db(path: Optional[Union[str, Path]] = None) -> Database:
    db = get_db(path)
    await db.connect()
    return db


async def close_db() -> None:
    if _default_db is not None:
        await _default_db.close()


def _db() -> Database:
    return get_db()


# Public async API (thin wrappers over the default instance).


async def add_city(city: m.City) -> m.City:
    return await _db().add_city(city)


async def get_city(city_id: int) -> Optional[m.City]:
    return await _db().get_city(city_id)


async def get_city_by_name(name: str, year: Optional[int] = None) -> Optional[m.City]:
    return await _db().get_city_by_name(name, year)


async def update_city_status(city_id: int, status: str) -> Optional[m.City]:
    return await _db().update_city_status(city_id, status)


async def update_city_priority(city_id: int, priority: int) -> Optional[m.City]:
    return await _db().update_city_priority(city_id, priority)


async def get_queue(limit: Optional[int] = None) -> List[m.City]:
    return await _db().get_queue(limit)


async def get_all_cities() -> List[m.City]:
    return await _db().get_all_cities()


async def get_cities_by_status(status: str, limit: Optional[int] = None, offset: int = 0) -> List[m.City]:
    return await _db().get_cities_by_status(status, limit, offset)


async def get_queued_cities(limit: Optional[int] = None, offset: int = 0, require_photos: bool = True) -> List[m.City]:
    return await _db().get_queued_cities(limit, offset, require_photos)


async def count_cities_by_status(status: str) -> int:
    return await _db().count_cities_by_status(status)


async def delete_city(city_id: int) -> bool:
    return await _db().delete_city(city_id)


async def add_photo(photo: m.Photo) -> m.Photo:
    return await _db().add_photo(photo)


async def get_photos_by_city(city_id: int) -> List[m.Photo]:
    return await _db().get_photos_by_city(city_id)


async def update_photo(photo_id: int, **fields) -> Optional[m.Photo]:
    return await _db().update_photo(photo_id, **fields)


async def add_draft(draft: m.Draft) -> m.Draft:
    return await _db().add_draft(draft)


async def get_draft(draft_id: int) -> Optional[m.Draft]:
    return await _db().get_draft(draft_id)


async def get_drafts(
    city_id: Optional[int] = None,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    draft_id: Optional[int] = None,
) -> List[m.Draft]:
    return await _db().get_drafts(city_id, platform, status, draft_id)


async def add_vibecoding_post(
    title: str, topic: str, prompt_text: str = "", image_prompt: str = ""
) -> m.VibeCodingPost:
    return await _db().add_vibecoding_post(title, topic, prompt_text, image_prompt)


async def get_vibecoding_post(post_id: int) -> Optional[m.VibeCodingPost]:
    return await _db().get_vibecoding_post(post_id)


async def get_vibecoding_posts(
    status: Optional[str] = None, limit: int = 20
) -> List[m.VibeCodingPost]:
    return await _db().get_vibecoding_posts(status, limit)


async def update_vibecoding_post(post_id: int, **fields) -> Optional[m.VibeCodingPost]:
    return await _db().update_vibecoding_post(post_id, **fields)


async def update_vibecoding_status(post_id: int, status: str) -> Optional[m.VibeCodingPost]:
    return await _db().update_vibecoding_status(post_id, status)


async def delete_vibecoding_post(post_id: int) -> bool:
    return await _db().delete_vibecoding_post(post_id)


async def update_draft_status(draft_id: int, status: str) -> Optional[m.Draft]:
    return await _db().update_draft_status(draft_id, status)


async def update_draft_content(
    draft_id: int,
    *,
    title: Optional[str] = None,
    content: Optional[str] = None,
    photos_json: Optional[str] = None,
) -> Optional[m.Draft]:
    return await _db().update_draft_content(draft_id, title=title, content=content, photos_json=photos_json)


async def save_published(pub: m.Publication) -> m.Publication:
    return await _db().save_published(pub)


async def get_published(publication_id: int) -> Optional[m.Publication]:
    return await _db().get_published(publication_id)


async def get_publication_by_platform(city_id: int, platform: str) -> Optional[m.Publication]:
    return await _db().get_publication_by_platform(city_id, platform)


async def get_all_publications() -> List[m.Publication]:
    return await _db().get_all_publications()


async def add_pending_task(task: m.PendingTask) -> m.PendingTask:
    return await _db().add_pending_task(task)


async def get_pending_tasks(status: Optional[str] = None, due_only: bool = False) -> List[m.PendingTask]:
    return await _db().get_pending_tasks(status, due_only)


async def update_pending_task(task_id: int, **fields) -> Optional[m.PendingTask]:
    return await _db().update_pending_task(task_id, **fields)


async def delete_pending_task(task_id: int) -> bool:
    return await _db().delete_pending_task(task_id)


async def get_gemini_stats(date: Optional[str] = None) -> Optional[m.GeminiStats]:
    return await _db().get_gemini_stats(date)


async def increment_gemini_counter(date: Optional[str], field: str, delta: int = 1) -> None:
    return await _db().increment_gemini_counter(date, field, delta)


async def reset_gemini_counter(date: Optional[str]) -> None:
    return await _db().reset_gemini_counter(date)


async def get_ai_cache(
    image_hash: str, provider: str, model: str, prompt_hash: str
) -> Optional[m.AICacheEntry]:
    return await _db().get_ai_cache(image_hash, provider, model, prompt_hash)


async def save_ai_cache(entry: m.AICacheEntry) -> m.AICacheEntry:
    return await _db().save_ai_cache(entry)
