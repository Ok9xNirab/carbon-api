"""Render JavaScript-built pages in headless Chromium, and the fetch → extract → render pipeline.

The browser is held to the same rules as ``fetch``: it may only reach public addresses. Two
layers enforce that:

- A route handler sees every request the page makes. It rejects non-http(s) URLs and hosts that
  resolve to non-public addresses, and drops images, fonts and media, which extraction never uses.
- Chromium follows redirects and opens WebSockets without consulting the route handler, so all
  of its traffic also goes through a local SOCKS5 proxy. The proxy resolves each host itself,
  vets every address, and connects to the vetted IP, so neither a redirect nor a DNS answer that
  changes after the route check can reach an internal address.

Playwright is an optional dependency (the ``render`` extra). Without it this module still imports;
``Renderer`` raises ``RenderUnavailable`` and ``get_page`` skips rendering.
"""

import asyncio
import contextlib
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, Self

from carbon.ingest.extract import Extracted, extract
from carbon.ingest.fetch import (
    DEFAULT_MAX_BYTES,
    USER_AGENT,
    BlockedURL,
    Fetcher,
    FetchError,
    FetchResult,
    Resolver,
    TooLarge,
    is_public_ip,
    parse_url,
    resolve_host,
    resolve_public,
    url_port,
)

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright, Route

logger = logging.getLogger(__name__)

DEFAULT_RENDER_TIMEOUT = 15.0
# Time kept back from the timeout to serialise the DOM once the page has settled.
CONTENT_RESERVE = 1.0
BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})
# Schemes that never touch the network.
LOCAL_SCHEMES = frozenset({"data", "blob", "about"})

PROXY_CONNECT_TIMEOUT = 5.0
PROXY_HANDSHAKE_TIMEOUT = 5.0


# --- Errors ----------------------------------------------------------------------------------


class RenderError(FetchError):
    """Base class for every failure to render a page."""


class RenderUnavailable(RenderError):
    """Playwright (or its Chromium build) is not installed."""


class RenderTimeout(RenderError):
    """The page did not load within the render timeout."""


class RenderFailed(RenderError):
    """The browser could not load the page."""


def render_available() -> bool:
    """True when Playwright is installed (the Chromium build may still be missing)."""
    return async_playwright is not None


# --- SOCKS5 guard proxy ----------------------------------------------------------------------

# RFC 1928 reply codes.
_SOCKS_OK = 0x00
_SOCKS_FAILURE = 0x01
_SOCKS_NOT_ALLOWED = 0x02
_SOCKS_HOST_UNREACHABLE = 0x04
_SOCKS_BAD_COMMAND = 0x07
_SOCKS_BAD_ADDRESS = 0x08


class _GuardProxy:
    """Minimal SOCKS5 proxy (CONNECT, no auth) that only connects to vetted addresses."""

    def __init__(self, resolver: Resolver, allow: Callable[[str], bool]) -> None:
        self._resolve = resolver
        self._allow = allow
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            self._tasks.discard(task)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with asyncio.timeout(PROXY_HANDSHAKE_TIMEOUT):
            version, n_methods = await reader.readexactly(2)
            methods = await reader.readexactly(n_methods)
            if version != 5 or 0x00 not in methods:
                writer.write(b"\x05\xff")
                return
            writer.write(b"\x05\x00")
            _, command, _, address_type = await reader.readexactly(4)
            if address_type == 0x01:
                host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
            elif address_type == 0x04:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            elif address_type == 0x03:
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode("ascii", errors="replace")
            else:
                await _socks_reply(writer, _SOCKS_BAD_ADDRESS)
                return
            port = int.from_bytes(await reader.readexactly(2))
            if command != 0x01:  # CONNECT only
                await _socks_reply(writer, _SOCKS_BAD_COMMAND)
                return
            try:
                addresses = await resolve_public(host, port, self._resolve, self._allow)
            except BlockedURL:
                await _socks_reply(writer, _SOCKS_NOT_ALLOWED)
                return
            except FetchError:
                await _socks_reply(writer, _SOCKS_HOST_UNREACHABLE)
                return
            try:
                async with asyncio.timeout(PROXY_CONNECT_TIMEOUT):
                    up_reader, up_writer = await asyncio.open_connection(addresses[0], port)
            except (OSError, TimeoutError):
                await _socks_reply(writer, _SOCKS_FAILURE)
                return
        await _socks_reply(writer, _SOCKS_OK)
        try:
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            up_writer.close()


