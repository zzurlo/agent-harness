"""Single-owner bearer gate. No per-user identity or alternate credential sources."""
from __future__ import annotations

import hmac
import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class OwnerAuthMiddleware:
    """Pure ASGI: authorize before routing without reading/buffering SSE or bodies.

    CORS must wrap this middleware so only validated preflight responses bypass
    authentication, and auth errors remain readable by the browser.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if (scope["type"] == "http" and scope["method"] == "GET"
                and scope["path"] in {"/health", "/warmup"}):
            await self.app(scope, receive, send)
            return

        configured = os.getenv("APP_ACCESS_TOKEN", "")
        try:
            expected = configured.encode("utf-8")
        except UnicodeEncodeError:
            expected = b""
        if len(configured) < 32 or not expected:
            status, detail = 503, "Private access is not configured on the server."
        else:
            values = [value for name, value in scope.get("headers", [])
                      if name.lower() == b"authorization"]
            parts = values[0].split(b" ") if len(values) == 1 else []
            if (len(parts) == 2 and parts[0].lower() == b"bearer"
                    and hmac.compare_digest(parts[1], expected)):
                await self.app(scope, receive, send)
                return
            status, detail = 401, "A valid owner access key is required."

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {"Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        await JSONResponse({"detail": detail}, status_code=status, headers=headers)(
            scope, receive, send
        )
