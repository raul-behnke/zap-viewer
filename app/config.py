import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_key: str
    waha_url: str
    waha_session: str
    waha_api_key: str
    hook_hmac_key: str
    media_retention_days: int


def load() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://zap:zap@localhost:5432/zap"
        ),
        api_key=os.environ.get("API_KEY", "dev"),
        waha_url=os.environ.get("WAHA_URL", "http://localhost:3000"),
        waha_session=os.environ.get("WAHA_SESSION", "default"),
        waha_api_key=os.environ.get("WAHA_API_KEY_PLAIN", "dev"),
        hook_hmac_key=os.environ.get("WHATSAPP_HOOK_HMAC_KEY", "dev"),
        media_retention_days=int(os.environ.get("MEDIA_RETENTION_DAYS", "30")),
    )


settings = load()
