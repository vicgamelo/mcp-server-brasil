"""Command line entry point.

mcp-server-brasil                      # stdio (Claude Desktop, Claude Code, Cursor...)
mcp-server-brasil --http --port 8000   # Streamable HTTP at /mcp, health check at /health
"""

from __future__ import annotations

import argparse
import os

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .server import build_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-server-brasil")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument(
        "--allowed-hosts",
        default=os.getenv("MCP_ALLOWED_HOSTS", ""),
        help="comma separated Host headers accepted in HTTP mode (DNS rebinding protection)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    server = build_server()
    if not args.http:
        server.run("stdio")
        return

    @server.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    allowed = [h.strip() for h in args.allowed_hosts.split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed),
        allowed_hosts=allowed,
    )
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )


if __name__ == "__main__":
    main()
