import asyncio
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from carbon.ingest import render as render_module
from carbon.ingest.fetch import BlockedURL, FetchResult, NotHTML
from carbon.ingest.render import (
    Renderer,
    RenderFailed,
    RenderTimeout,
    RenderUnavailable,
    _Blocked,
    _GuardProxy,
    get_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "html"
ARTICLE = (FIXTURES / "blog_lemire.html").read_text(encoding="utf-8")
SHELL = (FIXTURES / "shell_netlify_app.html").read_text(encoding="utf-8")
SHORT = "<html><body><h1>Loading</h1><p>Just a moment.</p></body></html>"

DNS = {
    "example.com": ["93.184.216.34"],
    "internal.example.com": ["10.0.0.5"],
    "mixed.example.com": ["93.184.216.34", "127.0.0.1"],
}


async def fake_resolve(host: str, port: int) -> list[str]:
    if host in DNS:
        return DNS[host]
    raise OSError("no such host")


# --- get_page: when to render ----------------------------------------------------------------


class StubFetcher:
    def __init__(self, html: str, final_url: str = "https://example.com/final") -> None:
        self.html = html
        self.final_url = final_url

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            url=url,
            final_url=self.final_url,
            status=200,
            content_type="text/html",
            html=self.html,
            fetched_at=datetime.now(UTC),
        )


@dataclass
class StubRenderer:
    html: str = ARTICLE
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def render(self, url: str) -> str:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.html


async def test_page_with_enough_text_is_not_rendered():
    renderer = StubRenderer()
    page = await get_page("https://example.com/", fetcher=StubFetcher(ARTICLE), renderer=renderer)
    assert renderer.calls == []
    assert not page.rendered
    assert not page.extracted.needs_render


@pytest.mark.parametrize("html", [SHELL, SHORT], ids=["js_shell", "short"])
async def test_page_that_needs_render_is_rendered(html):
    renderer = StubRenderer()
    page = await get_page("https://example.com/", fetcher=StubFetcher(html), renderer=renderer)
    assert renderer.calls == ["https://example.com/final"]  # the URL after redirects
    assert page.rendered
    assert page.extracted.word_count > 150
    assert page.fetched.html == html


async def test_no_renderer_keeps_fetched_extraction():
    page = await get_page("https://example.com/", fetcher=StubFetcher(SHORT), renderer=None)
    assert not page.rendered
    assert page.extracted.needs_render


@pytest.mark.parametrize(
    "error",
    [RenderTimeout("slow"), RenderFailed("crashed"), RenderUnavailable("no"), BlockedURL("x")],
)
async def test_render_failure_keeps_fetched_extraction(error):
    renderer = StubRenderer(error=error)
    page = await get_page("https://example.com/", fetcher=StubFetcher(SHORT), renderer=renderer)
    assert renderer.calls
    assert not page.rendered
    assert page.extracted.text.startswith("Loading")


async def test_render_with_no_more_text_keeps_fetched_extraction():
    renderer = StubRenderer(html="<html><body><p>Nothing here</p></body></html>")
    page = await get_page("https://example.com/", fetcher=StubFetcher(SHORT), renderer=renderer)
    assert not page.rendered
    assert page.extracted.text.startswith("Loading")


async def test_fetch_errors_propagate():
    class Failing:
        async def fetch(self, url: str) -> FetchResult:
            raise NotHTML("pdf")

    with pytest.raises(NotHTML):
        await get_page("https://example.com/", fetcher=Failing(), renderer=StubRenderer())


# --- route handler ---------------------------------------------------------------------------


@dataclass
class FakeFrame:
    parent_frame: "FakeFrame | None" = None


@dataclass
class FakeRequest:
    url: str
    resource_type: str = "document"
    navigation: bool = False
    frame: FakeFrame = field(default_factory=FakeFrame)

    def is_navigation_request(self) -> bool:
        return self.navigation


