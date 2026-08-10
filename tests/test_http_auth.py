from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp_service"))

from http_auth import BearerTokenMiddleware


async def invoke(headers):
    messages = []
    downstream_called = []

    async def downstream(scope, receive, send):
        downstream_called.append(True)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    middleware = BearerTokenMiddleware(downstream, "expected-secret")
    await middleware({"type": "http", "headers": headers}, receive, send)
    return messages, downstream_called


class BearerTokenMiddlewareTests(unittest.TestCase):
    def test_missing_or_wrong_token_is_rejected(self):
        for headers in ([], [(b"authorization", b"Bearer wrong")]):
            messages, called = asyncio.run(invoke(headers))
            self.assertEqual(messages[0]["status"], 401)
            self.assertFalse(called)

    def test_valid_bearer_token_reaches_mcp_app(self):
        messages, called = asyncio.run(invoke([(b"authorization", b"Bearer expected-secret")]))
        self.assertEqual(messages[0]["status"], 204)
        self.assertTrue(called)

    def test_empty_server_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, "WHO_MCP_API_KEY"):
            BearerTokenMiddleware(object(), "")
