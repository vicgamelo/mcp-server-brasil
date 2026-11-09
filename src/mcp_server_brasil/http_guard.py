"""Protection for the public HTTP endpoint: per-IP rate limit, concurrency cap and body size.

Pure ASGI middleware, so it runs before the MCP transport parses anything.

- Client IP is the socket peer. ``X-Forwarded-For`` is only read when the peer is a trusted
  proxy, walking the header from the right and stopping at the first untrusted address, so a
  client can't pick its own IP by sending the header.
- Token bucket per IP (``rate_per_minute`` refill, ``burst`` capacity). Over the limit: 429 with
  ``Retry-After``. Idle buckets are pruned so memory stays bounded under a flood of addresses.
- At most ``max_concurrency`` requests in flight; beyond that 503 right away instead of queueing.
- Bodies larger than ``max_body_bytes`` get 413, by ``Content-Length`` or while streaming.
"""

from __future__ import annotations

import ipaddress
import json
import math
import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(value: str | Iterable[str]) -> list[Network]:
    items = value.split(",") if isinstance(value, str) else value
    return [ipaddress.ip_network(i.strip(), strict=False) for i in items if i.strip()]


@dataclass
class GuardConfig:
    rate_per_minute: float = 30.0
    burst: int = 10
    max_concurrency: int = 16
    max_body_bytes: int = 64 * 1024
    trusted_proxies: list[Network] = field(default_factory=list)
    exempt_paths: frozenset[str] = frozenset({"/health"})
    max_tracked_ips: int = 50_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class _BodyTooLarge(Exception):
    pass


class RateLimiter:
    def __init__(
        self, rate_per_minute: float, burst: int, max_tracked: int, clock: Callable[[], float]
    ) -> None:
        self.rate = rate_per_minute / 60.0
        self.burst = float(burst)
        self.max_tracked = max_tracked
        self.clock = clock
        self.buckets: dict[str, _Bucket] = {}

    def hit(self, key: str) -> float:
        """Take one token. Returns 0 when allowed, else seconds until the next token."""
        now = self.clock()
        bucket = self.buckets.get(key)
        if bucket is None:
            if len(self.buckets) >= self.max_tracked:
                self._prune(now)
            bucket = self.buckets[key] = _Bucket(self.burst, now)
        else:
            bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
            bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return 0.0
        return (1 - bucket.tokens) / self.rate

    def _prune(self, now: float) -> None:
        full_after = self.burst / self.rate  # an idle bucket is full again after this long
        self.buckets = {k: b for k, b in self.buckets.items() if now - b.updated < full_after}
        if len(self.buckets) >= self.max_tracked:  # still full: drop the oldest half
            keep = sorted(self.buckets.items(), key=lambda kv: kv[1].updated)[len(self.buckets) // 2 :]
            self.buckets = dict(keep)


class HttpGuard:
    def __init__(
        self, app: ASGIApp, config: GuardConfig, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.app = app
        self.config = config
        self.limiter = RateLimiter(config.rate_per_minute, config.burst, config.max_tracked_ips, clock)
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") in self.config.exempt_paths:
            await self.app(scope, receive, send)
            return

        length = _header(scope, b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.config.max_body_bytes):
            await _reply(send, 413, {"error": "request body too large"})
            return

        wait = self.limiter.hit(self.client_ip(scope))
        if wait:
            await _reply(send, 429, {"error": "rate limited"}, retry_after=wait)
            return

        if self.in_flight >= self.config.max_concurrency:
            await _reply(send, 503, {"error": "server busy"}, retry_after=1)
            return

        self.in_flight += 1
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, self._limited_receive(receive), tracking_send)
        except _BodyTooLarge:
            if not started:
                await _reply(send, 413, {"error": "request body too large"})
        finally:
            self.in_flight -= 1

    def client_ip(self, scope: Scope) -> str:
        client = scope.get("client")
        peer = client[0] if client else ""
        if not self._trusted(peer):
            return peer
        forwarded = _header(scope, b"x-forwarded-for")
        if not forwarded:
            return peer
        for hop in reversed([h.strip() for h in forwarded.split(",")]):
            if not _is_ip(hop):
                return peer  # garbage in the header: don't trust any of it
            if not self._trusted(hop):
                return hop
        return peer

    def _trusted(self, ip: str) -> bool:
        if not ip or not self.config.trusted_proxies:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.config.trusted_proxies)

    def _limited_receive(self, receive: Receive) -> Receive:
        total = 0
        limit = self.config.max_body_bytes

        async def wrapped() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise _BodyTooLarge
            return message

        return wrapped


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


async def _reply(send: Send, status: int, body: dict[str, str], retry_after: float | None = None) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    if retry_after is not None:
        headers.append((b"retry-after", str(max(1, math.ceil(retry_after))).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
