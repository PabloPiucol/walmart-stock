from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    bsale_access_token: str = ""
    bsale_api_url: str = "https://api.bsale.io/v1"
    walmart_client_id: str = ""
    walmart_client_secret: str = ""
    walmart_partner_id: str = ""
    walmart_channel_type: str = ""
    walmart_api_url: str = "https://marketplace.walmartapis.com"
    walmart_market: str = "cl"
    walmart_feed_timeout_seconds: int = 2100
    walmart_feed_poll_seconds: int = 30
    database_url: str = "sqlite:///./data/app.db"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
