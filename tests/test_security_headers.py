"""Every API response carries strict security headers; HSTS only over HTTPS."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from api.security_headers import HSTS_VALUE, SecurityHeadersMiddleware


def _app(https: bool) -> FastAPI:
    app = FastAPI()

    @app.get("/json")
    async def json_route() -> dict:
        return {"ok": True}

    @app.get("/stream")
    async def stream_route() -> StreamingResponse:
        async def gen():
            yield b"data: 1\n\n"
            yield b"data: 2\n\n"

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    app.add_middleware(SecurityHeadersMiddleware, https=https)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(path)


@pytest.mark.parametrize("path", ["/json", "/stream", "/missing"])
async def test_strict_headers_on_every_response(path):
    r = await _get(_app(https=False), path)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers  # plain HTTP: no HSTS


async def test_hsts_only_over_https():
    r = await _get(_app(https=True), "/json")
    assert r.headers["strict-transport-security"] == HSTS_VALUE


async def test_streams_still_stream_and_cache_header_is_not_duplicated():
    r = await _get(_app(https=False), "/stream")
    assert r.text == "data: 1\n\ndata: 2\n\n"
    assert r.headers.get_list("cache-control") == ["no-store"]


async def test_real_app_sends_the_headers(monkeypatch, tmp_path):
    from api.server import create_app
    from tests.test_api import serve

    monkeypatch.setenv("API_AUTH_DISABLED", "1")
    async with serve(create_app(db_path=tmp_path / "lab.db")) as c:
        r = await c.get("/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "server" not in r.headers
