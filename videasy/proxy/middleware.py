from __future__ import annotations

import base64
import json
import logging

from fastapi import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from videasy.proxy.stream import do_proxy_stream

logger = logging.getLogger("videasy")

# ─── Token encode / decode ────────────────────────────────────────────
# The DASH middleware encodes base_dir + headers into a URL-safe token
# so segments are routed through our proxy without any global state.
#
# URL pattern: /dash/{token}/{segment}
# token = base64url( base_dir + "|" + headers_json )


def encode_dash_token(base_dir: str, headers_json: str) -> str:
    payload = base_dir + "|" + headers_json
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_dash_token(token: str) -> tuple[str, str]:
    # Re-add padding
    padded = token + "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode()
        base_dir, headers_json = decoded.split("|", 1)
        return base_dir, headers_json
    except Exception:
        raise ValueError("Invalid DASH token")


# ─── Middleware ────────────────────────────────────────────────────────


class DASHSegmentMiddleware:
    """ASGI middleware that intercepts /dash/{token}/* requests
    and proxies the segment through the CDN."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if not path.startswith("/dash/"):
            return await self.app(scope, receive, send)

        # Parse: /dash/{token}/{segment...}
        parts = path[6:].split("/", 1)  # strip "/dash/"
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return await self.app(scope, receive, send)

        token, segment = parts[0], parts[1]
        try:
            base_dir, headers_json = decode_dash_token(token)
        except ValueError:
            return await self.app(scope, receive, send)

        target_url = base_dir + segment

        from starlette.requests import Request
        request = Request(scope, receive, send)
        try:
            response = await do_proxy_stream(request, target_url, headers_json)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await self.app(scope, receive, send)
            raise

        await response(scope, receive, send)
