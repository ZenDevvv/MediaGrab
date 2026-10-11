"""Client identity resolution.

Two distinct concerns, deliberately kept separate:

* **Client IP** -- trusted-header-aware resolution of the network peer.
* **Session identity** -- an HMAC-signed opaque token used purely to make the
  concurrency gate fair across unrelated users who share a CGNAT NAT address.

The session token is *not* authentication. It carries no user identity, no PII,
and no cross-session linkage; it exists so that two strangers behind one carrier
NAT are not permanently 429'd by each other (§1.1 Decision #2).
"""

import hmac
import logging
import secrets
from hashlib import sha256
from typing import Optional, Tuple

from fastapi import Request

from app import config

__all__ = [
    "Identity",
    "get_client_ip",
    "issue_session_token",
    "read_session_token",
    "resolve_client_ip",
    "resolve_identity",
    "session_key",
    "verify_session_token",
]

logger = logging.getLogger(__name__)

_TOKEN_BYTES = 16  # 128 bits of entropy


def _secret() -> bytes:
    """Return the signing key, generating an ephemeral one if unset.

    An ephemeral key means sessions reset on restart. That is acceptable for a
    fairness device and is preferable to running with a hardcoded key.
    """
    if config.SESSION_SECRET:
        return config.SESSION_SECRET.encode("utf-8")
    return _EPHEMERAL_SECRET


_EPHEMERAL_SECRET = secrets.token_bytes(32)


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), sha256).hexdigest()


def issue_session_token() -> str:
    """Mint a new opaque session token of the form ``<id>.<hmac>``."""
    token_id = secrets.token_urlsafe(_TOKEN_BYTES)
    return f"{token_id}.{_sign(token_id)}"


def session_key(token: Optional[str]) -> Optional[str]:
    """Return the stable identity key for a token, or None if it is not valid.

    The key is always the *id* half, never the signed envelope, so a token
    resolves to the same key whether it arrived as a freshly-minted value or as
    a verified cookie. Mixing the two forms would silently split one client's
    occupancy across two gate keys.
    """
    return verify_session_token(token)


def verify_session_token(token: Optional[str]) -> Optional[str]:
    """Return the token ID if ``token`` carries a valid signature, else None.

    Uses a constant-time comparison so a valid signature cannot be recovered by
    timing the rejection.
    """
    if not token:
        return None
    token_id, _, signature = token.partition(".")
    if not token_id or not signature:
        return None
    if not hmac.compare_digest(signature, _sign(token_id)):
        return None
    return token_id


def read_session_token(request: Request) -> Optional[str]:
    raw = request.cookies.get(config.SESSION_COOKIE_NAME)
    return verify_session_token(raw)


class Identity:
    """Resolved caller identity plus the context needed to explain a rejection."""

    def __init__(self, session_id: Optional[str], client_ip: Optional[str]):
        self.session_id = session_id
        self.client_ip = client_ip

    @property
    def is_session_backed(self) -> bool:
        return bool(self.session_id)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Identity(session={'set' if self.session_id else 'none'}, ip={self.client_ip})"


def resolve_identity(request: Request) -> Identity:
    """Session-first identity: cookie when valid, otherwise the client IP.

    Falls back to IP when the cookie is absent, tampered with, or stale, so a
    forged cookie cannot obtain a fresh gate.
    """
    return Identity(
        session_id=read_session_token(request),
        client_ip=get_client_ip(request),
    )


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


def set_session_cookie(response, token: str, secure: bool = True) -> None:
    """Attach the session cookie to a response.

    HttpOnly keeps it out of JS, SameSite=Lax blocks cross-site use, and Secure
    is applied whenever the request itself was HTTPS so the token is never sent
    in the clear.
    """
    response.set_cookie(
        config.SESSION_COOKIE_NAME,
        token,
        max_age=config.SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )