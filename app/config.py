from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_mode: Literal["demo", "live"] = "demo"
    database_url: str = "sqlite:///./data/adrobot.db"
    demo_database_url: str = "sqlite:///./data/keitaro-demo.db"
    keitaro_url: str = ""
    keitaro_api_key: SecretStr = SecretStr("")
    keitaro_timeout: float = 15
    default_domain_id: int | None = None
    default_group_id: int | None = None
    default_source_id: int | None = None
    app_username: str = "adrobot"
    app_password: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_live(self):
        if self.app_mode == "live":
            parsed = urlparse(self.keitaro_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Для live нужен KEITARO_URL с http:// или https://")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("KEITARO_URL не должен содержать пароль, query или fragment")
            if not self.keitaro_api_key.get_secret_value():
                raise ValueError("Для live нужен KEITARO_API_KEY")
        return self


def prepare_sqlite_directory(url: str):
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
