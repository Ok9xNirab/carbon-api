"""Renders real pages in headless Chromium. Slow, so deselected by default: ``pytest -m slow``.

Needs the ``render`` extra and a Chromium build: ``uv sync --extra render`` then
``uv run playwright install chromium``.
"""

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright")

from carbon.ingest.fetch import BlockedURL
from carbon.ingest.render import Renderer, RenderTimeout, render

pytestmark = pytest.mark.slow

PARAGRAPH = "Rendered paragraph number {} that only exists after the page script runs."

JS_PAGE = """<!doctype html><html><head><title>App</title></head><body>
<div id="root"></div>
<img src="/pixel.png">
<script>
  fetch("/api/content").then(r => r.json()).then(items => {
    const root = document.getElementById("root");
    for (const text of items) {
      const p = document.createElement("p");
      p.textContent = text;
      root.appendChild(p);
    }
  });
</script>
</body></html>"""

# Fetches a public URL that redirects to a private one; the guard must stop the second hop.
REDIRECT_PAGE = """<!doctype html><html><body><p id="out">pending</p>
<script>
  fetch("/hop").then(r => r.text()).then(
    t => { document.getElementById("out").textContent = t; },
    () => { document.getElementById("out").textContent = "blocked"; });
</script>
</body></html>"""

HANGING_PAGE = """<!doctype html><html><body><script src="/slow.js"></script></body></html>"""


class DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


@pytest.fixture(scope="module")
def site() -> Iterator[tuple[int, list[str]]]:
    """Local site on 127.0.0.1 and ::1. Yields its port and the paths it was asked for."""
    requested: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.append(self.path)
            port = self.server.server_address[1]
            if self.path == "/hop":
                self.send_response(302)
                self.send_header("Location", f"http://private.test:{port}/secret")
                self.end_headers()
                return
            if self.path == "/slow.js":
                threading.Event().wait(5)
            bodies = {
                "/app": ("text/html", JS_PAGE),
                "/redirect": ("text/html", REDIRECT_PAGE),
                "/hang": ("text/html", HANGING_PAGE),
                "/api/content": (
                    "application/json",
                    "[" + ",".join(f'"{PARAGRAPH.format(i)}"' for i in range(40)) + "]",
                ),
                "/secret": ("text/plain", "internal secret"),
            }
            content_type, body = bodies.get(self.path, ("text/plain", ""))
            self.send_response(200 if self.path in bodies else 404)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args: object) -> None:
            pass

    server = DualStackServer(("::", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], requested
    server.shutdown()


async def resolve(host: str, port: int) -> list[str]:
    return {"public.test": ["127.0.0.1"], "private.test": ["::1"]}.get(host, [host])


def only_ipv4_loopback(address: str) -> bool:
    """Stands in for is_public_ip: 127.0.0.1 plays the public internet, ::1 the private one."""
    return address == "127.0.0.1"


@pytest.fixture
async def renderer():
    async with Renderer(resolver=resolve, allow_address=only_ipv4_loopback, timeout=10) as r:
        yield r


async def test_renders_javascript_content(renderer, site):
    port, requested = site
    html = await renderer.render(f"http://public.test:{port}/app")
    assert PARAGRAPH.format(0) in html
    assert PARAGRAPH.format(39) in html
    assert "/api/content" in requested
    assert "/pixel.png" not in requested  # images are dropped


async def test_redirect_to_private_address_is_not_followed(renderer, site):
    port, requested = site
    html = await renderer.render(f"http://public.test:{port}/redirect")
    assert "/hop" in requested
    assert "/secret" not in requested
    assert "internal secret" not in html
    assert ">blocked<" in html


async def test_private_page_is_refused(renderer, site):
    port, _ = site
    with pytest.raises(BlockedURL):
        await renderer.render(f"http://private.test:{port}/app")


async def test_loopback_is_refused_by_default(site):
    port, _ = site
    with pytest.raises(BlockedURL):
        await render(f"http://127.0.0.1:{port}/app")


async def test_slow_page_times_out(site):
    port, _ = site
    async with Renderer(resolver=resolve, allow_address=only_ipv4_loopback, timeout=2) as r:
        with pytest.raises(RenderTimeout):
            await r.render(f"http://public.test:{port}/hang")
