"""The web process: landing, privacy and installed pages, /healthz, /install and the GitHub webhook."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import db
from .config import Settings
from .poller import latest_version
from .webhooks import HANDLED_EVENTS, MAX_BODY, BadPayload, handle_event, verify_signature

APP_DIR = Path(__file__).resolve().parent
ROBOTS = "User-agent: *\nAllow: /\n"
DESCRIPTIONS = {
    "apidrift": "Stripe changed its API. apidrift opens one GitHub issue that links to the line in your code that breaks.",
    "Privacy": "What apidrift reads, stores and deletes when it checks a repository.",
    "Installed": "apidrift is installed; affected repositories get one issue within a few minutes.",
}


def load_demo() -> dict:
    return json.loads((APP_DIR / "demo.json").read_text(encoding="utf-8"))


def create_app(settings: Settings, conn: sqlite3.Connection | None = None) -> Starlette:
    """Build the Starlette app; ``conn`` lets tests share an in-memory database."""
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    demo = load_demo()
    connection = conn if conn is not None else db.connect(settings.db_path)
    db.migrate(connection)
    lock = threading.Lock()

    def context(request: Request, title: str) -> dict:
        return {
            "request": request,
            "title": title,
            "description": DESCRIPTIONS.get(title),
            "stripe_latest": latest_version(settings.data),
            "host": settings.host,
            "install_url": "/install",
        }

    async def landing(request: Request) -> Response:
        return templates.TemplateResponse(request, "landing.html", {**context(request, "apidrift"), "demo": demo})

    async def privacy(request: Request) -> Response:
        return templates.TemplateResponse(request, "privacy.html", context(request, "Privacy"))

    async def installed(request: Request) -> Response:
        return templates.TemplateResponse(request, "installed.html", context(request, "Installed"))

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "stripe_latest": latest_version(settings.data)})

    async def install(request: Request) -> Response:
        return RedirectResponse(settings.install_url, status_code=302)

    async def robots(request: Request) -> Response:
        return PlainTextResponse(ROBOTS)

    def handle(event: str, delivery: str, payload: dict) -> list[str]:
        with lock:
            return handle_event(connection, event, delivery, payload, settings.data)

    async def webhook(request: Request) -> Response:
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return Response("content-length required", status_code=411)
        if int(length) > MAX_BODY:
            return Response("payload too large", status_code=413)
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response("payload too large", status_code=413)
        if not verify_signature(settings.github_webhook_secret, body, request.headers.get("x-hub-signature-256")):
            return Response("bad signature", status_code=401)
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery", "")
        if not event or not delivery:
            return Response("missing event headers", status_code=400)
        if event not in HANDLED_EVENTS:
            return Response(status_code=204)
        try:
            payload = json.loads(body)
        except ValueError:
            return Response("invalid json", status_code=400)
        try:
            actions = await run_in_threadpool(handle, event, delivery, payload)
        except BadPayload as exc:
            return Response(str(exc), status_code=400)
        return JSONResponse({"ok": True, "actions": actions})

    return Starlette(
        routes=[
            Route("/", landing, methods=["GET"]),
            Route("/privacy", privacy, methods=["GET"]),
            Route("/installed", installed, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/install", install, methods=["GET"]),
            Route("/robots.txt", robots, methods=["GET"]),
            Route("/github/webhook", webhook, methods=["POST"]),
            Mount("/static", StaticFiles(directory=str(APP_DIR / "static"), check_dir=False), name="static"),
        ]
    )
