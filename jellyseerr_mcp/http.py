"""Small shared transport: bounded responses, no retries, no credential-bearing errors."""
from __future__ import annotations

import json as json_module
import logging
import math
import re
import time
from datetime import date
import xml.etree.ElementTree as ET
from contextlib import closing
from typing import Any, Iterable, cast
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .config import ServiceConfig, origin

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_NZB_BYTES = 4 * 1024 * 1024


class ServiceError(RuntimeError):
    """Safe to present to MCP callers; never includes an upstream body or URL."""


def positive_id(value: Any, name: str = "id") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a positive integer") from None
    if str(result) != str(value) or result <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def non_negative_id(value: Any, name: str = "id") -> int:
    """Like positive_id but allows 0.

    Seerr numbers its configured Sonarr/Radarr servers from 0, so the default
    server legitimately has id 0 and must not be rejected as missing.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a non-negative integer") from None
    if str(result) != str(value) or result < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def pagination(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
        raise ValueError("offset must be between 0 and 1000000")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


def window(start: str, end: str, days_max: int = 90) -> tuple[str, str]:
    """Validate a calendar window and return the two native date strings.

    ``start`` and ``end`` are ISO dates. Both calendar APIs reject malformed
    dates with 400, so the shape is checked here as well; the order and span are
    rejected locally because the window is the one input that can force an
    unbounded upstream read. The values are passed through unchanged, so a date
    stays a date and an ISO timestamp stays a timestamp.
    """
    values = []
    for value, name in ((start, "start"), (end, "end")):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", value.strip()):
            raise ValueError(f"{name} must be an ISO date")
        text = value.strip()
        if len(text) > 30:
            raise ValueError(f"{name} must be an ISO date")
        values.append(text)
    try:
        first, last = (date.fromisoformat(item[:10]) for item in values)
    except ValueError:
        raise ValueError("start and end must be valid ISO dates") from None
    if first > last:
        raise ValueError("start must not be after end")
    if (last - first).days > days_max:
        raise ValueError(f"window must not exceed {days_max} days")
    return values[0], values[1]


def page(items: list, offset: int = 0, limit: int = 25) -> dict:
    pagination(offset, limit)
    return {"items": items[offset:offset + limit], "offset": offset, "limit": limit,
            "total": len(items), "has_more": offset + limit < len(items), "pagination": "local"}


def select(data: dict, fields: tuple | list) -> dict:
    return {key: data[key] for key in fields if key in data}


def parse_xml(content: bytes) -> ET.Element:
    # Reject alternate encodings too: declaration scanning is only safe on UTF-8.
    if len(content) > MAX_NZB_BYTES or b"\x00" in content:
        raise ValueError("XML exceeds the limit or uses an unsupported encoding")
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DTD and entity declarations are not allowed")
    try:
        text = content.decode("utf-8-sig")
        parser = ET.XMLPullParser(events=("start", "end"))
        root: ET.Element | None = None
        depth = count = 0
        for offset in range(0, len(text), 16384):
            parser.feed(text[offset:offset + 16384])
            for event, element in cast(Iterable[tuple[str, ET.Element]], parser.read_events()):
                if event == "start":
                    depth += 1
                    count += 1
                    if root is None:
                        root = element
                    if depth > 32 or count > 50000:
                        raise ValueError("XML nesting or element limit exceeded")
                else:
                    depth -= 1
        parser.close()
        if root is None:
            raise ValueError("Empty XML response")
        return root
    except (UnicodeError, ET.ParseError):
        raise ValueError("Invalid UTF-8 XML response") from None


def validate_nzb(content: bytes) -> None:
    if not isinstance(content, bytes) or not content:
        raise ValueError("NZB must contain bytes")
    root = parse_xml(content)
    if root.tag.rsplit("}", 1)[-1] != "nzb":
        raise ValueError("Response is not an NZB")
    files = [child for child in root if child.tag.rsplit("}", 1)[-1] == "file"]
    if not files or not all(any(node.tag.rsplit("}", 1)[-1] == "segment" for node in child.iter()) for child in files):
        raise ValueError("NZB has no valid file segments")


def sanitize(value: Any, secrets: tuple[str, ...] = (), _depth: int = 0) -> Any:
    if _depth > 32:
        raise ServiceError("Response nesting exceeds the safety limit")
    if isinstance(value, dict):
        return {key: sanitize(item, secrets, _depth + 1) for key, item in value.items()
                if not re.search(r"(?i)(api.?key|password|authorization|access.?token|refresh.?token)", str(key))}
    if isinstance(value, list):
        return [sanitize(item, secrets, _depth + 1) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(api.?key|password|token)=([^\s&<>]+)", r"\1=[redacted]", value)
        value = re.sub(r"https?://[^\s/]+:[^\s@]+@", "[authenticated URL]/", value)
    return value


class ServiceClient:
    def __init__(self, config: ServiceConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        # Query credentials must stay out of HTTPX's request logs, including
        # when clients are embedded without the CLI's logging setup.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._http = httpx.Client(timeout=config.timeout, transport=transport,
                                  follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self._http.close()

    def require_write(self) -> None:
        if not self.config.allow_writes:
            raise PermissionError(f"{self.config.name}: writes are disabled")

    def request_bytes(self, method: str, path: str, *, params: dict | None = None,
                      json: Any = None, data: dict | None = None, files: Any = None,
                      mutation: bool = False, follow_allowed_redirects: bool = False,
                      timeout: float | None = None) -> tuple[bytes, dict]:
        if mutation:
            self.require_write()
        parsed = urlsplit(path)
        decoded_path = unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or path.startswith("/") or "\\" in path
                or any(part in {".", ".."} for part in decoded_path.split("/"))
                or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise ValueError("Endpoint must be a relative service path")
        method = method.upper()
        url = self.config.url + "/" + path
        own_origin = origin(self.config.url)
        base_path = unquote(urlsplit(self.config.url).path).rstrip("/") + "/"
        allowed = {own_origin, *self.config.allowed_redirect_origins}
        # None (or a disabled 0) keeps the client's configured deadline; a positive
        # value widens this one request only (indexer fan-out), for both the socket
        # and the wall clock.
        widening = timeout is not None and timeout > 0
        deadline: float = timeout if widening and timeout is not None else self.config.timeout
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("timeout must be a positive finite number")
        started = time.monotonic()
        try:
            for hop in range(4):
                same_origin = origin(url.split("?", 1)[0]) == own_origin
                redirect_path = unquote(urlsplit(url).path)
                if ("\\" in redirect_path or "%" in redirect_path
                        or any(part in {".", ".."} for part in redirect_path.split("/"))):
                    raise ServiceError(f"{self.config.name}: unsafe request path refused")
                if same_origin and not redirect_path.startswith(base_path):
                    raise ServiceError(f"{self.config.name}: redirect outside configured service path refused")
                headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
                # Tautulli, SABnzbd and NZBHydra carry the key in the query string or
                # their own parameter; a header as well would duplicate the secret.
                if same_origin and self.config.api_key and self.config.name not in {"sabnzbd", "nzbhydra", "tautulli"}:
                    headers["X-Api-Key"] = self.config.api_key
                auth = httpx.BasicAuth(self.config.username, self.config.password) if same_origin and self.config.name == "nzbget" else None
                # New Request avoids carrying cookies/auth to redirect destinations.
                request = httpx.Request(method, url, headers=headers, params=params,
                                        json=json, data=data, files=files)
                if widening:
                    # httpx 0.28 has no timeout argument on send(); the per-request
                    # deadline travels in the request extensions instead.
                    request.extensions["timeout"] = httpx.Timeout(deadline).as_dict()
                with closing(self._http.send(request, stream=True, auth=auth)) as response:
                    if response.is_redirect:
                        if not follow_allowed_redirects or mutation or method != "GET" or hop == 3:
                            raise ServiceError(f"{self.config.name}: redirect refused")
                        destination = urljoin(str(response.url), response.headers.get("location", ""))
                        parts = urlsplit(destination)
                        if (parts.username is not None or parts.password is not None or parts.fragment
                                or origin(destination.split("?", 1)[0]) not in allowed
                                or (urlsplit(url).scheme == "https" and parts.scheme != "https")):
                            raise ServiceError(f"{self.config.name}: redirect destination refused")
                        # A same-origin link can carry our key. Never send it cross-origin.
                        if origin(destination.split("?", 1)[0]) != own_origin:
                            if any(s and (s in destination or s in unquote(destination)) for s in (self.config.api_key, self.config.password)):
                                raise ServiceError(f"{self.config.name}: credential-bearing redirect refused")
                        url, params = destination, None
                        continue
                    if not 200 <= response.status_code < 300:
                        outcome = "outcome uncertain; inspect state before retrying" if mutation else "API request failed"
                        raise ServiceError(f"{self.config.name}: {outcome} (HTTP {response.status_code})")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ServiceError(f"{self.config.name}: compressed response refused")
                    content = bytearray()
                    chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                    for chunk in chunks:
                        if time.monotonic() - started > deadline:
                            raise httpx.ReadTimeout("bounded deadline")
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ServiceError(f"{self.config.name}: response exceeds 8 MiB limit")
                        content.extend(chunk)
                    return bytes(content), dict(response.headers)
            raise ServiceError(f"{self.config.name}: redirect limit exceeded")
        except httpx.TimeoutException:
            suffix = "outcome uncertain; inspect state before retrying" if mutation else "request timed out"
            raise ServiceError(f"{self.config.name}: {suffix}") from None
        except httpx.RequestError:
            suffix = "outcome uncertain; inspect state before retrying" if mutation else "service unavailable"
            raise ServiceError(f"{self.config.name}: {suffix}") from None
        except ValueError:
            raise ServiceError(f"{self.config.name}: invalid response or redirect") from None

    def request_json(self, method: str, path: str, *, params: dict | None = None,
                     json: Any = None, data: dict | None = None, files: Any = None,
                     mutation: bool = False, timeout: float | None = None) -> Any:
        content, _ = self.request_bytes(method, path, params=params, json=json,
                                        data=data, files=files, mutation=mutation,
                                        timeout=timeout)
        if not content:
            return None
        try:
            result = json_module.loads(content)
        except (ValueError, UnicodeError, RecursionError):
            detail = "outcome uncertain; inspect state before retrying" if mutation else "invalid JSON response"
            raise ServiceError(f"{self.config.name}: {detail}") from None
        return sanitize(result, (self.config.api_key, self.config.password))