async def _socks_reply(writer: asyncio.StreamWriter, code: int) -> None:
    # The bound address is not used by Chromium; report 0.0.0.0:0.
    writer.write(bytes([0x05, code, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
    await writer.drain()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    finally:
        with contextlib.suppress(OSError):
            writer.write_eof()


# --- Renderer --------------------------------------------------------------------------------


@dataclass(slots=True)
class _Blocked:
    navigation: bool = False  # the top-level page tried to go somewhere it may not


class Renderer:
    """Headless Chromium kept open across renders. Use as ``async with Renderer() as r``."""

    def __init__(
        self,
        *,
        resolver: Resolver = resolve_host,
        allow_address: Callable[[str], bool] = is_public_ip,
        timeout: float = DEFAULT_RENDER_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._resolve = resolver
        self._allow = allow_address
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._proxy = _GuardProxy(resolver, allow_address)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> Self:
        if async_playwright is None:
            raise RenderUnavailable("playwright is not installed")
        await self._proxy.start()
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                proxy={"server": f"socks5://127.0.0.1:{self._proxy.port}"},
            )
        except PlaywrightError as exc:  # typically: the Chromium build is not installed
            await self.aclose()
            raise RenderUnavailable("could not launch Chromium") from exc
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        await self._proxy.aclose()

    async def render(self, url: str) -> str:
        """Load ``url``, let its scripts run, and return the resulting DOM as HTML."""
        if self._browser is None:
            raise RuntimeError("Renderer is not open; use it as an async context manager")
        target = parse_url(url)
        await resolve_public(target.host, url_port(target), self._resolve, self._allow)

        context = await self._browser.new_context(
            user_agent=USER_AGENT,
            service_workers="block",  # their requests would bypass the route handler
            accept_downloads=False,
        )
        blocked = _Blocked()
        await context.route("**/*", lambda route: self._route(route, blocked))
        try:
            html = await self._load(context, str(target), blocked)
        finally:
            await context.close()
        if len(html.encode()) > self._max_bytes:
            raise TooLarge(f"rendered page is larger than {self._max_bytes} bytes")
        return html

    async def _load(self, context: "BrowserContext", url: str, blocked: _Blocked) -> str:
        page = await context.new_page()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        try:
            async with asyncio.timeout_at(deadline):
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                except PlaywrightError as exc:
                    if blocked.navigation:
                        raise BlockedURL("page navigated to a non-public address") from exc
                    raise RenderFailed("browser could not load the page") from exc
                # Give scripts time to fetch and render content. Pages that poll or stream never
                # go idle; render whatever is there when time runs out.
                settle = deadline - CONTENT_RESERVE - loop.time()
                if settle > 0:
                    with contextlib.suppress(PlaywrightTimeout):
                        await page.wait_for_load_state("networkidle", timeout=settle * 1000)
                if blocked.navigation:
                    raise BlockedURL("page navigated to a non-public address")
                return await page.content()
        except TimeoutError as exc:
            raise RenderTimeout("render took too long") from exc

    async def _route(self, route: "Route", blocked: _Blocked) -> None:
        request = route.request
        if request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort("blockedbyclient")
            return
        if request.url.split(":", 1)[0].lower() in LOCAL_SCHEMES:
            await route.continue_()
            return
        try:
            url = parse_url(request.url)
            await resolve_public(url.host, url_port(url), self._resolve, self._allow)
        except FetchError:
            if request.is_navigation_request() and request.frame.parent_frame is None:
                blocked.navigation = True
            await route.abort("blockedbyclient")
            return
        await route.continue_()


async def render(url: str, **options: Any) -> str:
    """Render one page in a browser launched for this call. ``options`` go to ``Renderer``."""
    async with Renderer(**options) as renderer:
        return await renderer.render(url)


# --- Pipeline --------------------------------------------------------------------------------


class PageRenderer(Protocol):
    async def render(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Page:
    fetched: FetchResult
    extracted: Extracted
    rendered: bool  # extracted came from the browser-rendered DOM, not the fetched HTML


async def get_page(url: str, *, fetcher: Fetcher, renderer: PageRenderer | None = None) -> Page:
    """Fetch and extract ``url``, re-extracting from a browser render when the page needs one.

    Without a renderer, or when rendering fails or does not find more text, the extraction of
    the fetched HTML is returned.
    """
    fetched = await fetcher.fetch(url)
    static = extract(fetched.html, fetched.final_url)
    if not static.needs_render or renderer is None:
        return Page(fetched, static, rendered=False)
    try:
        html = await renderer.render(fetched.final_url)
    except FetchError as exc:
        logger.warning(
            "render failed; using fetched HTML",
            extra={"error": type(exc).__name__, "static_words": static.word_count},
        )
        return Page(fetched, static, rendered=False)
    dynamic = extract(html, fetched.final_url)
    if dynamic.word_count <= static.word_count:
        return Page(fetched, static, rendered=False)
    return Page(fetched, dynamic, rendered=True)
