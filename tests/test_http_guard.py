"""HTTP hardening: rate limit per IP, trusted proxies, body size, concurrency."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest

from mcp_server_brasil import __main__ as cli
from mcp_server_brasil.http_guard import GuardConfig, HttpGuard, Receive, Scope, Send, parse_networks
from mcp_server_brasil.server import build_server
from mcp_server_brasil.sources import PublicDataClient


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": str(len(body)).encode()})


def client_for(app: Any, ip: str = "203.0.113.9") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://t")


@pytest.mark.anyio
async def test_burst_then_429_with_retry_after_then_refill() -> None:
    clock = Clock()
    guard = HttpGuard(echo_app, GuardConfig(rate_per_minute=60, burst=3), clock=clock)
    async with client_for(guard) as c:
        codes = [(await c.post("/mcp", content=b"{}")).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        limited = await c.post("/mcp", content=b"{}")
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "1"
        assert limited.json() == {"error": "rate limited"}
        clock.now += 1.0  # 60/min = one token per second
        assert (await c.post("/mcp", content=b"{}")).status_code == 200


@pytest.mark.anyio
async def test_limit_is_per_ip() -> None:
    guard = HttpGuard(echo_app, GuardConfig(rate_per_minute=60, burst=1), clock=Clock())
    async with client_for(guard, "198.51.100.1") as a, client_for(guard, "198.51.100.2") as b:
        assert (await a.get("/mcp")).status_code == 200
        assert (await a.get("/mcp")).status_code == 429
        assert (await b.get("/mcp")).status_code == 200


@pytest.mark.anyio
async def test_health_is_not_rate_limited() -> None:
    guard = HttpGuard(echo_app, GuardConfig(rate_per_minute=60, burst=1), clock=Clock())
    async with client_for(guard) as c:
        codes = [(await c.get("/health")).status_code for _ in range(5)]
        assert codes == [200] * 5


@pytest.mark.anyio
async def test_spoofed_forwarded_for_from_untrusted_peer_is_ignored() -> None:
    guard = HttpGuard(echo_app, GuardConfig(rate_per_minute=60, burst=1), clock=Clock())
    async with client_for(guard, "203.0.113.50") as c:
        assert (await c.get("/mcp", headers={"x-forwarded-for": "10.0.0.1"})).status_code == 200
        # a new fake IP per request must not buy a new bucket
        assert (await c.get("/mcp", headers={"x-forwarded-for": "10.0.0.2"})).status_code == 429
    assert list(guard.limiter.buckets) == ["203.0.113.50"]


@pytest.mark.anyio
async def test_forwarded_for_from_trusted_proxy_is_used() -> None:
    config = GuardConfig(rate_per_minute=60, burst=1, trusted_proxies=parse_networks("172.16.0.0/12"))
    guard = HttpGuard(echo_app, config, clock=Clock())
    async with client_for(guard, "172.18.0.5") as proxy:
        # the client prepends a fake hop; the proxy appends the real one: the rightmost untrusted wins
        hdr = {"x-forwarded-for": "1.2.3.4, 198.51.100.7"}
        assert (await proxy.get("/mcp", headers=hdr)).status_code == 200
        spoofed = {"x-forwarded-for": "9.9.9.9, 198.51.100.7"}
        assert (await proxy.get("/mcp", headers=spoofed)).status_code == 429
        assert (await proxy.get("/mcp", headers={"x-forwarded-for": "198.51.100.8"})).status_code == 200
    assert set(guard.limiter.buckets) == {"198.51.100.7", "198.51.100.8"}


@pytest.mark.anyio
async def test_garbage_forwarded_for_falls_back_to_peer() -> None:
    config = GuardConfig(trusted_proxies=parse_networks("172.16.0.0/12"))
    guard = HttpGuard(echo_app, config, clock=Clock())
    async with client_for(guard, "172.18.0.5") as proxy:
        await proxy.get("/mcp", headers={"x-forwarded-for": "not-an-ip"})
    assert list(guard.limiter.buckets) == ["172.18.0.5"]


@pytest.mark.anyio
async def test_large_body_by_content_length_is_413() -> None:
    guard = HttpGuard(echo_app, GuardConfig(max_body_bytes=1024), clock=Clock())
    async with client_for(guard) as c:
        assert (await c.post("/mcp", content=b"x" * 1024)).status_code == 200
        r = await c.post("/mcp", content=b"x" * 1025)
        assert r.status_code == 413


@pytest.mark.anyio
async def test_large_streamed_body_without_length_is_413() -> None:
    guard = HttpGuard(echo_app, GuardConfig(max_body_bytes=1024), clock=Clock())

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * 600

    async with client_for(guard) as c:
        r = await c.post("/mcp", content=chunks())
        assert "content-length" not in r.request.headers
        assert r.status_code == 413


@pytest.mark.anyio
async def test_concurrency_cap_returns_503() -> None:
    release = anyio.Event()
    both_in = anyio.Event()
    entered = 0

    async def slow_app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_in.set()
        await release.wait()
        await echo_app(scope, receive, send)

    guard = HttpGuard(slow_app, GuardConfig(max_concurrency=2, burst=100), clock=Clock())
    results: list[int] = []
    async with client_for(guard) as c:

        async def call() -> None:
            results.append((await c.get("/mcp")).status_code)

        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            tg.start_soon(call)
            await both_in.wait()
            busy = await c.get("/mcp")
            assert busy.status_code == 503
            assert busy.headers["retry-after"] == "1"
            release.set()
    assert results == [200, 200]
    assert guard.in_flight == 0


def test_bucket_table_stays_bounded() -> None:
    clock = Clock()
    guard = HttpGuard(echo_app, GuardConfig(rate_per_minute=60, burst=5, max_tracked_ips=100), clock=clock)
    for i in range(1000):
        guard.limiter.hit(f"10.0.{i // 250}.{i % 250}")
    assert len(guard.limiter.buckets) <= 100


def test_main_disables_uvicorn_proxy_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: uvicorn's default proxy_headers=True rewrites the client IP from
    X-Forwarded-For before the guard sees it, which would let anyone dodge the limit."""
    seen: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        seen.update(kwargs, app=app)

    monkeypatch.setitem(sys.modules, "uvicorn", type(sys)("uvicorn"))
    sys.modules["uvicorn"].run = fake_run  # type: ignore[attr-defined]
    cli.main(["--http", "--port", "9999"])
    assert seen["proxy_headers"] is False
    assert seen["server_header"] is False
    assert isinstance(seen["app"], HttpGuard)


