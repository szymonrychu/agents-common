"""Bearer service-token ASGI guard, shared by the services' token-gated admin listeners."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Iterable
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["EXEMPT_PATHS", "ServiceTokenGate", "load_tokens"]

log = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})
HEALTH_METHODS = frozenset({"GET", "HEAD"})


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).hexdigest().encode()


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            token = token.strip()
            return token if scheme.lower() == "bearer" and token else None
    return None


def load_tokens(path: str | Path) -> list[str]:
    """One token per line, blank lines and surrounding whitespace dropped; none is an error."""
    tokens = [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    if not tokens:
        raise ValueError("token file holds no tokens")
    return tokens


class ServiceTokenGate:
    """401 unless the bearer token equals one of `tokens`; GET/HEAD on health paths are exempt.

    Tokens are compared by sha256 digest with `hmac.compare_digest`, every entry on every
    request, so neither the match position nor the token length leaks through timing. The
    presented token is never logged. Missing, malformed, empty and wrong tokens all get the
    same 401 body.
    """

    def __init__(self, app: ASGIApp, tokens: Iterable[str]):
        self.app = app
        tokens = list(tokens)
        if not all(tokens):
            raise ValueError("empty service token")
        self.digests = [_digest(t) for t in tokens]

    def authenticate(self, token: str) -> bool:
        presented = _digest(token)
        ok = False
        for digest in self.digests:
            if hmac.compare_digest(presented, digest):
                ok = True
        return ok

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope.get("path", "")
        if path in EXEMPT_PATHS and method in HEALTH_METHODS:
            await self.app(scope, receive, send)
            return
        token = _bearer(scope)
        if token is None or not self.authenticate(token):
            log.info("service request method=%s path=%r status=401", method, path)
            response = JSONResponse(
                {"error": {"code": "unauthorized", "message": "a valid service token is required"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
