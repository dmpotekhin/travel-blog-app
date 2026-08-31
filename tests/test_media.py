import shutil
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from core.config import Config, MediaPreset
from core.exceptions import MediaProcessingError
from modules.media import create_video, optimize_image, prepare_platform_set


def _img(path, color=(100, 120, 140), size=(800, 600)):
    Image.new("RGB", size, color).save(path)


def test_optimize_image_resizes_and_caps():
    tmp = Path(tempfile.mkdtemp(prefix="tba_media_"))
    try:
        src = tmp / "src.jpg"
        _img(src)
        preset = MediaPreset(width=400, height=300, max_mb=5.0)
        out = optimize_image(src, tmp / "out.jpg", preset, jpeg_quality=90)
        assert out.exists()
        with Image.open(out) as im:
            assert im.size == (400, 300), im.size
            assert im.mode == "RGB"
        assert out.stat().st_size < 5 * 1024 * 1024
        print("PASS: optimize_image resized to preset box")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prepare_platform_set():
    tmp = Path(tempfile.mkdtemp(prefix="tba_media_"))
    try:
        (tmp / "a.jpg").write_bytes(b"")
        _img(tmp / "a.jpg")
        _img(tmp / "b.jpg")
        config = Config(media_presets={"instagram": MediaPreset(width=1080, height=1080, max_mb=5.0)})
        outs = prepare_platform_set(
            [tmp / "a.jpg", tmp / "b.jpg"],
            "instagram",
            tmp / "out",
            config.media_presets,
            jpeg_quality=88,
        )
        assert len(outs) == 2
        for o in outs:
            assert o.exists()
            assert "1080x1080" in o.name
        print("PASS: prepare_platform_set produced per-platform assets")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prepare_platform_set_missing_preset():
    with pytest.raises(MediaProcessingError):
        prepare_platform_set([], "unknown_platform", tempfile.mkdtemp(), {}, 90)


def test_create_video_requires_photos():
    with pytest.raises(MediaProcessingError):
        create_video([], "/tmp/x.mp4")
    print("PASS: create_video rejects empty photo list")
