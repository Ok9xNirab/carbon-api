"""Work out when a page was published, and whether to believe it.

A page's own date (``article:published_time``, JSON-LD ``datePublished``, or whatever htmldate
can find) is only a claim: a copier can backdate it. The Wayback Machine's earliest capture of
the URL is independent evidence that the page existed by that day. The two are combined into a
single best date with a confidence level.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from urllib.parse import urldefrag

import htmldate
import httpx
import lxml.html
from lxml.etree import ParserError

CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_TIMEOUT = 5.0
# A claimed date this close before the first capture counts as corroborated by it.
CORROBORATION_WINDOW = timedelta(days=90)

Confidence = Literal["high", "medium", "low", "none"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublishInfo:
    claimed_date: date | None  # what the page says about itself
    first_seen_wayback: date | None  # the Wayback Machine's earliest capture of the URL
    best_date: date | None
    # high: the claim is corroborated by an early capture. medium: the date rests on Wayback
    # (alone, or overriding a later claim), or the claim predates the first capture by a lot.
    # low: only the page's own claim. none: no date at all.
    confidence: Confidence


# --- Claimed date ----------------------------------------------------------------------------


def claimed_date(html: str, url: str | None = None) -> date | None:
    """The publish date the page claims for itself, if it states one."""
    try:
        tree = lxml.html.document_fromstring(html)
    except (ParserError, ValueError):
        return None
    return _meta_published(tree) or _jsonld_published(tree) or _htmldate(tree, url)


def _meta_published(tree: lxml.html.HtmlElement) -> date | None:
    for content in tree.xpath(
        "//meta[@property='article:published_time' or @name='article:published_time']/@content"
    ):
        if parsed := _parse_date(content):
            return parsed
    return None


def _jsonld_published(tree: lxml.html.HtmlElement) -> date | None:
    for script in tree.xpath("//script[@type='application/ld+json']"):
        try:
            data = json.loads(script.text or "")
        except ValueError:
            continue
        for node in _jsonld_nodes(data):
            if parsed := _parse_date(node.get("datePublished")):
                return parsed
    return None


def _jsonld_nodes(data: object):
    """Every JSON object in a JSON-LD document, including those in lists and ``@graph``."""
    if isinstance(data, list):
        for item in data:
            yield from _jsonld_nodes(item)
    elif isinstance(data, dict):
        yield data
        yield from _jsonld_nodes(data.get("@graph"))


def _htmldate(tree: lxml.html.HtmlElement, url: str | None) -> date | None:
    try:
        found = htmldate.find_date(tree, original_date=True, url=url)
    except Exception:  # htmldate is heuristic; a failure there just means no date
        return None
    return _parse_date(found)


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# --- Wayback Machine -------------------------------------------------------------------------


async def first_seen_wayback(url: str, *, client: httpx.AsyncClient | None = None) -> date | None:
    """The date of the Wayback Machine's earliest successful capture of ``url``, if any.

    Any failure (timeout, error status, unexpected body) returns ``None``.
    """
    params = {
        "url": urldefrag(url).url,
        "limit": "1",
        "output": "json",
        "fl": "timestamp",
        "filter": "statuscode:200",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=WAYBACK_TIMEOUT) as own:
                response = await own.get(CDX_URL, params=params)
        else:
            response = await client.get(CDX_URL, params=params, timeout=WAYBACK_TIMEOUT)
        response.raise_for_status()
        rows = response.json()
        # [["timestamp"], ["20150321084512"]]; just [] when there are no captures.
        if len(rows) < 2:
            return None
        stamp = rows[1][0]
        return datetime.strptime(stamp[:8], "%Y%m%d").replace(tzinfo=UTC).date()
    except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError) as exc:
        logger.warning("wayback lookup failed", extra={"error": type(exc).__name__})
        return None


# --- Combining -------------------------------------------------------------------------------


def resolve(claimed: date | None, first_seen: date | None) -> PublishInfo:
    """Pick the best publish date from the page's claim and the first Wayback capture.

    The page existed by its first capture, so a claim later than that (or no claim) loses to it.
    """
    if claimed is None and first_seen is None:
        return PublishInfo(None, None, None, "none")
    if first_seen is None:
        return PublishInfo(claimed, None, claimed, "low")
    if claimed is None or claimed > first_seen:
        return PublishInfo(claimed, first_seen, first_seen, "medium")
    confidence: Confidence = "high" if first_seen - claimed <= CORROBORATION_WINDOW else "medium"
    return PublishInfo(claimed, first_seen, claimed, confidence)


async def publish_info(
    html: str, url: str, *, client: httpx.AsyncClient | None = None
) -> PublishInfo:
    """Resolve the publish date of the page at ``url`` whose HTML is ``html``."""
    return resolve(claimed_date(html, url), await first_seen_wayback(url, client=client))


# --- Comparing two sides ---------------------------------------------------------------------


def months_before(side_a: PublishInfo, side_b: PublishInfo) -> int | None:
    """Whole calendar months by which side B's best date precedes side A's.

    Negative when side B came later; ``None`` when either date is unknown.
    """
    a, b = side_a.best_date, side_b.best_date
    if a is None or b is None:
        return None
    if b > a:
        return -_whole_months(b, a)
    return _whole_months(a, b)


def describe_months_before(side_a: PublishInfo, side_b: PublishInfo) -> str | None:
    """Side B's date relative to side A's, e.g. "3 months before Side A"."""
    months = months_before(side_a, side_b)
    if months is None:
        return None
    a, b = side_a.best_date, side_b.best_date
    if a == b:
        return "same day as Side A"
    direction = "before" if b < a else "after"
    if months == 0:
        return f"less than a month {direction} Side A"
    n = abs(months)
    return f"{n} month{'s' if n != 1 else ''} {direction} Side A"


def _whole_months(later: date, earlier: date) -> int:
    months = (later.year - earlier.year) * 12 + later.month - earlier.month
    return months - 1 if later.day < earlier.day else months
