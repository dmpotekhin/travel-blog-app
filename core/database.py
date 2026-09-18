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

-- ---------------------------------------------------------------------------
-- Visual Narrative Studio (ADR-106): a storyboard is the visual narrative of a
-- city. Every (re)generation writes a new version, so history is immutable and
-- the draft/approved/archived machine in core/models.py stays meaningful.
-- CREATE TABLE IF NOT EXISTS keeps the migration idempotent: an existing
-- database picks the tables up on the next connect() and stored data is
-- untouched.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS storyboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id INTEGER NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    logline TEXT NOT NULL DEFAULT '',
    narrative_arc TEXT NOT NULL DEFAULT '',
    emotional_journey TEXT NOT NULL DEFAULT '',
    primary_theme TEXT NOT NULL DEFAULT '',
    secondary_themes_json TEXT NOT NULL DEFAULT '[]',
    target_platforms_json TEXT NOT NULL DEFAULT '[]',
    accessibility_notes_json TEXT NOT NULL DEFAULT '[]',
    cultural_sensitivity_notes_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(city_id, version)
);

CREATE TABLE IF NOT EXISTS narrative_beats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storyboard_id INTEGER NOT NULL REFERENCES storyboards(id) ON DELETE CASCADE,
    beat_type TEXT NOT NULL DEFAULT 'setup',
    "order" INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    emotional_tone TEXT NOT NULL DEFAULT '',
    visual_goal TEXT NOT NULL DEFAULT '',
    photo_paths_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS storyboard_shots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storyboard_id INTEGER NOT NULL REFERENCES storyboards(id) ON DELETE CASCADE,
    photo_path TEXT NOT NULL,
    "order" INTEGER NOT NULL DEFAULT 0,
    caption TEXT NOT NULL DEFAULT '',
    alt_text TEXT NOT NULL DEFAULT '',
    crop_recommendation TEXT NOT NULL DEFAULT '',
    focus_point TEXT NOT NULL DEFAULT '',
    visual_metaphor TEXT NOT NULL DEFAULT '',
    pacing_weight REAL NOT NULL DEFAULT 1.0,
    is_hero_image INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_storyboards_city ON storyboards(city_id, version);
