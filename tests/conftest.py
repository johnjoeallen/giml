import http.server
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from hypothesis import settings

# Deterministic by default (principles: same inputs, same outputs). Set
# HYPOTHESIS_PROFILE=explore to search with fresh random examples and more of them.
settings.register_profile("default", derandomize=True)
settings.register_profile("explore", max_examples=2000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@dataclass
class Route:
    body: bytes = b""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class LocalHttpServer:
    """A loopback HTTP server serving fixed routes; records every request it receives."""

    base_url: str
    routes: dict[str, Route]
    requests: list[tuple[str, str, str]]  # (method, path, User-Agent)


@pytest.fixture
def http_server() -> Iterator[LocalHttpServer]:
    routes: dict[str, Route] = {}
    requests: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _respond(self, include_body: bool) -> None:
            with lock:
                requests.append((self.command, self.path, self.headers.get("User-Agent", "")))
            route = routes.get(self.path, Route(status=404))
            self.send_response(route.status)
            for name, value in route.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(route.body)))
            self.end_headers()
            if include_body:
                self.wfile.write(route.body)

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            self._respond(include_body=True)

        def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
            self._respond(include_body=False)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield LocalHttpServer(f"http://127.0.0.1:{server.server_address[1]}", routes, requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
