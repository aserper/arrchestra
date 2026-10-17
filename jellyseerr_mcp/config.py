"""Environment-only configuration. Secrets are never represented in diagnostics."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

SERVICES = ("jellyseerr", "radarr", "sonarr", "nzbget", "sabnzbd", "nzbhydra", "tautulli")

# Services whose target/release search fans out over every configured indexer and
# therefore needs its own, longer deadline than ordinary metadata reads.
SEARCH_TIMEOUT_SERVICES = ("radarr", "sonarr")


def validate_url(value: str, label: str = "URL") -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(f"{label} is invalid") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)
            or "\\" in value or "%" in parsed.netloc
            or any(part in {".", ".."} for part in parsed.path.split("/"))
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError(f"{label} must be an HTTP(S) base URL without credentials, query or fragment")
    return value.rstrip("/")


def origin(value: str) -> str:
    parsed = urlsplit(validate_url(value))
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{host}:{port}"


def boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value.lower() == "true"


def timeout(name: str, default: float, maximum: float = 300.0, allow_zero: bool = False) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(value) or value > maximum or (value <= 0 and not (allow_zero and value == 0)):
        raise ValueError(f"{name} must be between 0 and {maximum:g} seconds")
    return value


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    url: str
    api_key: str = field(default="", repr=False)
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    timeout: float = 15.0
    allow_writes: bool = False
    allowed_redirect_origins: tuple[str, ...] = ()
    # Target/release search fans out over every indexer and is routinely slower than
    # metadata reads, so it carries its own deadline.
    search_timeout: float = 120.0

    def __post_init__(self) -> None:
        if self.name not in SERVICES:
            raise ValueError("Unknown service")
        object.__setattr__(self, "url", validate_url(self.url, f"{self.name} URL"))
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("Service timeout must be positive and finite, at most 300 seconds")
        # 0 disables the bound and leaves the transport default in place.
        if not math.isfinite(self.search_timeout) or not 0 <= self.search_timeout <= 300:
            raise ValueError("Service search timeout must be finite and at most 300 seconds")
        for credential in (self.api_key, self.username, self.password):
            if any(ord(c) < 32 or ord(c) == 127 for c in credential):
                raise ValueError("Credentials must not contain control characters")
        if any(urlsplit(validate_url(u)).path not in {"", "/"} for u in self.allowed_redirect_origins):
            raise ValueError("Redirect allowlist must contain origins, not paths")
        object.__setattr__(self, "allowed_redirect_origins", tuple(origin(u) for u in self.allowed_redirect_origins))


@dataclass
class AppConfig:
    # Legacy constructor/variable compatibility; new installations need no Seerr.
    jellyseerr_url: str = ""
    jellyseerr_api_key: str = field(default="", repr=False)
    timeout: float = 15.0
    auth_issuer_url: str | None = None
    auth_resource_server_url: str | None = None
    auth_required_scopes: list[str] | None = None
    services: dict[str, ServiceConfig] = field(default_factory=dict)
    read_only: bool = True
    allow_raw_read: bool = False

    def service_configs(self) -> dict[str, ServiceConfig]:
        configs = dict(self.services)
        if any(name != conf.name for name, conf in configs.items()):
            raise ValueError("Service configuration names do not match their registry keys")
        if self.jellyseerr_url and "jellyseerr" not in configs:
            configs["jellyseerr"] = ServiceConfig("jellyseerr", self.jellyseerr_url,
                api_key=self.jellyseerr_api_key, timeout=self.timeout)
        if self.read_only:
            from dataclasses import replace
            configs = {name: replace(conf, allow_writes=False) for name, conf in configs.items()}
        return configs


def load_config() -> AppConfig:
    # Explicit process environment wins over .env; no credentials from MCP callers.
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    common_timeout = timeout("MCP_REQUEST_TIMEOUT", 15.0)
    read_only = boolean("MCP_READ_ONLY", True)
    # One default for every service, validated once even for services that are not
    # configured, so a typo surfaces instead of silently doing nothing.
    search_default = timeout("MCP_SEARCH_TIMEOUT", 120.0, allow_zero=True)
    configs: dict[str, ServiceConfig] = {}
    for name in SERVICES:
        prefix = name.upper()
        required = ("URL", "USERNAME", "PASSWORD") if name == "nzbget" else ("URL", "API_KEY")
        values = {key: os.getenv(f"{prefix}_{key}", "").strip() for key in required}
        if not any(values.values()):
            continue
        missing = [f"{prefix}_{key}" for key, value in values.items() if not value]
        if missing:
            raise ValueError("Incomplete service configuration: " + ", ".join(missing))
        redirects = tuple(u.strip() for u in os.getenv("NZBHYDRA_ALLOWED_REDIRECT_ORIGINS", "").split(",") if u.strip()) if name == "nzbhydra" else ()
        # Only indexer-backed search services take an override; any other prefix is ignored.
        # 0 disables the bound, so the search deadline may be zero here even though the
        # shared default must be positive.
        search_timeout = (timeout(f"{prefix}_SEARCH_TIMEOUT", search_default, allow_zero=True)
                          if name in SEARCH_TIMEOUT_SERVICES else search_default)
        configs[name] = ServiceConfig(name, values["URL"],
            api_key=values.get("API_KEY", ""), username=values.get("USERNAME", ""),
            password=values.get("PASSWORD", ""), timeout=timeout(f"{prefix}_TIMEOUT", common_timeout),
            allow_writes=boolean(f"{prefix}_ALLOW_WRITES") and not read_only,
            allowed_redirect_origins=redirects, search_timeout=search_timeout)
    if not configs:
        raise ValueError("Configure at least one service URL and its credentials (see .env.example)")
    return AppConfig(services=configs, timeout=common_timeout, read_only=read_only,
        allow_raw_read=boolean("MCP_ALLOW_RAW_READ"))
