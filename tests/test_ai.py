import hashlib
from pathlib import Path

import pytest

from core.config import Config
from core.database import Database
from core.exceptions import AIProviderError
from modules.ai.base import BaseAIProvider, ImageAnalysis
from modules.ai.registry import build_provider


class CountingProvider(BaseAIProvider):
    """Real provider skeleton used to test cache + limit wiring."""

    name = "counting"
    raw_calls = 0

    def _model_name(self) -> str:
        return "counting-model"

    async def _analyze_image_raw(self, image_path: str, prompt: str) -> ImageAnalysis:
        type(self).raw_calls += 1
        return ImageAnalysis(subjects=["x"], scene=image_path, raw=prompt)

    async def _generate_text_raw(self, system: str, user: str, *, max_tokens=None) -> str:
        return f"out:{user}"


@pytest.fixture
async def db():
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="tba_ai_")
    d = Database(str(Path(tmp) / "t.db"))
    await d.connect()
    try:
        yield d
    finally:
        await d.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_build_provider_dry_run_returns_mock(db):
    config = Config()  # app.dry_run defaults True
    provider = build_provider(db, config)
    assert provider.name == "mock"
    print("PASS: dry_run -> mock provider")


@pytest.mark.asyncio
async def test_mock_analyze_deterministic(db):
    config = Config()
    provider = build_provider(db, config)
    h = hashlib.sha256(b"photo1").hexdigest()
    a = await provider.analyze_image("/tmp/x.jpg", "prompt", image_hash=h)
    b = await provider.analyze_image("/tmp/x.jpg", "prompt", image_hash=h)
    assert isinstance(a, ImageAnalysis)
    assert a.subjects == b.subjects
    # a path containing "corrupt" is flagged low quality
    bad = await provider.analyze_image("/tmp/you_corrupt.jpg", "p", image_hash=hashlib.sha256(b"bad1").hexdigest())
    assert bad.quality_ok is False
    print("PASS: mock analysis deterministic + quality flag")


@pytest.mark.asyncio
async def test_cache_avoids_second_raw_call(db):
    config = Config()
    provider = CountingProvider(db, config)
    CountingProvider.raw_calls = 0
    h = hashlib.sha256(b"cachetest").hexdigest()
    await provider.analyze_image("/tmp/a.jpg", "same-prompt", image_hash=h)
    first = CountingProvider.raw_calls
    await provider.analyze_image("/tmp/a.jpg", "same-prompt", image_hash=h)
    second = CountingProvider.raw_calls
    assert first == 1, first
    assert second == 1, second  # cache hit — no second raw call
    print("PASS: AI cache prevents duplicate provider calls")


@pytest.mark.asyncio
async def test_rate_limiter_counts_requests(db):
    config = Config()
    provider = CountingProvider(db, config)
    await provider.generate_text("sys", "hi")
    stats = await db.get_gemini_stats()
    assert stats is not None and stats.requests_count == 1, stats
    print("PASS: rate limiter records request count")


@pytest.mark.asyncio
async def test_mock_generate_text_extracts_city(db):
    config = Config()
    provider = build_provider(db, config)
    text = await provider.generate_text(
        "You are a travel writer.",
        "Write about\ncity: Казань\nyear: 2018\ncountry: Россия",
    )
    assert "Казань" in text and "2018" in text
    print("PASS: mock story uses city/country/year hints")


@pytest.mark.asyncio
async def test_deepseek_rejects_image_analysis(db):
    from modules.ai.deepseek import DeepSeekProvider
    config = Config()
    provider = DeepSeekProvider(db, config)
    with pytest.raises(AIProviderError):
        await provider._analyze_image_raw("/tmp/x.jpg", "p")
    print("PASS: DeepSeek refuses image analysis (text-only)")
