from datetime import date

import httpx
import pytest
import respx

from carbon.ingest.dates import (
    CDX_URL,
    PublishInfo,
    claimed_date,
    describe_months_before,
    first_seen_wayback,
    months_before,
    publish_info,
    resolve,
)


def page(head: str = "", body: str = "<p>Hello.</p>") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


# --- claimed date: each metadata source ---


def test_claimed_from_article_published_time():
    html = page('<meta property="article:published_time" content="2021-04-05T10:30:00+00:00">')
    assert claimed_date(html) == date(2021, 4, 5)


def test_claimed_from_article_published_time_with_z_suffix():
    html = page('<meta property="article:published_time" content="2021-04-05T23:30:00Z">')
    assert claimed_date(html) == date(2021, 4, 5)


def test_claimed_from_jsonld_date_published():
    html = page(
        '<script type="application/ld+json">'
        '{"@type": "BlogPosting", "headline": "Hi", "datePublished": "2019-11-02"}'
        "</script>"
    )
    assert claimed_date(html) == date(2019, 11, 2)


def test_claimed_from_jsonld_graph():
    html = page(
        '<script type="application/ld+json">{"@graph": ['
        '{"@type": "WebSite", "name": "Blog"},'
        '{"@type": "Article", "datePublished": "2018-07-14T08:00:00-05:00"}'
        "]}</script>"
    )
    assert claimed_date(html) == date(2018, 7, 14)


def test_claimed_skips_broken_jsonld():
    html = page(
        '<script type="application/ld+json">{not json</script>'
        '<script type="application/ld+json">[{"datePublished": "2017-01-09"}]</script>'
    )
    assert claimed_date(html) == date(2017, 1, 9)


def test_meta_wins_over_jsonld():
    html = page(
        '<meta property="article:published_time" content="2021-04-05">'
        '<script type="application/ld+json">{"datePublished": "2019-11-02"}</script>'
    )
    assert claimed_date(html) == date(2021, 4, 5)


def test_claimed_falls_back_to_htmldate():
    html = page(
        body='<article><time class="entry-date published" datetime="2016-03-22">'
        "March 22, 2016</time><p>Post text.</p></article>"
    )
    assert claimed_date(html) == date(2016, 3, 22)


def test_claimed_none_without_any_date():
    assert claimed_date(page()) is None


def test_claimed_none_for_unparseable_html():
    assert claimed_date("") is None


# --- Wayback ---


@pytest.fixture
def mock():
    with respx.mock(assert_all_mocked=True) as router:
        yield router


async def test_wayback_earliest_capture(mock):
    route = mock.get(CDX_URL).respond(200, json=[["timestamp"], ["20150321084512"]])
    assert await first_seen_wayback("https://example.com/post#top") == date(2015, 3, 21)

    params = route.calls.last.request.url.params
    assert params["url"] == "https://example.com/post"
    assert params["limit"] == "1"
    assert params["output"] == "json"


async def test_wayback_uses_given_client(mock):
    mock.get(CDX_URL).respond(200, json=[["timestamp"], ["20200101000000"]])
    async with httpx.AsyncClient() as client:
        assert await first_seen_wayback("https://example.com/", client=client) == date(2020, 1, 1)


async def test_wayback_no_captures(mock):
    mock.get(CDX_URL).respond(200, json=[])
    assert await first_seen_wayback("https://example.com/new") is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=[["timestamp"], ["garbage"]]),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
async def test_wayback_bad_response_returns_none(mock, response):
    mock.get(CDX_URL).mock(return_value=response)
    assert await first_seen_wayback("https://example.com/") is None


async def test_wayback_timeout_returns_none(mock):
    mock.get(CDX_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    assert await first_seen_wayback("https://example.com/") is None


async def test_wayback_connection_error_returns_none(mock):
    mock.get(CDX_URL).mock(side_effect=httpx.ConnectError("down"))
    assert await first_seen_wayback("https://example.com/") is None


# --- conflict rule ---


def test_resolve_nothing_known():
    assert resolve(None, None) == PublishInfo(None, None, None, "none")


def test_resolve_claim_only():
    assert resolve(date(2020, 5, 1), None) == PublishInfo(
        date(2020, 5, 1), None, date(2020, 5, 1), "low"
    )


def test_resolve_wayback_only():
    info = resolve(None, date(2020, 5, 1))
    assert info.best_date == date(2020, 5, 1)
    assert info.confidence == "medium"


def test_resolve_claim_corroborated_by_wayback():
    info = resolve(date(2020, 5, 1), date(2020, 5, 9))
    assert info.best_date == date(2020, 5, 1)
    assert info.confidence == "high"


def test_resolve_claim_same_day_as_capture():
    assert resolve(date(2020, 5, 1), date(2020, 5, 1)).confidence == "high"


def test_resolve_claim_later_than_capture_prefers_wayback():
    info = resolve(date(2023, 1, 10), date(2019, 6, 2))
    assert info.claimed_date == date(2023, 1, 10)
    assert info.first_seen_wayback == date(2019, 6, 2)
    assert info.best_date == date(2019, 6, 2)
    assert info.confidence == "medium"


def test_resolve_claim_long_before_capture_is_uncorroborated():
    info = resolve(date(2010, 1, 1), date(2020, 1, 1))
    assert info.best_date == date(2010, 1, 1)
    assert info.confidence == "medium"


async def test_publish_info_combines_both(mock):
    mock.get(CDX_URL).respond(200, json=[["timestamp"], ["20190602120000"]])
    html = page('<meta property="article:published_time" content="2023-01-10">')
    info = await publish_info(html, "https://example.com/post")
    assert info == PublishInfo(date(2023, 1, 10), date(2019, 6, 2), date(2019, 6, 2), "medium")


async def test_publish_info_survives_wayback_failure(mock):
    mock.get(CDX_URL).respond(500)
    html = page('<meta property="article:published_time" content="2023-01-10">')
    info = await publish_info(html, "https://example.com/post")
    assert info == PublishInfo(date(2023, 1, 10), None, date(2023, 1, 10), "low")


# --- months before side A ---


def dated(d: date | None) -> PublishInfo:
    return resolve(d, None)


@pytest.mark.parametrize(
    ("a", "b", "months", "text"),
    [
        (date(2024, 6, 15), date(2024, 3, 15), 3, "3 months before Side A"),
        (date(2024, 6, 15), date(2024, 3, 16), 2, "2 months before Side A"),
        (date(2024, 6, 15), date(2024, 5, 1), 1, "1 month before Side A"),
        (date(2024, 6, 15), date(2024, 6, 1), 0, "less than a month before Side A"),
        (date(2024, 6, 15), date(2024, 6, 15), 0, "same day as Side A"),
        (date(2024, 1, 10), date(2022, 1, 10), 24, "24 months before Side A"),
        (date(2024, 3, 15), date(2024, 6, 15), -3, "3 months after Side A"),
        (date(2024, 3, 15), date(2024, 3, 20), 0, "less than a month after Side A"),
    ],
)
def test_months_before(a, b, months, text):
    assert months_before(dated(a), dated(b)) == months
    assert describe_months_before(dated(a), dated(b)) == text


@pytest.mark.parametrize(("a", "b"), [(None, date(2024, 1, 1)), (date(2024, 1, 1), None)])
def test_months_before_unknown(a, b):
    assert months_before(dated(a), dated(b)) is None
    assert describe_months_before(dated(a), dated(b)) is None
