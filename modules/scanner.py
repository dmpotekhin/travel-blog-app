"""PHOTO SCANNER (section 15-18).

Recursive + incremental scan of a (possibly 1 TB) photo archive:

* never loads the archive into RAM — walks and processes folder by folder;
* incremental: unchanged files are skipped (size + mtime), changed are
  re-hashed/updated, new files are added, deleted files are marked `missing`;
* EXIF (date, GPS, camera) extracted via Pillow's modern ``getexif()``;
* city/country/year resolved from reverse-geocoded GPS when available, else
  from the folder / filename;
* one corrupted file never stops the scan — it is logged and marked `failed`;
* duplicate image content (same SHA-256) is detected and marked `duplicate`.

Blocking IO (hashing, EXIF, reverse-geocode) is offloaded with
``asyncio.to_thread`` so the event loop stays responsive.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from PIL import Image
from PIL.ExifTags import GPSTAGS
from loguru import logger

from core.config import get_settings
from core.database import Database
from core.models import City, Photo, ScanStatus

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif",
}
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

ProgressCb = Callable[[int, int, int, int, str], None]


# --- low-level helpers -----------------------------------------------------

def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def compute_sha256(path: str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _dms_to_decimal(dms, ref: str) -> Optional[float]:
    try:
        degrees = float(dms[0])
        minutes = float(dms[1])
        seconds = float(dms[2])
        dec = degrees + minutes / 60.0 + seconds / 3600.0
        if ref in ("S", "W"):
            dec = -dec
        return dec
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None


def _parse_exif_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def extract_exif(path: str) -> dict:
    """Return normalized EXIF: taken_at, latitude, longitude, camera, year.

    Brave against corrupted / non-image files. Sets ``_valid`` to ``False`` for
    files that Pillow cannot open as an image so the scanner can mark them
    ``failed`` instead of treating them as usable photos (section 15 / 67).
    """
    data: dict = {}
    valid = True
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if exif:
                make = exif.get(0x010F) or ""
                model = exif.get(0x0110) or ""
                camera = (f"{make} {model}").strip()
                if camera:
                    data["camera"] = camera
                # EXIF date: DateTimeOriginal preferred, else DateTime
                raw = exif.get(0x9003) or exif.get(0x0132)
                dt = _parse_exif_datetime(raw)
            else:
                dt = None

            if dt is not None:
                data["taken_at"] = dt
                data["year"] = dt.year

            gps_ifd = exif.get_ifd(0x8825) if exif else {}
            if gps_ifd:
                gps = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
                lat = _dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef", "N"))
                lon = _dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef", "E"))
                if lat is not None and lon is not None:
                    data["latitude"] = lat
                    data["longitude"] = lon
    except Exception as exc:  # noqa: BLE001 — a broken file must not stop the scan
        logger.debug("EXIF skipped for {}: {}", path, exc)
        valid = False
    data["_valid"] = valid
    return data


# --- city detection --------------------------------------------------------

class CityDetector:
    """Resolve city/country from GPS (offline reverse_geocoder) or folder name.

    ``reverse_geocoder`` bundles a small offline cities DB but depends on
    scikit-learn, which can fail to build on machines without a C toolchain
    (ARCHITECTURE R2). It is therefore *optional* — if unavailable we fall back
    to folder / filename detection and log a warning.
    """

    def __init__(self):
        self._rg = self._load_reverse_geocoder()

    @staticmethod
    def _load_reverse_geocoder():
        try:
            import reverse_geocoder as rg
            rg.search((0, 0))  # trigger any dataset warm-up / import errors now
            logger.info("reverse_geocoder loaded (offline GPS → city)")
            return rg
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reverse_geocoder unavailable ({}); falling back to folder/filename "
                "city detection", exc,
            )
            return None

    def reverse_geocode(self, latitude: Optional[float], longitude: Optional[float]) -> Optional[dict]:
        if self._rg is None or latitude is None or longitude is None:
            return None
        try:
            result = self._rg.search((latitude, longitude))[0]
            return {
                "city": result.get("name"),
                "country": result.get("cc", ""),
                "lat": result.get("lat"),
                "lon": result.get("lon"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("reverse_geocode failed for ({}, {}): {}", latitude, longitude, exc)
            return None

    @staticmethod
    def folder_city_name(folder: Path) -> str:
        name = folder.name.strip()
        # strip a trailing year separator: "Moscow_2019" -> "Moscow"
        m = re.match(r"^(.+?)[ _\-.](19|20)\d{2}$", name)
        if m:
            name = m.group(1)
        # a bare year isn't a city name — climb one level
        if YEAR_RE.fullmatch(name):
            parent = folder.parent.name.strip()
            pm = re.match(r"^(.+?)[ _\-.](19|20)\d{2}$", parent)
            if pm:
                parent = pm.group(1)
            if not YEAR_RE.fullmatch(parent):
                name = parent
        return name or folder.name

    @staticmethod
    def year_from_path(folder: Path) -> Optional[int]:
        for part in folder.parts:
            m = re.search(r"(19|20)\d{2}", part)
            if m and (YEAR_RE.fullmatch(part) or part.endswith(m.group(0))):
                return int(m.group(0))
        return None


# --- scanner ---------------------------------------------------------------

class Scanner:
    def __init__(self, db: Database, archive_path: Optional[str] = None, settings=None):
        self.db = db
        self.settings = settings or get_settings()
        archive = archive_path or self.settings.archive.path
        if not archive:
            raise ValueError("No archive path configured (set app.archive.path or pass archive_path)")
        self.archive_path = Path(archive).expanduser().resolve()
        self.detector = CityDetector()
        self.stats = {"folders": 0, "total_files": 0, "new": 0, "updated": 0,
                      "skipped": 0, "failed": 0, "duplicates": 0, "missing": 0}

    def _find_city_folders(self) -> List[Path]:
        """Return every directory that directly contains ≥1 image file.

        A photo's city folder is the directory that holds it, so nested archives
        (e.g. year/country/city/) work: the deepest image container is the city.
        """
        buckets: List[Path] = []
        for dirpath, _dirnames, filenames in os.walk(self.archive_path):
            if any(is_image(f) for f in filenames):
                buckets.append(Path(dirpath))
        return sorted(buckets)

    def _iter_images(self, folder: Path) -> Iterable[str]:
        for entry in sorted(folder.iterdir()):
            if entry.is_file() and is_image(entry.name):
                yield str(entry)

    async def _resolve_city(self, folder: Path) -> City:
        # sample a few images for GPS / date to seed the city record
        lat = lon = None
        year_by_exif: Optional[int] = None
        for path in list(self._iter_images(folder))[:5]:
            exif = await asyncio.to_thread(extract_exif, path)
            if exif.get("latitude") is not None and exif.get("longitude") is not None:
                lat, lon = exif["latitude"], exif["longitude"]
                if exif.get("year"):
                    year_by_exif = exif["year"]
                break

        geo = self.detector.reverse_geocode(lat, lon)
        city_name = (geo or {}).get("city") or self.detector.folder_city_name(folder)
        country = (geo or {}).get("country") or ""
        # folder year is the stronger trip-grouping signal; EXIF year is per-photo
        year = self.detector.year_from_path(folder) or year_by_exif

        existing = await self.db.get_city_by_name(city_name, year)
        if existing:
            # same city+year already exists (possibly another folder) → reuse
            return existing

        city = City(
            name=city_name, country=country, year=year,
            latitude=lat, longitude=lon, folder_path=str(folder),
        )
        created = await self.db.add_city(city)
        logger.info("New city detected: {} ({}) <- {}", city_name, year, folder)
        return created

    @staticmethod
    def _mtime_dt(mtime: float) -> datetime:
        return datetime.fromtimestamp(mtime, tz=timezone.utc)

    async def _process_image(self, path: str, city: City) -> None:
        try:
            st = await asyncio.to_thread(os.stat, path)
        except OSError as exc:
            self.stats["failed"] += 1
            logger.warning("cannot stat {}: {}", path, exc)
            return

        existing = await self.db.get_photo_by_path(path)

        # incremental: unchanged file → skip EXIF/hash recompute
        if (
            existing is not None
            and existing.size == st.st_size
            and existing.modified_at is not None
            and abs(existing.modified_at.timestamp() - st.st_mtime) < 1.0
        ):
            self.stats["skipped"] += 1
            return

        mtime_dt = self._mtime_dt(st.st_mtime)
        sha = await asyncio.to_thread(compute_sha256, path)
        exif = await asyncio.to_thread(extract_exif, path)

        # corrupted / not-an-image file → mark failed, never stop the scan
        if exif.get("_valid") is False:
            self.stats["failed"] += 1
            logger.warning("corrupted image (marked failed): {}", path)
            if existing is not None:
                await self.db.update_photo(existing.id, scan_status=ScanStatus.FAILED,
                                           size=st.st_size, modified_at=mtime_dt, sha256=sha)
            else:
                try:
                    await self.db.add_photo(Photo(
                        city_id=city.id, path=path, filename=os.path.basename(path),
                        size=st.st_size, modified_at=mtime_dt, sha256=sha,
                        scan_status=ScanStatus.FAILED, city=city.name,
                        country=city.country, year=city.year,
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("failed to store corrupted photo {}: {}", path, exc)
            return

        # duplicate by content (same SHA-256, different path)
        dup = await self.db.get_photo_by_sha(sha)
        if dup is not None and (existing is None or dup.id != existing.id):
            status = ScanStatus.DUPLICATE
            self.stats["duplicates"] += 1
            logger.info("duplicate content {} (already {}); marking duplicate", path, dup.path)
        else:
            status = ScanStatus.SCANNED

        fields = dict(
            city_id=city.id,
            path=path,
            filename=os.path.basename(path),
            size=st.st_size,
            modified_at=mtime_dt,
            sha256=sha,
            taken_at=exif.get("taken_at"),
            latitude=exif.get("latitude"),
            longitude=exif.get("longitude"),
            country=city.country,
            city=city.name,
            year=exif.get("taken_at").year if exif.get("taken_at") else city.year,
            scan_status=status,
        )

        try:
            if existing is not None:
                await self.db.update_photo(existing.id, **fields)
                self.stats["updated"] += 1
            else:
                await self.db.add_photo(Photo(**fields))
                self.stats["new"] += 1
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            logger.warning("failed to store {}: {}", path, exc)

    async def _mark_missing(self, seen_paths: set) -> None:
        rows = await self.db.get_all_photo_paths()
        for pid, path in rows:
            if path not in seen_paths:
                await self.db.update_photo(pid, scan_status=ScanStatus.MISSING)
                self.stats["missing"] += 1
        if self.stats["missing"]:
            logger.warning("{} photos no longer present (marked missing)", self.stats["missing"])

    async def scan(self, progress: Optional[ProgressCb] = None) -> dict:
        """Scan the whole archive. Returns aggregate stats for THIS run."""
        self.stats = {"folders": 0, "total_files": 0, "new": 0, "updated": 0,
                      "skipped": 0, "failed": 0, "duplicates": 0, "missing": 0}
        if not self.archive_path.is_dir():
            logger.error("archive path is not a directory: {}", self.archive_path)
            return self.stats

        folders = self._find_city_folders()
        logger.info("Scanning {} city folders under {}", len(folders), self.archive_path)
        seen: set = set()

        for i, folder in enumerate(folders, start=1):
            city = await self._resolve_city(folder)
            files = list(self._iter_images(folder))
            self.stats["folders"] += 1
            for j, path in enumerate(files, start=1):
                self.stats["total_files"] += 1
                seen.add(path)
                await self._process_image(path, city)
                if progress is not None:
                    progress(i, len(folders), j, len(files), path)

        await self._mark_missing(seen)
        logger.info("Scan complete: {}", self.stats)
        return self.stats
