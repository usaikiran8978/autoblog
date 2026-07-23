"""API authentication and outbound-URL safety checks."""

from __future__ import annotations

import hmac
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import Header, HTTPException, status

from app.config import settings


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Constant-time admin key check for every mutating endpoint.

    `compare_digest` rather than `==` so response timing does not leak the
    key prefix to an attacker probing byte by byte.
    """
    if not hmac.compare_digest(x_api_key, settings.ADMIN_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key"
        )


# --------------------------------------------------------------------- SSRF
_BLOCKED_PORTS = {22, 23, 25, 445, 3306, 5432, 6379, 9200, 11211, 27017}


def assert_safe_url(url: str) -> None:
    """Reject URLs that resolve into private space before we fetch them.

    The collector follows links that ultimately come from third-party feeds,
    so an attacker who controls a feed entry could otherwise point us at
    169.254.169.254 (cloud metadata) or an internal admin service.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"blocked scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("missing host")
    if parsed.port and parsed.port in _BLOCKED_PORTS:
        raise ValueError(f"blocked port: {parsed.port}")

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"dns resolution failed: {parsed.hostname}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise ValueError(f"blocked non-public address: {ip}")


def redact(value: str | None, keep: int = 4) -> str:
    """For logging credentials without logging credentials."""
    if not value:
        return ""
    return f"{value[:keep]}…{'*' * 6}" if len(value) > keep else "*" * 6
