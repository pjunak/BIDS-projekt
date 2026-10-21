"""Shared fixtures: a local fake HTTP server, a fast config and a no-op sleep."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from bids_scrapers.config import Config


@dataclass
class Request:
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    raw_query: str = ""


@dataclass
class Response:
    status: int = 200
    body: bytes | str = b""
    headers: dict[str, str] = field(default_factory=dict)


class FakeServer:
    """Routes map a path (or a prefix ending in "*") to a callable(Request) -> Response,
    or to a fixed Response. Exact paths win over prefixes."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requests: list[Request] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                req = Request(parts.path, parse_qs(parts.query), dict(self.headers), parts.query)
                server.requests.append(req)
                route = server.routes.get(parts.path)
                if route is None:  # prefix routes: "/api/items/*"
                    route = next(
                        (r for p, r in server.routes.items() if p.endswith("*") and parts.path.startswith(p[:-1])),
                        None,
                    )
                if route is None:
                    resp = Response(404, b"not found")
                elif callable(route):
                    resp = route(req)
                else:
                    resp = route
                body = resp.body.encode("utf-8") if isinstance(resp.body, str) else resp.body
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def hits(self, path: str) -> list[Request]:
        return [r for r in self.requests if r.path == path]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    srv = FakeServer()
    yield srv
    srv.close()


@pytest.fixture
def sleeps(monkeypatch):
    """Replace time.sleep in the HTTP client with a recorder, so tests never wait."""
    recorded: list[float] = []
    monkeypatch.setattr("bids_scrapers.http.time.sleep", lambda s: recorded.append(s))
    return recorded


@pytest.fixture
def cfg(tmp_path, sleeps):
    c = Config(data_dir=tmp_path / "data")
    c.http.min_delay = c.http.max_delay = 0.0
    c.http.long_pause_every = 0
    c.http.max_retries = 2
    return c