CREATE INDEX IF NOT EXISTS idx_storyboards_status ON storyboards(status);
CREATE INDEX IF NOT EXISTS idx_beats_storyboard ON narrative_beats(storyboard_id, "order");
CREATE INDEX IF NOT EXISTS idx_shots_storyboard ON storyboard_shots(storyboard_id, "order");
"""

# Whitelists for storyboard writes (Visual Narrative Studio): dynamic SQL uses
# column names, so only names from these sets may reach an UPDATE statement.
_STORYBOARD_WRITABLE = frozenset(
    {
        "title",
        "logline",
        "narrative_arc",
        "emotional_journey",
        "primary_theme",
        "secondary_themes_json",
        "target_platforms_json",
        "accessibility_notes_json",
        "cultural_sensitivity_notes_json",
        "status",
        "version",
    }
)
_SHOT_WRITABLE = frozenset(
    {
        "photo_path",
        "order",
        "caption",
        "alt_text",
        "crop_recommendation",
        "focus_point",
        "visual_metaphor",
        "pacing_weight",
        "is_hero_image",
    }
)

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
        if "status" in updates:
            # ADR-103: every status write goes through the Publication state
            # machine. This stops the publish path from skipping straight from
            # SCHEDULED/PENDING to PUBLISHED via the generic setter (F4).
            current = await self.get_published(publication_id)
            if current is None:
                raise NotFoundError(f"Publication {publication_id} not found")
            check_transition(
                current.status.value, updates["status"],
                _PUBLICATION_TRANSITIONS, entity="publication",
            )
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [publication_id]
        async with self.transaction() as conn:
            await conn.execute(f"UPDATE published SET {sets} WHERE id = ?", values)
        return await self.get_published(publication_id)

    async def claim_publication(self, publication_id: int) -> Optional[m.Publication]:
        """Atomically claim a waiting publication (ADR-103).

        ``UPDATE ... WHERE status IN ('scheduled','pending')`` is a compare-and-
        swap on the status column: exactly one concurrent tick sees a non-zero
        ``rowcount`` and wins the claim (status -> processing); the loser gets
        ``None`` and must skip, so the same row is never published twice (F6).
        """
        async with self.transaction() as conn:
            cur = await conn.execute(
                "UPDATE published SET status = ? WHERE id = ? AND status IN (?, ?)",
                (
                    m.PublicationStatus.PROCESSING.value,
                    publication_id,
                    m.PublicationStatus.SCHEDULED.value,
                    m.PublicationStatus.PENDING.value,
                ),
            )
            if cur.rowcount == 0:
                return None
        return await self.get_published(publication_id)

    async def update_publication_status(self, publication_id: int, status: str) -> Optional[m.Publication]:
        # ADR-103: update_publication already fetches the row and runs the state
        # machine (check_transition), so delegate — doing it here too would SELECT
        # + validate the transition twice for every single status write.
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

    # -- storyboards (Visual Narrative Studio, ADR-106) ---------------------

    async def add_storyboard(self, storyboard: m.Storyboard) -> m.Storyboard:
        """Insert one storyboard version (unique per city+version)."""
        now = _iso(utcnow())
        row_id = await self._insert(
            """
            INSERT INTO storyboards (city_id, title, logline, narrative_arc, emotional_journey,
                                     primary_theme, secondary_themes_json, target_platforms_json,
                                     accessibility_notes_json, cultural_sensitivity_notes_json,
                                     status, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                storyboard.city_id,
                storyboard.title,
                storyboard.logline,
                storyboard.narrative_arc,
                storyboard.emotional_journey,
                storyboard.primary_theme,
                storyboard.secondary_themes_json,
                storyboard.target_platforms_json,
                storyboard.accessibility_notes_json,
                storyboard.cultural_sensitivity_notes_json,
                storyboard.status.value,
                storyboard.version,
                _iso(storyboard.created_at) or now,
                _iso(storyboard.updated_at) or now,
            ),
        )
        return await self._require_storyboard(row_id)

    async def add_storyboard_graph(
        self,
        storyboard: m.Storyboard,
        beats: Optional[List[m.NarrativeBeat]] = None,
        shots: Optional[List[m.StoryboardShot]] = None,
    ) -> m.Storyboard:
        """Insert a storyboard with its beats and shots in one transaction.

        One atomic write keeps a half-saved storyboard (beats without shots)
        impossible — the studio always writes whole versions.
        """
        now = _iso(utcnow())
        try:
            async with self.transaction() as conn:
                cur = await conn.execute(
                    """
                    INSERT INTO storyboards (city_id, title, logline, narrative_arc, emotional_journey,
                                             primary_theme, secondary_themes_json, target_platforms_json,
                                             accessibility_notes_json, cultural_sensitivity_notes_json,
                                             status, version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        storyboard.city_id,
                        storyboard.title,
                        storyboard.logline,
                        storyboard.narrative_arc,
                        storyboard.emotional_journey,
                        storyboard.primary_theme,
                        storyboard.secondary_themes_json,
                        storyboard.target_platforms_json,
                        storyboard.accessibility_notes_json,
                        storyboard.cultural_sensitivity_notes_json,
                        storyboard.status.value,
                        storyboard.version,
                        _iso(storyboard.created_at) or now,
                        _iso(storyboard.updated_at) or now,
                    ),
                )
                storyboard_id = int(cur.lastrowid or 0)
                for beat in beats or []:
                    await conn.execute(
                        """
                        INSERT INTO narrative_beats (storyboard_id, beat_type, "order", title,
                                                     description, emotional_tone, visual_goal,
                                                     photo_paths_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            storyboard_id,
                            beat.beat_type.value,
                            beat.order,
                            beat.title,
                            beat.description,
                            beat.emotional_tone,
                            beat.visual_goal,
                            beat.photo_paths_json,
                        ),
                    )
                for shot in shots or []:
                    await conn.execute(
                        """
                        INSERT INTO storyboard_shots (storyboard_id, photo_path, "order", caption,
                                                      alt_text, crop_recommendation, focus_point,
                                                      visual_metaphor, pacing_weight, is_hero_image)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            storyboard_id,
                            shot.photo_path,
                            shot.order,
                            shot.caption,
                            shot.alt_text,
                            shot.crop_recommendation,
                            shot.focus_point,
                            shot.visual_metaphor,
                            float(shot.pacing_weight),
                            int(bool(shot.is_hero_image)),
                        ),
                    )
        except aiosqlite.IntegrityError as exc:
            raise DuplicateError(f"Duplicate storyboard version: {exc}") from exc
        return await self._require_storyboard(storyboard_id)

    async def _require_storyboard(self, storyboard_id: int) -> m.Storyboard:
        storyboard = await self.get_storyboard(storyboard_id)
        if storyboard is None:  # pragma: no cover - the row was just written
            raise DatabaseError(f"Storyboard {storyboard_id} not found after write")
        return storyboard

    async def get_storyboard(self, storyboard_id: int) -> Optional[m.Storyboard]:
        row = await self._fetchone("SELECT * FROM storyboards WHERE id = ?", (storyboard_id,))
        return _storyboard_from_row(row)

    async def get_city_storyboard(
        self, city_id: int, status: Optional[str] = None
    ) -> Optional[m.Storyboard]:
        """Latest storyboard version of a city (optionally filtered by status)."""
        sql = "SELECT * FROM storyboards WHERE city_id = ?"
        params: List = [city_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY version DESC LIMIT 1"
        return _storyboard_from_row(await self._fetchone(sql, params))

    async def list_storyboards(
        self, city_id: Optional[int] = None, status: Optional[str] = None
    ) -> List[m.Storyboard]:
        sql = "SELECT * FROM storyboards"
        where: List[str] = []
        params: List = []
        if city_id is not None:
            where.append("city_id = ?")
            params.append(city_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY city_id, version DESC"
        rows = await self._fetchall(sql, params)
        return [sb for sb in (_storyboard_from_row(r) for r in rows) if sb is not None]

    async def next_storyboard_version(self, city_id: int) -> int:
        """Version to use for the next storyboard of this city (1-based)."""
        row = await self._fetchone(
            "SELECT MAX(version) AS v FROM storyboards WHERE city_id = ?", (city_id,)
        )
        return int(row["v"] or 0) + 1 if row else 1

    async def update_storyboard(self, storyboard_id: int, **fields: object) -> Optional[m.Storyboard]:
        """Update whitelisted columns; unknown names raise ValueError (no dynamic SQL)."""
        sets: List[str] = []
        values: List = []
        for key, value in fields.items():
            if key not in _STORYBOARD_WRITABLE:
                raise ValueError(f"Cannot update storyboard column {key!r}")
            sets.append(f"{key} = ?")
            values.append(value.value if isinstance(value, m.StoryboardStatus) else value)
        if not sets:
            return await self.get_storyboard(storyboard_id)
        sets.append("updated_at = ?")
        values.append(_iso(utcnow()))
        values.append(storyboard_id)
        async with self.transaction() as conn:
            await conn.execute(f"UPDATE storyboards SET {', '.join(sets)} WHERE id = ?", tuple(values))
        return await self.get_storyboard(storyboard_id)

    async def update_storyboard_status(self, storyboard_id: int, status: str) -> Optional[m.Storyboard]:
        """Move a storyboard through its state machine (draft/approved/archived)."""
        current = await self.get_storyboard(storyboard_id)
        if current is None:
            raise NotFoundError(f"Storyboard {storyboard_id} not found")
        m.storyboard_transition(current.status.value, status)
        return await self.update_storyboard(storyboard_id, status=status)

    async def add_beat(self, beat: m.NarrativeBeat) -> m.NarrativeBeat:
        if beat.storyboard_id is None:
            raise DatabaseError("NarrativeBeat.storyboard_id is required")
        beat.id = await self._insert(
            """
            INSERT INTO narrative_beats (storyboard_id, beat_type, "order", title, description,
                                         emotional_tone, visual_goal, photo_paths_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                beat.storyboard_id,
                beat.beat_type.value,
                beat.order,
                beat.title,
                beat.description,
                beat.emotional_tone,
                beat.visual_goal,
                beat.photo_paths_json,
            ),
        )
        return beat

    async def get_beats(self, storyboard_id: int) -> List[m.NarrativeBeat]:
        rows = await self._fetchall(
            'SELECT * FROM narrative_beats WHERE storyboard_id = ? ORDER BY "order", id',
            (storyboard_id,),
        )
        return [b for b in (_beat_from_row(r) for r in rows) if b is not None]

    async def add_shot(self, shot: m.StoryboardShot) -> m.StoryboardShot:
        if shot.storyboard_id is None:
            raise DatabaseError("StoryboardShot.storyboard_id is required")
        shot.id = await self._insert(
            """
            INSERT INTO storyboard_shots (storyboard_id, photo_path, "order", caption, alt_text,
                                          crop_recommendation, focus_point, visual_metaphor,
                                          pacing_weight, is_hero_image)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shot.storyboard_id,
                shot.photo_path,
                shot.order,
                shot.caption,
                shot.alt_text,
                shot.crop_recommendation,
                shot.focus_point,
                shot.visual_metaphor,
                float(shot.pacing_weight),
                int(bool(shot.is_hero_image)),
            ),
        )
        return shot

    async def get_shots(self, storyboard_id: int) -> List[m.StoryboardShot]:
        rows = await self._fetchall(
            'SELECT * FROM storyboard_shots WHERE storyboard_id = ? ORDER BY "order", id',
            (storyboard_id,),
        )
        return [s for s in (_shot_from_row(r) for r in rows) if s is not None]

    async def get_shot(self, shot_id: int) -> Optional[m.StoryboardShot]:
        row = await self._fetchone("SELECT * FROM storyboard_shots WHERE id = ?", (shot_id,))
        return _shot_from_row(row)

    async def replace_shot_order(
        self, storyboard_id: int, ordered_shot_ids: List[int]
    ) -> List[m.StoryboardShot]:
        """Apply a new visual order: ``order`` becomes the index in the given list."""
        async with self.transaction() as conn:
            for position, shot_id in enumerate(ordered_shot_ids):
                await conn.execute(
                    'UPDATE storyboard_shots SET "order" = ? WHERE id = ? AND storyboard_id = ?',
                    (position, shot_id, storyboard_id),
                )
        return await self.get_shots(storyboard_id)

    async def update_shot(self, shot_id: int, **fields: object) -> Optional[m.StoryboardShot]:
        """Update whitelisted shot columns (alt-text, captions, order, hero flag...)."""
        sets: List[str] = []
        values: List = []
        for key, value in fields.items():
            if key not in _SHOT_WRITABLE:
                raise ValueError(f"Cannot update shot column {key!r}")
            sets.append(f"{key} = ?")
            values.append(int(bool(value)) if key == "is_hero_image" else value)
        if not sets:
            row = await self._fetchone("SELECT * FROM storyboard_shots WHERE id = ?", (shot_id,))
            return _shot_from_row(row)
        values.append(shot_id)
        async with self.transaction() as conn:
            await conn.execute(f"UPDATE storyboard_shots SET {', '.join(sets)} WHERE id = ?", tuple(values))
        row = await self._fetchone("SELECT * FROM storyboard_shots WHERE id = ?", (shot_id,))
        return _shot_from_row(row)

    async def replace_shots(
        self, storyboard_id: int, shots: List[m.StoryboardShot]
    ) -> List[m.StoryboardShot]:
        """Rewrite the whole shot list atomically (used by reorder/hero-image edits)."""
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM storyboard_shots WHERE storyboard_id = ?", (storyboard_id,))
            for shot in shots:
                await conn.execute(
                    """
                    INSERT INTO storyboard_shots (storyboard_id, photo_path, "order", caption, alt_text,
                                                  crop_recommendation, focus_point, visual_metaphor,
                                                  pacing_weight, is_hero_image)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        storyboard_id,
                        shot.photo_path,
                        shot.order,
                        shot.caption,
                        shot.alt_text,
                        shot.crop_recommendation,
                        shot.focus_point,
                        shot.visual_metaphor,
                        float(shot.pacing_weight),
                        int(bool(shot.is_hero_image)),
                    ),
                )
        return await self.get_shots(storyboard_id)

    async def delete_storyboard_rows(self, storyboard_id: int) -> None:
        """Drop beats and shots of a storyboard (the storyboard row stays)."""
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM narrative_beats WHERE storyboard_id = ?", (storyboard_id,))
            await conn.execute("DELETE FROM storyboard_shots WHERE storyboard_id = ?", (storyboard_id,))


# --------------------------------------------------------------------------
# Row -> model converters
# --------------------------------------------------------------------------


def _storyboard_from_row(row: Optional[dict]) -> Optional[m.Storyboard]:
    if row is None:
        return None
    return m.Storyboard(**row)


def _beat_from_row(row: Optional[dict]) -> Optional[m.NarrativeBeat]:
    if row is None:
        return None
    return m.NarrativeBeat(**row)


def _shot_from_row(row: Optional[dict]) -> Optional[m.StoryboardShot]:
    if row is None:
        return None
    return m.StoryboardShot(**row)


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
