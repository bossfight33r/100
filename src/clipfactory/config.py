"""Настройки (env/.env) и загрузка YAML кампаний и аккаунтов."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from clipfactory.schemas import Account, Campaign


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CF_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    data_dir: Path = Path("data")
    campaigns_dir: Path = Path("config/campaigns")
    accounts_file: Path = Path("config/accounts.yaml")

    transcriber: Literal["auto", "mlx", "faster_whisper", "fake"] = "auto"
    whisper_model: str = "large-v3-turbo"
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 16000
    face_detector: Literal["mediapipe", "fake"] = "mediapipe"
    face_model_path: Path = Path("data/models/blaze_face_short_range.tflite")
    encoder: Literal["auto", "videotoolbox", "nvenc", "x264"] = "auto"

    caption_font: str = "Arial"
    caption_fonts_dir: Path | None = None
    caption_font_size: int = 84
    caption_max_words: int = Field(default=3, ge=2, le=4)

    analysis_fps: float = Field(default=4.0, ge=1, le=10)
    output_fps: int = 30
    chunk_seconds: float = 1200
    chunk_overlap_seconds: float = 60

    queue: Literal["inline", "rq"] = "inline"
    redis_url: str = "redis://localhost:6379/0"
    ffmpeg_timeout_sec: float = 3600

    # Локальный Telegram Bot API server (лимит файлов 2 ГБ вместо 20 МБ), например http://localhost:8081
    telegram_api_url: str | None = None

    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    youtube_client_secrets: Path = Path("data/secrets/youtube_client_secret.json")

    # Секреты читаются без префикса CF_ — так их называют SDK.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN"
    )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _admins(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "clipfactory.sqlite3"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @cached_property
    def campaigns(self) -> dict[str, Campaign]:
        return load_campaigns(self.campaigns_dir)

    @cached_property
    def accounts(self) -> dict[str, Account]:
        return load_accounts(self.accounts_file)

    def campaign(self, campaign_id: str) -> Campaign:
        try:
            return self.campaigns[campaign_id]
        except KeyError:
            known = ", ".join(sorted(self.campaigns)) or "<none>"
            raise ConfigError(f"unknown campaign {campaign_id!r}; known: {known}") from None


def _read_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e


def load_campaigns(directory: Path) -> dict[str, Campaign]:
    result: dict[str, Campaign] = {}
    if not directory.exists():
        return result
    for path in sorted(directory.glob("*.y*ml")):
        data = _read_yaml(path)
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected a mapping")
        data.setdefault("id", path.stem)
        try:
            campaign = Campaign.model_validate(data)
        except ValueError as e:
            raise ConfigError(f"{path}: {e}") from e
        if campaign.id in result:
            raise ConfigError(f"{path}: duplicate campaign id {campaign.id!r}")
        result[campaign.id] = campaign
    return result


def load_accounts(path: Path) -> dict[str, Account]:
    if not path.exists():
        example = path.with_name("accounts.example.yaml")
        if not example.exists():
            return {}
        path = example
    data = _read_yaml(path) or {}
    items = data.get("accounts", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ConfigError(f"{path}: expected 'accounts' list")
    result: dict[str, Account] = {}
    for item in items:
        try:
            account = Account.model_validate(item)
        except ValueError as e:
            raise ConfigError(f"{path}: {e}") from e
        if account.id in result:
            raise ConfigError(f"{path}: duplicate account id {account.id!r}")
        result[account.id] = account
    return result