@pytest.mark.anyio
async def test_guard_in_front_of_real_mcp_app() -> None:
    def no_network(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no upstream calls expected")

    data = PublicDataClient(http=httpx.AsyncClient(transport=httpx.MockTransport(no_network)))
    args = cli.parse_args(["--http", "--rate-burst", "2", "--max-body-bytes", "4096"])
    guard = cli.build_http_app(args, build_server(data))
    assert isinstance(guard, HttpGuard)
    inner: Any = guard.app
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

    def rpc(method: str, params: dict[str, Any] | None = None) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()

    async with inner.router.lifespan_context(inner), client_for(guard) as c:
        assert (await c.get("/health")).json()["status"] == "ok"
        doc = {"documento": "12.ABC.345/01DE-35"}
        call = rpc("tools/call", {"name": "validar_documento", "arguments": doc})
        ok = await c.post("/mcp", content=call, headers=headers)
        assert ok.status_code == 200
        assert '"valido":true' in ok.text.replace(" ", "")

        too_long = rpc("tools/call", {"name": "validar_documento", "arguments": {"documento": "1" * 33}})
        r = await c.post("/mcp", content=too_long, headers=headers)
        assert r.status_code == 200 and "muito longo" in r.text

        # rejected by size before taking a token; the next call is over the burst of 2
        big = await c.post("/mcp", content=b" " * 5000, headers=headers)
        assert big.status_code == 413
        assert (await c.post("/mcp", content=call, headers=headers)).status_code == 429


def test_parse_networks_rejects_garbage() -> None:
    assert parse_networks("") == []
    with pytest.raises(ValueError):
        parse_networks("not-a-cidr")
