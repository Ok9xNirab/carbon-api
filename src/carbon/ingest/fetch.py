"""Fetch user-supplied URLs without letting them reach internal networks.

Every hop (the first request and each redirect) is checked before it is sent: the scheme must be
http(s), robots.txt must allow it, and the host must resolve only to public addresses. The request
then goes to the vetted IP itself (with the original ``Host`` header and TLS SNI), so a DNS answer
that changes between the check and the connect cannot redirect it to a private address.
"""

import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from urllib.robotparser import RobotFileParser

import httpx

from carbon import __version__

ROBOTS_TOKEN = "CarbonBot"
USER_AGENT = f"{ROBOTS_TOKEN}/{__version__} (+https://github.com/Ok9xNirab/carbon-api)"

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
ALLOWED_SCHEMES = frozenset({"http", "https"})

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
DEFAULT_TOTAL_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5

ROBOTS_MAX_BYTES = 512 * 1024
ROBOTS_TTL = 3600.0
# robots.txt that could not be read counts as "disallow all" (RFC 9309); retry sooner.
ROBOTS_ERROR_TTL = 300.0
ROBOTS_CACHE_SIZE = 256

NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

Resolver = Callable[[str, int], Awaitable[list[str]]]


# --- Errors ----------------------------------------------------------------------------------


class FetchError(Exception):
    """Base class for every failure to fetch a URL."""


class BlockedURL(FetchError):
    """The URL (or a redirect target) has a disallowed scheme or resolves to a non-public IP."""


class RobotsDisallowed(BlockedURL):
    """The site's robots.txt does not allow us to fetch the URL."""


class FetchTimeout(FetchError):
    """Connecting or reading took too long."""


class TooLarge(FetchError):
    """The response body is larger than the configured limit."""


class NotHTML(FetchError):
    """The response is not an HTML document."""


class TooManyRedirects(FetchError):
    """The redirect chain is longer than the configured limit."""


