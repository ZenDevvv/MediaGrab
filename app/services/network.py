"""Client IP resolution.

The per-IP concurrency gate keys on the client address, so resolution has to be
trustworthy. When the app is exposed directly we use the socket peer and nothing
else; trusting ``X-Forwarded-For`` in that case would let any client forge an
address and bypass the gate entirely.
"""

from typing import Optional

from fastapi import Request

from app import config

__all__ = ["get_client_ip", "resolve_client_ip"]


def resolve_client_ip(
    cf_connecting_ip: Optional[str],
    x_forwarded_for: Optional[str],
    peer_host: Optional[str],
    behind_proxy: Optional[bool] = None,
) -> Optional[str]:
    """Resolve the effective client IP from raw header/socket values.

    Pure function so the resolution order is directly testable without
    constructing a real ``Request``.
    """
    if behind_proxy is None:
        behind_proxy = config.BEHIND_PROXY

    if behind_proxy:
        # Cloudflare sets this and it cannot be spoofed when the origin is
        # restricted to Cloudflare ranges.
        if cf_connecting_ip:
            candidate = cf_connecting_ip.strip()
            if candidate:
                return candidate

        # X-Forwarded-For is client-appendable: the left-most entry is the
        # original client, everything after it was appended by intermediaries.
        if x_forwarded_for:
            first = x_forwarded_for.split(",")[0].strip()
            if first:
                return first

    return peer_host


def get_client_ip(request: Request) -> Optional[str]:
    return resolve_client_ip(
        cf_connecting_ip=request.headers.get("CF-Connecting-IP"),
        x_forwarded_for=request.headers.get("X-Forwarded-For"),
        peer_host=request.client.host if request.client else None,
    )