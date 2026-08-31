"""Browser photo ingest — turn uploaded photos into pipeline input.

Uploaded bytes are validated (Pillow), written to the archive folder for the
chosen city/year, the City is created on first sight (status QUEUED, so the
content stage can process it), and every photo is recorded with
``scan_status=SCANNED``. Duplicates are skipped by ``sha256`` (a photo with the
same hash already in the DB is not re-inserted), and non-image files are
rejected.

Used by the Streamlit dashboard (``ui/dashboard.py``). Framework-agnostic: the
``files`` argument is a list of ``(name, bytes)`` tuples.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Sequence, Tuple

from PIL import Image

from core.config import Config
from core.database import Database
from core.models import City, Photo, ScanStatus

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}


def _archive_base(config: Config) -> Path:
    archive = getattr(config, "archive", None)
    raw = (getattr(archive, "path", None) if archive else None) or ""
    if raw:
        return Path(raw).expanduser()
    return Path("uploads")


async def ingest_photos(
    db: Database,
    config: Config,
    city_name: str,
    year: int,
    files: Sequence[Tuple[str, bytes]],
) -> dict:
    city_name = (city_name or "").strip()
    if not city_name:
        return {"error": "city name is required", "added": 0, "duplicates": 0, "rejected": 0}
    if not year:
        return {"error": "year is required", "added": 0, "duplicates": 0, "rejected": 0}

    year = int(year)
    base = _archive_base(config)
    folder = base / f"{city_name}_{year}"
    folder.mkdir(parents=True, exist_ok=True)

    city = await db.get_city_by_name(city_name, year)
    if city is None:
        city = await db.add_city(City(name=city_name, year=year, folder_path=str(folder)))

    added = duplicates = rejected = 0
    for idx, (name, data) in enumerate(files, start=1):
        if not data:
            rejected += 1
            continue
        sha = hashlib.sha256(data).hexdigest()
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
        except Exception:  # noqa: BLE001 - any Pillow failure => not a usable image
            rejected += 1
            continue
        if await db.get_photo_by_sha(sha):
            duplicates += 1
            continue
        ext = Path(name or "photo").suffix.lower() or ".jpg"
        if ext not in _IMAGE_EXTS:
            ext = ".jpg"
        # Globally-unique filename: same sha => same name (dedupe above already
        # caught it), different sha => different name, so no path collisions.
        fname = f"{city_name}_{year}_{sha[:16]}{ext}"
        fpath = folder / fname
        fpath.write_bytes(data)
        await db.add_photo(
            Photo(
                city_id=city.id,
                path=str(fpath),
                filename=fname,
                size=len(data),
                sha256=sha,
                city=city_name,
                year=year,
                scan_status=ScanStatus.SCANNED,
            )
        )
        added += 1

    return {
        "city_id": city.id,
        "city": city_name,
        "year": year,
        "folder": str(folder),
        "added": added,
        "duplicates": duplicates,
        "rejected": rejected,
    }
