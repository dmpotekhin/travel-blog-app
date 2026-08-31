"""Content-validation feature tests — Trip + VibeCoding validators and gates.

Validators are pure functions over config + domain objects (no network, no DB
reliance for the core checks); the scheduler gates are exercised with an empty
DB and the soft-vs-blocked behaviour.
"""

import json

import pytest

from core.config import Config
from core.database import Database
from core.models import City, Draft, VibeCodingPost
from modules.scheduler import Scheduler
from modules.trip_validator import TripValidator
from modules.vibecoding_validator import VibeCodingValidator


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "v.db"))
    await d.connect()
    return d


def _photos(n: int, scene: str = "interior hall") -> str:
    return json.dumps(
        {
            "photos": [
                {"path": f"/x/{i}.jpg", "analysis": {"scene": scene, "objects": []}}
                for i in range(n)
            ]
        }
    )


def _good_trip_content() -> str:
    # well over 100 words, no forbidden patterns
    return "Красивый город, тёплая атмосфера, вкусная еда, приятная прогулка. " * 14


def _good_vibe_text() -> str:
    return (
        "Вайбкодинг — это разработка вместе с AI-агентом. Я расскажу про практики, "
        "инструменты и личный опыт. " * 25
        + " #ai #coding #tools #practice А какой инструмент используешь ты?"
    )


# -- Trip validator ---------------------------------------------------------
def test_trip_valid_post_compliant():
    cfg = Config()
    draft = Draft(id=1, city_id=1, platform="trip_com", title="😊 Москва",
                  content=_good_trip_content(), photos_json=_photos(3))
    city = City(id=1, name="Moscow", latitude=55.75, longitude=37.61)
    r = TripValidator(cfg).validate(draft, city)
    assert r.compliant, r.summary()
    assert r.score >= 85


def test_trip_restaurant_needs_five_photos():
    cfg = Config()
    draft = Draft(id=1, city_id=1, platform="trip_com", title="Ресторан",
                  content=_good_trip_content(), photos_json=_photos(3, "restaurant interior"))
    city = City(id=1, name="Moscow", latitude=55.75, longitude=37.61)
    r = TripValidator(cfg).validate(draft, city)
    assert not r.compliant
    assert any(c.id == "min_photos" and not c.passed for c in r.checks)


def test_trip_missing_geotag_not_compliant():
    cfg = Config()
    draft = Draft(id=1, city_id=1, platform="trip_com", title="Москва",
                  content=_good_trip_content(), photos_json=_photos(3))
    city = City(id=1, name="Moscow")  # no coordinates
    r = TripValidator(cfg).validate(draft, city)
    assert not r.compliant
    assert any(c.id == "geotag" and not c.passed for c in r.checks)


def test_trip_forbidden_pattern_detected():
    cfg = Config()
    draft = Draft(id=1, city_id=1, platform="trip_com", title="Москва",
                  content=_good_trip_content() + " www.watermark.example",
                  photos_json=_photos(3))
    city = City(id=1, name="Moscow", latitude=1, longitude=1)
    r = TripValidator(cfg).validate(draft, city)
    assert any(c.id == "forbidden" and not c.passed for c in r.checks)


# -- VibeCoding validator ---------------------------------------------------
def test_vibe_valid_post_compliant():
    cfg = Config()
    post = VibeCodingPost(id=1, topic="AI", title="AI", status="draft",
                          generated_text=_good_vibe_text(), image_url="/tmp/x.png")
    r = VibeCodingValidator(cfg).validate(post)
    assert r.compliant, r.summary()


def test_vibe_forbidden_clickbait_not_compliant():
    cfg = Config()
    post = VibeCodingPost(id=1, topic="AI", title="AI", status="draft",
                          generated_text="Заработай миллион бесплатно " * 5,
                          image_url="/tmp/x.png")
    r = VibeCodingValidator(cfg).validate(post)
    assert not r.compliant
    assert any(c.id == "forbidden" and not c.passed for c in r.checks)


def test_vibe_thin_content_not_compliant():
    cfg = Config()
    post = VibeCodingPost(id=1, topic="AI", title="AI", status="draft",
                          generated_text="Маловато слов", image_url="/tmp/x.png")
    r = VibeCodingValidator(cfg).validate(post)
    assert not r.compliant
    assert any(c.id == "min_words" and not c.passed for c in r.checks)


def test_vibe_helpers():
    post = VibeCodingPost(id=1, topic="AI", title="AI", status="draft",
                          generated_text="Привет #ai #coding мир?", image_url="")
    r = VibeCodingValidator(Config()).validate(post)
    # media missing -> error severity fails (media photo required by default)
    assert any(c.id == "media" and not c.passed for c in r.checks)


# -- scheduler gates (soft vs block) ----------------------------------------
async def test_trip_gate_soft_then_blocked(db):
    cfg = Config()
    draft = Draft(id=1, city_id=1, platform="trip_com", title="Москва",
                  content=_good_trip_content(), photos_json=_photos(3))
    scheduler = Scheduler(db, cfg)

    cfg.trip_guidelines.block_non_compliant = False
    soft = await scheduler._trip_gate(draft)
    # no city in DB -> geotag fails -> not compliant, but not blocked (soft)
    assert soft["compliant"] is False
    assert soft["blocked"] is False

    cfg.trip_guidelines.block_non_compliant = True
    hard = await scheduler._trip_gate(draft)
    assert hard["blocked"] is True


async def test_vibe_gate_soft_then_blocked(db):
    cfg = Config()
    post = VibeCodingPost(id=1, topic="AI", title="AI", status="pending",
                          generated_text="Маловато слов", image_url="")
    scheduler = Scheduler(db, cfg)

    cfg.vibecoding_guidelines.block_non_compliant = False
    soft = await scheduler._vibe_gate(post)
    assert soft["compliant"] is False
    assert soft["blocked"] is False

    cfg.vibecoding_guidelines.block_non_compliant = True
    hard = await scheduler._vibe_gate(post)
    assert hard["blocked"] is True
