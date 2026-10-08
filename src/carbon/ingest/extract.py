"""Pull the main text and metadata out of a fetched HTML page.

trafilatura does the extraction: it keeps the article body and drops navigation, headers,
footers, sidebars and comment threads. Pages whose content only appears after JavaScript runs
(single-page-app shells, or pages that extract to very little text) are flagged with
``needs_render`` so the caller can retry them through a headless browser.
"""

import re
from dataclasses import dataclass
from datetime import date

import lxml.html
import trafilatura
from lxml.etree import ParserError

# Fewer extracted words than this and the page probably needs a browser to render it.
MIN_WORDS = 150
# A JS shell has almost no visible text in its body...
SHELL_MAX_BODY_WORDS = 50
# ...and its weight is in scripts: an external bundle, or inline code making up this much of it.
SHELL_MIN_INLINE_SCRIPT_SHARE = 0.5

# "Related" / "recent posts" link lists that trafilatura's own heuristics sometimes keep.
PRUNE_XPATH = [
    "//*[contains(@class, 'recent-articles') or contains(@class, 'recent-posts')"
    " or contains(@class, 'related-articles') or contains(@class, 'related-posts')"
    " or contains(@class, 'widget_recent_entries')]",
]

_INVISIBLE = "//script|//style|//noscript|//template|//svg"
# Byline link text some themes put around the author's name.
_AUTHOR_PREFIX = re.compile(r"^(?:view all posts by|posts by|by)\s+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Extracted:
    text: str  # main text; one paragraph, heading or list item per line
    title: str | None
    site_name: str | None
    canonical_url: str | None  # the page's canonical/og:url, else the URL it was fetched from
    author: str | None
    published: date | None
    word_count: int
    needs_render: bool


def extract(html: str, url: str | None = None) -> Extracted:
    """Extract main text and metadata from ``html`` (fetched from ``url``, if known)."""
    doc = trafilatura.bare_extraction(
        html,
        url=url,
        with_metadata=True,
        include_comments=False,
        include_tables=True,
        include_images=False,
        include_links=False,
        prune_xpath=PRUNE_XPATH,
    )
    # bare_extraction gives up when there is no usable text; the metadata may still be there.
    meta = doc if doc is not None else trafilatura.extract_metadata(html, default_url=url)
    text = _tidy(doc.text) if doc is not None and doc.text else ""
    word_count = len(text.split())
    return Extracted(
        text=text,
        title=_clean(meta.title) if meta else None,
        site_name=_clean(meta.sitename) if meta else None,
        canonical_url=_clean(meta.url) if meta else url,
        author=_clean_author(meta.author) if meta else None,
        published=_parse_date(meta.date) if meta else None,
        word_count=word_count,
        needs_render=word_count < MIN_WORDS or looks_like_js_shell(html),
    )


def looks_like_js_shell(html: str) -> bool:
    """True for pages that are a near-empty body plus a script bundle that renders the content."""
    try:
        tree = lxml.html.document_fromstring(html)
    except (ParserError, ValueError):
        return False
    scripts = tree.xpath("//script")
    external = sum(1 for s in scripts if s.get("src"))
    inline_chars = sum(len(s.text or "") for s in scripts if not s.get("src"))

    for el in tree.xpath(_INVISIBLE):
        el.drop_tree()
    body = tree.find("body")
    body_words = len(body.text_content().split()) if body is not None else 0

    if body_words >= SHELL_MAX_BODY_WORDS:
        return False
    return external > 0 or inline_chars >= SHELL_MIN_INLINE_SCRIPT_SHARE * len(html)


def _tidy(text: str) -> str:
    """Collapse runs of whitespace inside each line and drop blank lines."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def _clean_author(value: str | None) -> str | None:
    value = _clean(value)
    return _clean(_AUTHOR_PREFIX.sub("", value)) if value else None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
