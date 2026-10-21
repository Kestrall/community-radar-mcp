"""Command line: run the MCP server, or produce the report and the docs index from the shell."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .docs import DocsIndex, build_index
from .report import render_html, render_telegram, snapshot
from .server import create_server
from .sources import USER_AGENT, Collector


def _days(request: Request, default: int = 7) -> int:
    try:
        return max(1, min(int(request.query_params.get("days", default)), 180))
    except ValueError:
        return default


def add_routes(server: MCPServer, collector: Collector, docs: DocsIndex) -> None:
    """Plain HTTP endpoints next to /mcp: the HTML report and a JSON digest for automations (n8n)."""

    @server.custom_route("/report", methods=["GET"])
    async def report(request: Request) -> Response:
        return HTMLResponse(render_html(await snapshot(collector, docs, _days(request))))

    @server.custom_route("/digest.json", methods=["GET"])
    async def digest(request: Request) -> Response:
        return JSONResponse({"messages": render_telegram(await snapshot(collector, docs, _days(request)))})

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "guides": len(docs.guides)})


def main() -> None:
    parser = argparse.ArgumentParser(prog="community-radar", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    serve.add_argument("--host", default=os.getenv("RADAR_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.getenv("RADAR_PORT", "8000")))
    report = sub.add_parser("report", help="write the HTML report")
    report.add_argument("--days", type=int, default=7)
    report.add_argument("-o", "--out", default="radar.html")
    digest = sub.add_parser("digest", help="print the Telegram digest messages as JSON")
    digest.add_argument("--days", type=int, default=7)
    build = sub.add_parser("build-docs", help="rebuild the index of official guides from github.com/ovh/docs")
    build.add_argument("-o", "--out", default=os.getenv("RADAR_DOCS_INDEX") or "docs_index.json")
    args = parser.parse_args()

    docs = DocsIndex.load(os.getenv("RADAR_DOCS_INDEX"))
    collector = Collector(ttl=float(os.getenv("RADAR_CACHE_TTL", "900")))

    if args.command == "report":
        html = render_html(asyncio.run(snapshot(collector, docs, args.days)))
        Path(args.out).write_text(html)
        print(Path(args.out).resolve())
    elif args.command == "digest":
        print(json.dumps(render_telegram(asyncio.run(snapshot(collector, docs, args.days))), ensure_ascii=False))
    elif args.command == "build-docs":

        async def run() -> DocsIndex:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
                return await build_index(c)

        index = asyncio.run(run())
        Path(args.out).write_text(index.to_json())
        print(f"{len(index.guides)} guides -> {Path(args.out).resolve()}")
    else:
        transport = getattr(args, "transport", "stdio")
        server = create_server(collector, docs)
        if transport == "stdio":
            server.run()
            return
        add_routes(server, collector, docs)
        allowed = [h.strip() for h in os.getenv("RADAR_ALLOWED_HOSTS", "").split(",") if h.strip()]
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(allowed),
            allowed_hosts=allowed,
            allowed_origins=[f"http://{h}" for h in allowed],
        )
        server.run("streamable-http", host=args.host, port=args.port, transport_security=security)


if __name__ == "__main__":
    main()
