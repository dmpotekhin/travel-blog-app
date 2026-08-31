"""Publisher factory: platform + app mode -> concrete publisher."""
from __future__ import annotations

MANUAL_PLATFORMS = ("zen", "trip_com", "instagram", "youtube")


def build_publisher(db, config, platform: str):
    """Return the adapter for ``platform``. dry_run forces mock for auto ones."""
    if platform in MANUAL_PLATFORMS:
        from modules.publishers.manual import ManualPublisher
        return ManualPublisher(db, config, platform)
    if getattr(config.app, "dry_run", False):
        from modules.publishers.mock import MockPublisher
        return MockPublisher(db, config, platform)
    if platform == "telegram":
        from modules.publishers.telegram import TelegramPublisher
        return TelegramPublisher(db, config)
    if platform == "vk":
        from modules.publishers.vk import VKPublisher
        return VKPublisher(db, config)
    if platform == "facebook":
        from modules.publishers.facebook import FacebookPublisher
        return FacebookPublisher(db, config)
    from modules.publishers.manual import ManualPublisher
    return ManualPublisher(db, config, platform)
