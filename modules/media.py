"""Media processing (section 32-35): turn source photos into platform-ready assets.

Image transforms (orientation, resize, format, quality, size cap) are done with
Pillow, which is always available. Video slideshows are built with moviepy when
it is importable; otherwise ``create_video`` raises :class:`MediaProcessingError`
with a clear message so the pipeline can degrade to photo-only publishing.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageOps

from core.config import Config, MediaPreset
from core.exceptions import MediaProcessingError

log = logging.getLogger(__name__)


def _open(path) -> Image.Image:
    try:
        return Image.open(path)
    except Exception as exc:
        raise MediaProcessingError(f"Cannot open image {path}: {exc}") from exc


def optimize_image(
    src,
    dest,
    preset: MediaPreset,
    jpeg_quality: int = 90,
    target_format: str = "JPEG",
) -> Path:
    """Orient, resize (fit within preset box), re-encode and cap file size."""
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _open(src) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if preset.width > 0 and preset.height > 0:
            im = ImageOps.fit(im, (preset.width, preset.height), method=Image.LANCZOS)
        elif preset.width > 0:
            im = _resize_width(im, preset.width)
        elif preset.height > 0:
            im = _resize_height(im, preset.height)

        quality = jpeg_quality
        while True:
            im.save(dest, format=target_format, quality=quality, optimize=True)
            size_mb = dest.stat().st_size / (1024 * 1024)
            if size_mb <= preset.max_mb or quality <= 40:
                break
            quality -= 10
    return dest


@functools.lru_cache(maxsize=None)
def _ffmpeg_supported() -> bool:
    try:
        import imageio_ffmpeg  # noqa: F401
        return True
    except Exception:
        return False


def create_video(
    photo_paths: Iterable[str],
    dest,
    resolution: str = "1080x1920",
    photo_duration: int = 4,
    fps: int = 24,
) -> Optional[Path]:
    """Build a slideshow video from photos. Returns the path, or None if moviepy
    is unavailable (caller should fall back to photo-only publishing)."""
    photos = [str(p) for p in photo_paths]
    if not photos:
        raise MediaProcessingError("create_video requires at least one photo")
    try:
        from moviepy.editor import ImageClip, concatenate_videoclips
    except Exception as exc:
        raise MediaProcessingError(
            "moviepy/ffmpeg not available for video creation; use photo-only publishing"
        ) from exc
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    w, h = _parse_resolution(resolution)
    clips = []
    for p in photos:
        clip = ImageClip(p).resize(newsize=(w, h)).with_duration(photo_duration)
        clips.append(clip)
    video = concatenate_videoclips(clips, method="compose")
    video = video.with_fps(fps)
    video.write_videofile(
        str(dest),
        codec="libx264",
        audio=False,
        verbose=False,
        logger=None,
    )
    return dest


def prepare_platform_set(
    photo_paths: Iterable[str],
    platform: str,
    dest_dir,
    presets: Dict[str, MediaPreset],
    jpeg_quality: int = 90,
) -> List[Path]:
    """Produce an optimised asset per photo for a platform's preset."""
    preset = presets.get(platform)
    if preset is None:
        raise MediaProcessingError(f"No media preset for platform '{platform}'")
    dest_dir = Path(dest_dir)
    outputs = []
    for src in photo_paths:
        src = Path(src)
        out = dest_dir / f"{preset.width}x{preset.height}_{src.name}"
        out = optimize_image(src, out, preset, jpeg_quality)
        outputs.append(out)
    return outputs


def _parse_resolution(resolution: str) -> Tuple[int, int]:
    try:
        w, h = (int(x) for x in resolution.lower().split("x"))
        return w, h
    except Exception as exc:
        raise MediaProcessingError(f"Invalid resolution '{resolution}'") from exc


def _resize_width(im: Image.Image, width: int) -> Image.Image:
    ratio = width / im.width
    return im.resize((width, max(1, int(im.height * ratio))), Image.LANCZOS)


def _resize_height(im: Image.Image, height: int) -> Image.Image:
    ratio = height / im.height
    return im.resize((max(1, int(im.width * ratio)), height), Image.LANCZOS)


# Which media preset each VibeCoding target platform uses (static-image only,
# so MP4 presets like instagram_reels / youtube_shorts are skipped).
_VIBE_PLATFORM_PRESETS: Dict[str, str] = {
    "trip_com": "trip_cover",
    "facebook": "facebook_post",
    "telegram": "telegram",
    "vk": "vk_post",
    "zen": "zen",
    "instagram": "instagram_post",
}


def prepare_vibecoding_media(
    image_path: str,
    post_id: int,
    presets: Dict[str, MediaPreset],
    config: Optional[Config] = None,
    jpeg_quality: int = 90,
) -> Dict[str, Path]:
    """Adapt one generated tile image for every enabled publishing platform.

    Reuses :func:`optimize_image` to orient/resize/encode each asset against the
    platform preset from ``config.yaml`` and writes the assets under
    ``media_ready/vibecoding/{post_id}/``. Returns ``{platform: path}`` for the
    platforms that were prepared (a platform is skipped if it is disabled in
    ``config.publishing`` or has no JPEG preset configured).
    """
    dest_root = Path("media_ready") / "vibecoding" / str(post_id)
    dest_root.mkdir(parents=True, exist_ok=True)
    publishing = getattr(config, "publishing", None) if config else None
    src = Path(image_path)
    out: Dict[str, Path] = {}
    for platform, preset_name in _VIBE_PLATFORM_PRESETS.items():
        if publishing is not None and not bool(getattr(publishing, platform, True)):
            continue
        preset = presets.get(preset_name)
        if preset is None or (preset.format or "JPEG").upper() != "JPEG":
            continue
        dest = dest_root / preset_name / f"{preset.width}x{preset.height}_{src.name}"
        out[platform] = optimize_image(src, dest, preset, jpeg_quality)
    return out
