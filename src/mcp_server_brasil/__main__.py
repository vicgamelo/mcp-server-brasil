"""Command line entry point.

mcp-server-brasil                      # stdio (Claude Desktop, Claude Code, Cursor...)
mcp-server-brasil --http --port 8000   # Streamable HTTP at /mcp, health check at /health
mcp-server-brasil --server noticias    # news and public information instead of the data tools
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .http_guard import ASGIApp, GuardConfig, HttpGuard, parse_networks
from .news import build_news_server
from .server import build_server

SERVERS: dict[str, Callable[[], MCPServer]] = {"brasil": build_server, "noticias": build_news_server}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.getenv
    parser = argparse.ArgumentParser(prog="mcp-server-brasil")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument(
        "--server",
        choices=sorted(SERVERS),
        default=env("MCP_SERVER", "brasil"),
        help="which tool set to serve: public data (brasil) or news and public information (noticias)",
    )
    parser.add_argument("--host", default=env("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("PORT", "8000")))
    parser.add_argument(
        "--allowed-hosts",
        default=env("MCP_ALLOWED_HOSTS", ""),
        help="comma separated Host headers accepted in HTTP mode (DNS rebinding protection)",
    )
    parser.add_argument(
        "--trusted-proxies",
        default=env("MCP_TRUSTED_PROXIES", ""),
        help="comma separated CIDRs whose X-Forwarded-For is trusted (e.g. your reverse proxy)",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=float(env("MCP_RATE_LIMIT", "30")),
        help="requests per minute per IP",
    )
    parser.add_argument("--rate-burst", type=int, default=int(env("MCP_RATE_BURST", "10")))
    parser.add_argument("--max-concurrency", type=int, default=int(env("MCP_MAX_CONCURRENCY", "16")))
    parser.add_argument("--max-body-bytes", type=int, default=int(env("MCP_MAX_BODY_BYTES", str(64 * 1024))))
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def build_http_app(args: argparse.Namespace, server: MCPServer | None = None) -> ASGIApp:
    server = server or SERVERS[args.server]()

    @server.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    allowed = [h.strip() for h in args.allowed_hosts.split(",") if h.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(allowed), allowed_hosts=allowed)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=security,
        max_request_body_size=args.max_body_bytes,
        host=args.host,
    )
    config = GuardConfig(
        rate_per_minute=args.rate_limit,
        burst=args.rate_burst,
        max_concurrency=args.max_concurrency,
        max_body_bytes=args.max_body_bytes,
        trusted_proxies=parse_networks(args.trusted_proxies),
    )
    return HttpGuard(app, config)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.http:
        SERVERS[args.server]().run("stdio")
        return

    import uvicorn

    uvicorn.run(
        build_http_app(args),
        host=args.host,
        port=args.port,
        # The guard is the only place that reads X-Forwarded-For, and only from trusted proxies.
        proxy_headers=False,
        server_header=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
