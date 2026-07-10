from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


_DEV_ADMIN_KEY = "dev-admin-" + secrets.token_hex(12)
_DEV_API_KEY = "dev-sk-" + secrets.token_hex(12)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    host: str = os.environ.get("GROK2API_HOST", "0.0.0.0")
    port: int = _int_env("GROK2API_PORT", 18024)
    admin_key: str = os.environ.get("GROK2API_ADMIN_KEY", "")
    api_key: str = os.environ.get("GROK2API_API_KEY", "")
    data_dir: Path = Path(os.environ.get("GROK2API_DATA_DIR", "/app/data"))
    browser_base_url: str = os.environ.get("GROK2API_BROWSER_BASE_URL", "http://127.0.0.1:6080")
    browser_mode: str = os.environ.get("GROK2API_BROWSER_MODE", "external-novnc")
    browser_image: str = os.environ.get("GROK2API_BROWSER_IMAGE", "grok2api-browser:latest")
    browser_port_base: int = _int_env("GROK2API_BROWSER_PORT_BASE", 18200)
    browser_debug_port_base: int = _int_env("GROK2API_BROWSER_DEBUG_PORT_BASE", 19200)
    browser_timezone: str = os.environ.get("GROK2API_BROWSER_TIMEZONE", os.environ.get("TZ", "Asia/Taipei"))
    browser_proxy_server: str = os.environ.get("GROK2API_BROWSER_PROXY_SERVER", "")
    browser_proxy_bypass_list: str = os.environ.get("GROK2API_BROWSER_PROXY_BYPASS_LIST", "")
    host_data_dir: Path = Path(os.environ.get("GROK2API_HOST_DATA_DIR", os.environ.get("GROK2API_DATA_DIR", "/app/data")))
    docker_host_gateway: str = os.environ.get("GROK2API_DOCKER_HOST_GATEWAY", "host.docker.internal")
    session_ttl_seconds: int = _int_env("GROK2API_SESSION_TTL_SECONDS", 900)
    cookie_writeback: bool = _bool_env("GROK2API_COOKIE_WRITEBACK", False)
    headless: bool = _bool_env("GROK2API_HEADLESS", True)
    capture_traces: bool = _bool_env("GROK2API_CAPTURE_TRACES", True)
    access_log: bool = _bool_env("GROK2API_ACCESS_LOG", False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "grok2api.sqlite3"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def diagnostics_dir(self) -> Path:
        return self.data_dir / "diagnostics"

    @property
    def effective_admin_key(self) -> str:
        return self.admin_key or _DEV_ADMIN_KEY

    @property
    def effective_api_key(self) -> str:
        return self.api_key or _DEV_API_KEY


settings = Settings()
