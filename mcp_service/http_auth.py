"""Small ASGI bearer-token guard for the Streamable HTTP MCP endpoint."""
from __future__ import annotations

import secrets
from typing import Any


class BearerTokenMiddleware:
    def __init__(self, app: Any, api_key: str):
        if not api_key:
            raise ValueError("WHO_MCP_API_KEY is required for Streamable HTTP")
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if not supplied or not secrets.compare_digest(supplied, self.api_key):
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
