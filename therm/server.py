"""Local HTTP server for the display.

Serves the static UI and a single JSON endpoint the page polls. Binds to
localhost by default: the API key stays in this process and is never sent to
the browser, and nothing is exposed to the network unless asked for.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .poller import Poller

_LOG = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

_SAFE_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".ico", ".woff2", ".json"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "therm"
    protocol_version = "HTTP/1.1"

    # Injected by make_server.
    poller: Poller
    config: Config
    web_root: Path

    def log_message(self, fmt: str, *args) -> None:
        # Route request logging through logging at debug level instead of
        # writing to stderr on every poll.
        _LOG.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        if path in ("/api/state", "/api/state/"):
            self._serve_state()
        elif path in ("/api/refresh", "/api/refresh/"):
            self.poller.refresh_now()
            self._send_json({"ok": True})
        elif path in ("/healthz", "/healthz/"):
            self._send_json({"status": "ok"})
        else:
            self._serve_static(path)

    def _serve_state(self) -> None:
        try:
            payload = self.poller.state()
        except Exception as exc:  # noqa: BLE001 - never let the UI get a blank 500
            _LOG.exception("Failed to build state")
            payload = {
                "connection": "error",
                "error": f"Internal error building state: {exc}",
                "panels": [],
            }
        self._send_json(payload)

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Always-changing data: never let the browser or a proxy hold onto it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")

        # Resolve and confirm the result is still inside the web root, so a
        # crafted path cannot read arbitrary files.
        candidate = (self.web_root / relative).resolve()
        try:
            candidate.relative_to(self.web_root.resolve())
        except ValueError:
            self._send_error_page(HTTPStatus.FORBIDDEN, "Forbidden")
            return

        if not candidate.is_file() or candidate.suffix.lower() not in _SAFE_SUFFIXES:
            self._send_error_page(HTTPStatus.NOT_FOUND, "Not found")
            return

        try:
            body = candidate.read_bytes()
        except OSError as exc:
            _LOG.warning("Could not read %s: %s", candidate, exc)
            self._send_error_page(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read file")
            return

        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_page(self, status: HTTPStatus, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(config: Config, poller: Poller, web_root: Path | None = None) -> ThreadingHTTPServer:
    root = (web_root or WEB_ROOT).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Web assets directory not found: {root}")

    handler = type(
        "BoundHandler",
        (_Handler,),
        {"poller": poller, "config": config, "web_root": root},
    )
    httpd = ThreadingHTTPServer((config.host, config.port), handler)
    httpd.daemon_threads = True
    return httpd
