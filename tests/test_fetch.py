import asyncio
import ipaddress

import httpx
import pytest
import respx

from carbon.ingest.fetch import (
    USER_AGENT,
    BadStatus,
    BlockedURL,
    Fetcher,
    FetchFailed,
    FetchTimeout,
    NotHTML,
    RobotsDisallowed,
    TooLarge,
    TooManyRedirects,
    is_public_ip,
)

# Fake DNS. Requests are pinned to the resolved IP, so respx routes match on IPs.
DNS = {
    "example.com": ["93.184.216.34"],
    "other.com": ["151.101.1.1"],
    "internal.example.com": ["10.0.0.5"],
    "mixed.example.com": ["93.184.216.34", "127.0.0.1"],
}
EXAMPLE = "93.184.216.34"
OTHER = "151.101.1.1"
HTML = {"content-type": "text/html; charset=utf-8"}
PAGE = "<html><body>Every brand deserves a system.</body></html>"


async def fake_resolve(host: str, port: int) -> list[str]:
    if host in DNS:
        return DNS[host]
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        raise OSError("no such host") from None


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        for ip in (EXAMPLE, OTHER):
            router.get(f"http://{ip}/robots.txt").respond(404)
            router.get(f"https://{ip}/robots.txt").respond(404)
        yield router


@pytest.fixture
async def fetcher():
    async with Fetcher(resolver=fake_resolve, max_bytes=1024) as f:
        yield f


# --- success ---


async def test_success(mock, fetcher):
    route = mock.get(f"https://{EXAMPLE}/page").respond(200, headers=HTML, text=PAGE)
    result = await fetcher.fetch("https://example.com/page#section")

    assert result.url == "https://example.com/page"
    assert result.final_url == "https://example.com/page"
    assert result.status == 200
    assert result.content_type == "text/html"
    assert result.html == PAGE
    assert result.fetched_at.tzinfo is not None

    request = route.calls.last.request
    assert request.headers["host"] == "example.com"
    assert request.headers["user-agent"] == USER_AGENT
    assert request.extensions["sni_hostname"] == "example.com"


async def test_follows_public_redirects(mock, fetcher):
    mock.get(f"http://{EXAMPLE}/old").respond(301, headers={"location": "https://other.com/new"})
    mock.get(f"https://{OTHER}/new").respond(200, headers=HTML, text=PAGE)
    result = await fetcher.fetch("http://example.com/old")
    assert result.url == "http://example.com/old"
    assert result.final_url == "https://other.com/new"
    assert result.html == PAGE


async def test_relative_redirect_and_xhtml(mock, fetcher):
    mock.get(f"https://{EXAMPLE}/a").respond(302, headers={"location": "/b"})
    mock.get(f"https://{EXAMPLE}/b").respond(
        200, headers={"content-type": "application/xhtml+xml"}, text=PAGE
    )
    result = await fetcher.fetch("https://example.com/a")
    assert result.final_url == "https://example.com/b"
    assert result.content_type == "application/xhtml+xml"


async def test_non_default_port_in_host_header(mock, fetcher):
    mock.get(f"http://{EXAMPLE}:8080/robots.txt").respond(404)
    route = mock.get(f"http://{EXAMPLE}:8080/").respond(200, headers=HTML, text=PAGE)
    await fetcher.fetch("http://example.com:8080/")
    assert route.calls.last.request.headers["host"] == "example.com:8080"


# --- SSRF ---


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://internal.example.com/",
        "http://127.0.0.1:9001/2018-06-01/runtime/invocation/next",
        "http://[::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://mixed.example.com/",
        "file:///etc/passwd",
    ],
)
async def test_redirect_to_private_address_is_blocked(mock, fetcher, location):
    mock.get(f"https://{EXAMPLE}/go").respond(302, headers={"location": location})
    with pytest.raises(BlockedURL) as exc_info:
        await fetcher.fetch("https://example.com/go")
    assert not isinstance(exc_info.value, RobotsDisallowed)
    # Only the robots.txt and the redirecting page were requested.
    assert {c.request.url.host for c in mock.calls} == {EXAMPLE}


@pytest.mark.parametrize(
    "url",
    [
        "http://10.1.2.3/",
        "http://192.168.0.1/",
        "http://172.16.0.1/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://localhost.internal.example.com/",
        "http://internal.example.com/",
        "ftp://example.com/",
        "javascript:alert(1)",
        "https://user:pass@example.com/",
        "not a url",
    ],
)
async def test_blocked_or_invalid_urls(mock, fetcher, url):
    with pytest.raises((BlockedURL, FetchFailed)):
        await fetcher.fetch(url)
    assert not mock.calls


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:4700::6810:85e5", True),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("192.0.2.1", False),  # documentation range
        ("240.0.0.1", False),  # reserved
        ("224.0.0.1", False),  # multicast
        ("::1", False),
        ("fe80::1", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),
        ("64:ff9b::a00:1", False),  # NAT64 of 10.0.0.1
    ],
)
def test_is_public_ip(address, public):
    assert is_public_ip(address) is public


# --- limits & content checks ---


async def test_oversized_body_by_content_length(mock, fetcher):
    mock.get(f"https://{EXAMPLE}/big").respond(200, headers=HTML, text="x" * 2048)
    with pytest.raises(TooLarge):
        await fetcher.fetch("https://example.com/big")