class BadStatus(FetchError):
    """The server answered with a 4xx or 5xx status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"server returned HTTP {status}")
        self.status = status


class FetchFailed(FetchError):
    """DNS or network failure."""


# --- Result ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str  # as requested
    final_url: str  # after redirects
    status: int
    content_type: str  # media type only, e.g. "text/html"
    html: str
    fetched_at: datetime


# --- Address checks --------------------------------------------------------------------------


def is_public_ip(address: str) -> bool:
    """True only for globally routable unicast addresses."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(str(ip.ipv4_mapped))
        if ip in NAT64_PREFIX:
            return is_public_ip(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    return ip.is_global and not ip.is_multicast


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _parse(url: str | httpx.URL) -> httpx.URL:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise BlockedURL("invalid URL") from exc
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(f"scheme {parsed.scheme!r} is not allowed")
    if not parsed.host:
        raise BlockedURL("URL has no host")
    if parsed.userinfo:
        raise BlockedURL("URLs with credentials are not allowed")
    return parsed.copy_with(fragment=None)


def _port(url: httpx.URL) -> int:
    return url.port or (443 if url.scheme == "https" else 80)


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip().lower()


# --- Fetcher ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _Body:
    url: httpx.URL
    status: int
    content_type: str
    text: str


@dataclass(slots=True)
class _Robots:
    expires_at: float
    parser: RobotFileParser | None  # None: robots.txt could not be read, disallow everything
    allow_all: bool = False


class Fetcher:
    """Safe HTTP fetcher. Use as ``async with Fetcher() as f: await f.fetch(url)``."""

    def __init__(
        self,
        *,
        resolver: Resolver = resolve_host,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolve = resolver
        self._total_timeout = total_timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._robots: OrderedDict[str, _Robots] = OrderedDict()
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,  # followed by hand so every hop is vetted
            trust_env=False,  # no proxies or .netrc from the environment
            headers={"User-Agent": USER_AGENT},
            # Connections are keyed by IP, so a kept-alive TLS connection could be reused for a
            # different hostname behind the same IP. Never reuse them.
            limits=httpx.Limits(max_keepalive_connections=0),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        """Fetch an HTML page, following up to ``max_redirects`` redirects."""
        start = _parse(url)
        try:
            async with asyncio.timeout(self._total_timeout):
                body = await self._follow(start, self._read_html, check_robots=True)
        except TimeoutError as exc:
            raise FetchTimeout("fetch took too long") from exc
        return FetchResult(
            url=str(start),
            final_url=str(body.url),
            status=body.status,
            content_type=body.content_type,
            html=body.text,
            fetched_at=datetime.now(UTC),
        )

    async def _follow(
        self,
        url: httpx.URL,
        read: Callable[[httpx.URL, httpx.Response], Awaitable[_Body]],
        *,
        check_robots: bool,
    ) -> _Body:
        for _ in range(self._max_redirects + 1):
            request = await self._pinned_request(url)
            if check_robots:
                await self._check_robots(url)
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise FetchTimeout("request timed out") from exc
            except httpx.TransportError as exc:
                raise FetchFailed("network error") from exc
            try:
                if response.is_redirect:
                    url = _parse(url.join(response.headers["location"]))
                    continue
                return await read(url, response)
            finally:
                await response.aclose()
        raise TooManyRedirects(f"more than {self._max_redirects} redirects")

    async def _pinned_request(self, url: httpx.URL) -> httpx.Request:
        """Request for ``url`` that connects to a vetted public IP of its host."""
        try:
            addresses = await self._resolve(url.host, _port(url))
        except OSError as exc:
            raise FetchFailed("could not resolve host") from exc
        if not addresses:
            raise FetchFailed("could not resolve host")
        if not all(is_public_ip(a) for a in addresses):
            raise BlockedURL("host resolves to a non-public address")
        extensions = {"sni_hostname": url.host} if url.scheme == "https" else {}
        return self._client.build_request(
            "GET",
            url.copy_with(host=addresses[0]),
            headers={"Host": url.netloc.decode("ascii")},
            extensions=extensions,
        )

    async def _read_body(self, response: httpx.Response, max_bytes: int) -> str:
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > max_bytes:
            raise TooLarge(f"body is larger than {max_bytes} bytes")
        body = bytearray()
        try:
            # Decompressed bytes, so the limit also stops compression bombs.
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise TooLarge(f"body is larger than {max_bytes} bytes")
        except httpx.TimeoutException as exc:
            raise FetchTimeout("read timed out") from exc
        except httpx.TransportError as exc:
            raise FetchFailed("network error") from exc
        try:
            return body.decode(response.charset_encoding or "utf-8", errors="replace")
        except LookupError:  # unknown charset name
            return body.decode("utf-8", errors="replace")

    async def _read_html(self, url: httpx.URL, response: httpx.Response) -> _Body:
        if response.status_code >= 400:
            raise BadStatus(response.status_code)
        content_type = _media_type(response)
        if content_type not in HTML_TYPES:
            raise NotHTML(f"content type {content_type or 'missing'!r} is not HTML")
        text = await self._read_body(response, self._max_bytes)
        return _Body(url, response.status_code, content_type, text)

    async def _read_any(self, url: httpx.URL, response: httpx.Response) -> _Body:
        if response.status_code >= 400:
            return _Body(url, response.status_code, _media_type(response), "")
        text = await self._read_body(response, ROBOTS_MAX_BYTES)
        return _Body(url, response.status_code, _media_type(response), text)

    # --- robots.txt ---

    async def _check_robots(self, url: httpx.URL) -> None:
        robots = await self._robots_for(url)
        if robots.allow_all:
            return
        if robots.parser is None or not robots.parser.can_fetch(ROBOTS_TOKEN, str(url)):
            raise RobotsDisallowed("robots.txt does not allow this URL")

    async def _robots_for(self, url: httpx.URL) -> _Robots:
        origin = f"{url.scheme}://{url.netloc.decode('ascii')}"
        cached = self._robots.get(origin)
        now = time.monotonic()
        if cached and cached.expires_at > now:
            self._robots.move_to_end(origin)
            return cached

        robots = await self._load_robots(httpx.URL(origin + "/robots.txt"), now)
        self._robots[origin] = robots
        self._robots.move_to_end(origin)
        while len(self._robots) > ROBOTS_CACHE_SIZE:
            self._robots.popitem(last=False)
        return robots

    async def _load_robots(self, robots_url: httpx.URL, now: float) -> _Robots:
        try:
            body = await self._follow(robots_url, self._read_any, check_robots=False)
        except (FetchFailed, FetchTimeout):
            # The site itself is unreachable; report that rather than a robots.txt refusal.
            raise
        except FetchError:
            return _Robots(now + ROBOTS_ERROR_TTL, parser=None)
        if 400 <= body.status < 500:  # no robots.txt: everything is allowed
            return _Robots(now + ROBOTS_TTL, parser=None, allow_all=True)
        if body.status >= 500:
            return _Robots(now + ROBOTS_ERROR_TTL, parser=None)
        parser = RobotFileParser()
        parser.parse(body.text.splitlines())
        return _Robots(now + ROBOTS_TTL, parser)