@dataclass
class FakeRoute:
    request: FakeRequest
    outcome: str | None = None

    async def abort(self, error_code: str = "failed") -> None:
        self.outcome = "abort"

    async def continue_(self) -> None:
        self.outcome = "continue"


async def route(url: str, **request: object) -> tuple[str | None, _Blocked]:
    fake = FakeRoute(FakeRequest(url, **request))
    blocked = _Blocked()
    await Renderer(resolver=fake_resolve)._route(fake, blocked)
    return fake.outcome, blocked


@pytest.mark.parametrize("kind", ["image", "font", "media"])
async def test_route_drops_heavy_resources(kind):
    outcome, _ = await route("https://example.com/a", resource_type=kind)
    assert outcome == "abort"


@pytest.mark.parametrize("kind", ["document", "script", "xhr", "fetch", "stylesheet"])
async def test_route_allows_public_requests(kind):
    outcome, _ = await route("https://example.com/a", resource_type=kind)
    assert outcome == "continue"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8080/",
        "https://internal.example.com/",
        "https://mixed.example.com/",
        "file:///etc/passwd",
        "ftp://example.com/",
        "https://nxdomain.example/",
    ],
)
async def test_route_blocks_non_public_requests(url):
    outcome, blocked = await route(url, resource_type="fetch")
    assert outcome == "abort"
    assert not blocked.navigation


async def test_route_flags_blocked_top_level_navigation():
    _, blocked = await route("http://10.1.2.3/", navigation=True)
    assert blocked.navigation


async def test_route_does_not_flag_blocked_iframe():
    _, blocked = await route("http://10.1.2.3/", navigation=True, frame=FakeFrame(FakeFrame()))
    assert not blocked.navigation


@pytest.mark.parametrize("url", ["data:text/plain,hi", "blob:https://example.com/1", "about:blank"])
async def test_route_lets_local_schemes_through(url):
    outcome, _ = await route(url, resource_type="fetch")
    assert outcome == "continue"


# --- guard proxy -----------------------------------------------------------------------------


async def socks_connect(proxy_port: int, host: str, port: int) -> tuple[int, object]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(b"\x05\x01\x00")
    assert await reader.readexactly(2) == b"\x05\x00"
    name = host.encode()
    writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2))
    reply = await reader.readexactly(10)
    return reply[1], (reader, writer)


@pytest.fixture
async def echo_server():
    async def echo(reader, writer):
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()


@pytest.fixture
async def proxy():
    async def resolve(host: str, port: int) -> list[str]:
        return {"public.test": ["127.0.0.1"], "private.test": ["10.0.0.5"]}.get(host, [host])

    guard = _GuardProxy(resolve, allow=lambda address: address == "127.0.0.1")
    await guard.start()
    yield guard
    await guard.aclose()


async def test_proxy_relays_to_allowed_host(proxy, echo_server):
    code, (reader, writer) = await socks_connect(proxy.port, "public.test", echo_server)
    assert code == 0x00
    writer.write(b"ping")
    assert await reader.read(100) == b"ping"
    writer.close()


async def test_proxy_refuses_non_public_host(proxy, echo_server):
    code, (_, writer) = await socks_connect(proxy.port, "private.test", echo_server)
    assert code == 0x02  # connection not allowed by ruleset
    writer.close()


async def test_proxy_refuses_ip_literal_that_is_not_allowed(proxy):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(b"\x05\x01\x00")
    await reader.readexactly(2)
    writer.write(b"\x05\x01\x00\x01" + bytes([10, 0, 0, 5]) + (80).to_bytes(2))
    assert (await reader.readexactly(10))[1] == 0x02
    writer.close()


# --- Playwright is optional ------------------------------------------------------------------


def test_module_imports_without_playwright():
    script = (
        "import sys; sys.modules['playwright'] = None\n"
        "from carbon.ingest import render\n"
        "assert not render.render_available()\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


async def test_renderer_is_unavailable_without_playwright(monkeypatch):
    monkeypatch.setattr(render_module, "async_playwright", None)
    with pytest.raises(RenderUnavailable):
        async with Renderer():
            pass