async def test_oversized_streamed_body_is_aborted(mock, fetcher):
    sent = 0

    async def chunks():
        nonlocal sent
        for _ in range(100):
            sent += 1
            yield b"x" * 256

    mock.get(f"https://{EXAMPLE}/stream").respond(200, headers=HTML, content=chunks())
    with pytest.raises(TooLarge):
        await fetcher.fetch("https://example.com/stream")
    assert sent < 10


@pytest.mark.parametrize("content_type", ["application/pdf", "application/json", "image/png", None])
async def test_non_html_rejected(mock, fetcher, content_type):
    headers = {"content-type": content_type} if content_type else {}
    mock.get(f"https://{EXAMPLE}/doc").respond(200, headers=headers, content=b"%PDF-1.7")
    with pytest.raises(NotHTML):
        await fetcher.fetch("https://example.com/doc")


async def test_error_status(mock, fetcher):
    mock.get(f"https://{EXAMPLE}/gone").respond(404, headers=HTML, text="nope")
    with pytest.raises(BadStatus) as exc_info:
        await fetcher.fetch("https://example.com/gone")
    assert exc_info.value.status == 404


def redirect_chain(mock, name: str, hops: int) -> str:
    """``hops`` redirects ending at an HTML page; returns the first URL."""
    for i in range(hops):
        mock.get(f"https://{EXAMPLE}/{name}{i}").respond(
            302, headers={"location": f"/{name}{i + 1}"}
        )
    mock.get(f"https://{EXAMPLE}/{name}{hops}").respond(200, headers=HTML, text=PAGE)
    return f"https://example.com/{name}0"


async def test_five_redirects_allowed(mock, fetcher):
    result = await fetcher.fetch(redirect_chain(mock, "ok", 5))
    assert result.final_url == "https://example.com/ok5"


async def test_six_redirects_rejected(mock, fetcher):
    with pytest.raises(TooManyRedirects):
        await fetcher.fetch(redirect_chain(mock, "long", 6))


# --- timeouts & network ---


async def test_read_timeout(mock, fetcher):
    mock.get(f"https://{EXAMPLE}/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchTimeout):
        await fetcher.fetch("https://example.com/slow")


async def test_connect_timeout(mock, fetcher):
    mock.get(f"https://{EXAMPLE}/slow").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(FetchTimeout):
        await fetcher.fetch("https://example.com/slow")


async def test_total_timeout(mock):
    async def drip():
        while True:
            await asyncio.sleep(0.05)
            yield b"x"

    mock.get(f"https://{EXAMPLE}/drip").respond(200, headers=HTML, content=drip())
    async with Fetcher(resolver=fake_resolve, total_timeout=0.2) as fetcher:
        with pytest.raises(FetchTimeout):
            await fetcher.fetch("https://example.com/drip")


async def test_connection_error(mock, fetcher):
    mock.get(f"https://{EXAMPLE}/down").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(FetchFailed):
        await fetcher.fetch("https://example.com/down")


async def test_unresolvable_host(mock, fetcher):
    with pytest.raises(FetchFailed):
        await fetcher.fetch("https://nowhere.invalid/")


# --- robots.txt ---


async def test_robots_disallow(mock, fetcher):
    mock.get(f"https://{OTHER}/robots.txt").respond(
        200, text="User-agent: CarbonBot\nDisallow: /private\n"
    )
    mock.get(f"https://{OTHER}/public").respond(200, headers=HTML, text=PAGE)
    page = mock.get(f"https://{OTHER}/private/x").respond(200, headers=HTML, text=PAGE)

    assert (await fetcher.fetch("https://other.com/public")).html == PAGE
    with pytest.raises(RobotsDisallowed):
        await fetcher.fetch("https://other.com/private/x")
    assert not page.called


async def test_robots_is_cached_per_origin(mock, fetcher):
    robots = mock.get(f"https://{OTHER}/robots.txt").respond(200, text="User-agent: *\nAllow: /\n")
    mock.get(url__regex=rf"https://{OTHER}/p\d").respond(200, headers=HTML, text=PAGE)
    for i in range(3):
        await fetcher.fetch(f"https://other.com/p{i}")
    assert robots.call_count == 1


async def test_robots_checked_on_redirect_target(mock, fetcher):
    mock.get(f"https://{OTHER}/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    mock.get(f"https://{EXAMPLE}/go").respond(302, headers={"location": "https://other.com/x"})
    with pytest.raises(RobotsDisallowed):
        await fetcher.fetch("https://example.com/go")


async def test_robots_server_error_disallows(mock, fetcher):
    mock.get(f"https://{OTHER}/robots.txt").respond(503)
    with pytest.raises(RobotsDisallowed):
        await fetcher.fetch("https://other.com/page")


async def test_robots_network_error_reports_network_error(mock, fetcher):
    mock.get(f"https://{OTHER}/robots.txt").mock(side_effect=httpx.ConnectError("tls failed"))
    with pytest.raises(FetchFailed):
        await fetcher.fetch("https://other.com/page")


async def test_robots_redirect_to_private_address_disallows(mock, fetcher):
    mock.get(f"https://{OTHER}/robots.txt").respond(
        302, headers={"location": "http://169.254.169.254/robots.txt"}
    )
    with pytest.raises(RobotsDisallowed):
        await fetcher.fetch("https://other.com/page")
