"""Command line: run the MCP server, or produce the report and the docs index from the shell."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .docs import DocsIndex, build_index
from .queue_page import find_thread, load_queue, render_queue
from .report import SOURCE_NAMES, render_html, render_telegram, snapshot
from .server import create_server
from .sources import USER_AGENT, Collector
from .sources_page import render_sources, source_status


def _days(request: Request, default: int = 7) -> int:
    try:
        return max(1, min(int(request.query_params.get("days", default)), 180))
    except ValueError:
        return default


def add_routes(server: MCPServer, collector: Collector, docs: DocsIndex) -> None:
    """Plain HTTP endpoints next to /mcp: the HTML report and a JSON digest for automations (n8n)."""

    @server.custom_route("/report", methods=["GET"])
    async def report(request: Request) -> Response:
        source = request.query_params.get("source") or None
        if source and source not in SOURCE_NAMES:
            return JSONResponse({"error": f"source must be one of {', '.join(SOURCE_NAMES)}"}, status_code=400)
        return HTMLResponse(render_html(await snapshot(collector, docs, _days(request), source)))

    @server.custom_route("/digest.json", methods=["GET"])
    async def digest(request: Request) -> Response:
        return JSONResponse({"messages": render_telegram(await snapshot(collector, docs, _days(request)))})

    @server.custom_route("/triage", methods=["GET"])
    async def triage_page(request: Request) -> Response:
        days = _days(request, 30)
        demo = ", ".join(d.name for d in collector.discords.values() if d.enabled and d.demo)
        refresh = request.query_params.get("refresh") == "1"  # skip the cache, e.g. right after a demo message
        return HTMLResponse(render_queue(await load_queue(collector, days, refresh), days, demo))

    @server.custom_route("/api/triage/history", methods=["GET"])
    async def triage_history(request: Request) -> Response:
        events = collector.triage.history(request.query_params.get("url", ""))
        return JSONResponse(
            [
                {
                    "at": e.at.isoformat(),
                    "actor": e.actor,
                    "from_status": e.from_status,
                    "to_status": e.to_status,
                    "team": e.team,
                    "assignee": e.assignee,
                    "note": e.note,
                }
                for e in events
            ]
        )

    @server.custom_route("/api/triage", methods=["POST"])
    async def triage_update(request: Request) -> Response:
        # A custom header and a JSON body force a CORS preflight, which this server never grants: another website
        # open in the same browser cannot change statuses behind the user's back.
        if request.headers.get("x-radar-triage") != "1" or not request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return JSONResponse({"error": "JSON with the X-Radar-Triage header expected"}, status_code=400)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("JSON object expected")
            thread = await find_thread(collector, str(body.get("url", "")))
            if not thread:
                return JSONResponse({"error": "unknown thread (not seen by the radar)"}, status_code=404)
            ticket = collector.triage.update(
                thread,
                str(body.get("status", "")),
                note=str(body.get("note") or ""),
                team=str(body["team"]) if body.get("team") else None,
                assignee=str(body["assignee"]) if body.get("assignee") else None,
                actor=str(body.get("actor") or ""),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "status": ticket.status,
                "team": ticket.team,
                "assignee": ticket.assignee,
                "updated": ticket.updated.isoformat(),
            }
        )

    @server.custom_route("/sources", methods=["GET"])
    async def sources_page(request: Request) -> Response:
        status = await source_status(collector)
        return HTMLResponse(render_sources(collector.config.to_dict(), status, _days(request)))

    @server.custom_route("/api/sources", methods=["GET", "POST"])
    async def sources_api(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse(collector.config.to_dict())
        if request.headers.get("x-radar-triage") != "1" or not request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return JSONResponse({"error": "JSON with the X-Radar-Triage header expected"}, status_code=400)
        try:
            config = collector.save_sources(await request.json())
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(config.to_dict())

    @server.custom_route("/api/sources/feeds", methods=["GET"])
    async def feeds_api(request: Request) -> Response:
        """Feeds an external collector should read and push to /ingest (same bearer token as /ingest)."""
        token = os.getenv("RADAR_INGEST_TOKEN", "")
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return JSONResponse({"feeds": collector.config.imported_feeds()})

    @server.custom_route("/ingest", methods=["POST"])
    async def ingest(request: Request) -> Response:
        """Threads pushed by an external collector (n8n…): Authorization: Bearer $RADAR_INGEST_TOKEN."""
        token = os.getenv("RADAR_INGEST_TOKEN", "")
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not token:
            return JSONResponse({"error": "ingestion is off: set RADAR_INGEST_TOKEN"}, status_code=404)
        if not hmac.compare_digest(given.encode(), token.encode()):
            return JSONResponse({"error": "invalid token"}, status_code=401)
        if int(request.headers.get("content-length") or 0) > 2_000_000:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        try:
            result = collector.inbox.ingest(await request.json())
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(result)

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
