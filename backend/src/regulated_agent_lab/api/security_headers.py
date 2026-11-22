"""Security headers on every API response.

The API only ever answers JSON (or server-sent events) to the console, so it can be strict:
nothing it returns may be rendered as a page, framed, sniffed into another type or cached by a
shared cache. HSTS is sent only when the API is served over HTTPS (``COOKIE_SECURE=1``, the same
switch as the session cookie's ``Secure`` flag), because browsers ignore it over plain HTTP and a
wrong HSTS on localhost would pin other local apps to HTTPS.
"""

from __future__ import annotations

import os

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HSTS_ENV = "COOKIE_SECURE"

BASE_HEADERS: dict[str, str] = {
    # A JSON API: no document, no subresources, no framing, no form posts from its responses.
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
    # Runs, audit entries and receipts are per user: never stored by a shared cache.
    "cache-control": "no-store",
}
HSTS_VALUE = "max-age=31536000; includeSubDomains"


def security_headers(https: bool) -> dict[str, str]:
    headers = dict(BASE_HEADERS)
    if https:
        headers["strict-transport-security"] = HSTS_VALUE
    return headers


class SecurityHeadersMiddleware:
    """Pure ASGI middleware, so streamed (SSE) responses keep streaming."""

    def __init__(self, app: ASGIApp, https: bool | None = None) -> None:
        self.app = app
        if https is None:
            https = os.environ.get(HSTS_ENV) == "1"
        self.headers = [(k.encode(), v.encode()) for k, v in security_headers(https).items()]
        self.names = {k for k, _ in self.headers}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in self.names and k.lower() != b"server"
                ]
                message["headers"] = kept + self.headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
