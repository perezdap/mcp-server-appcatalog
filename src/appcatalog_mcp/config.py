"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the app catalog MCP server."""

    model_config = SettingsConfigDict(
        env_prefix="APPCATALOG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Transport
    transport: str = Field(default="stdio", description="stdio | sse | streamable-http")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8010)

    # Cache
    cache_dir: Path = Field(default=Path("./data"))
    cache_ttl_hours: int = Field(default=6)

    # Sources
    winget_api: str = Field(
        default="auto",
        description='Winget backend: "auto" (GitHub then winget.run), "github", "winget.run"',
    )
    choco_api: str = Field(default="https://community.chocolatey.org/api/v2/")
    evergreen_api: str = Field(
        default="https://evergreen-api.stealthpuppy.com",
        description="Evergreen REST API base URL",
    )
    sihq_url: str = Field(
        default="http://127.0.0.1:8000/mcp",
        description="Silent Install HQ MCP server streamable-http endpoint (optional)",
    )

    # HTTP politeness
    user_agent: str = Field(
        default="mcp-server-appcatalog/0.1.0 (+https://github.com/perezdap/mcp-server-appcatalog)"
    )
    request_delay_seconds: float = Field(default=0.5)
    request_timeout_seconds: float = Field(default=30.0)
    httpx_max_connections: int = Field(default=10)
    httpx_max_keepalive_connections: int = Field(default=4)

    # Logging
    log_level: str = Field(default="INFO")

    # GitHub token is read from GITHUB_TOKEN (not APPCATATALOG prefixed).
    github_token: str = Field(
        default="",
        description="Optional GitHub token for microsoft/winget-pkgs API",
    )

    @model_validator(mode="after")
    def _resolve_github_token(self) -> Settings:
        if not self.github_token:
            self.github_token = os.getenv("GITHUB_TOKEN", "")
        return self

    # --- Derived / convenience ---------------------------------------------
    @property
    def cache_db_path(self) -> Path:
        return self.cache_dir / "cache.sqlite"

    @property
    def cache_ttl_seconds(self) -> int:
        return self.cache_ttl_hours * 3600

    @property
    def github_auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers


def get_settings() -> Settings:
    return Settings()
