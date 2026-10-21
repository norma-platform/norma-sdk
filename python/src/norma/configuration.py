# Modified for Norma.
"""Hosted connection configuration. Credentials never select an agent identity."""

import ipaddress
import os
from urllib.parse import urlsplit


def resolve_environment(
    name: str, explicit: str | None = None, default: str | None = None
) -> str | None:
    """Resolve an explicit setting or its canonical NORMA_ variable."""
    return explicit if explicit is not None else os.environ.get("NORMA_" + name, default)


def resolve_url(explicit: str | None = None) -> str:
    """Require HTTPS; permit HTTP only for loopback development."""
    value = resolve_environment("RUNTIME_URL", explicit)
    if not value or not value.strip():
        raise ValueError("NORMA_RUNTIME_URL is required.")
    parsed = urlsplit(value)
    # urlsplit defers malformed and out-of-range port validation until access.
    try:
        parsed.port
    except ValueError:
        raise ValueError("Runtime URL must have a valid port between 0 and 65535.") from None
    host = parsed.hostname
    local = host == "localhost"
    if host:
        try:
            local = local or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Runtime URL must have a host and no credentials, query or fragment.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Runtime URL requires HTTPS except on loopback development hosts.")
    return value.rstrip("/")


def resolve_agent_key(explicit: str | None = None) -> str:
    """Require a nonblank Agent Key without logging its value."""
    key = resolve_environment("AGENT_KEY", explicit)
    if not key or not key.strip():
        raise ValueError("NORMA_AGENT_KEY is required.")
    if key != key.strip() or "\r" in key or "\n" in key:
        raise ValueError("Agent Key contains invalid whitespace.")
    return key
