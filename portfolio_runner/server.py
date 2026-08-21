"""Read-only dashboard server for the in-memory portfolio.

Standard library only, so the research runner needs no extra dependencies.
Routes are whitelisted (no filesystem path is ever derived from the request) and
the server binds to localhost by default.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .store import PortfolioStore

_INDEX = Path(__file__).with_name("static") / "index.html"


def _make_handler(store: PortfolioStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            route = self.path.split("?", 1)[0].rstrip("/") or "/"

            if route == "/":
                try:
                    body = _INDEX.read_bytes()
                except OSError:
                    self._send(500, b"dashboard asset missing", "text/plain; charset=utf-8")
                    return
                self._send(200, body, "text/html; charset=utf-8")
            elif route == "/api/state":
                payload = json.dumps(store.snapshot()).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def log_message(self, *args) -> None:
            pass  # keep the console reserved for engine output

    return Handler


def start_server(store: PortfolioStore, host: str = "127.0.0.1", port: int = 8765):
    """Start the dashboard on a daemon thread and return the server object."""
    server = ThreadingHTTPServer((host, port), _make_handler(store))
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    return server
