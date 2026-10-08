from datetime import date
from functools import cache
from pathlib import Path

import lxml.html
import pytest

from carbon.ingest.extract import MIN_WORDS, Extracted, extract, looks_like_js_shell

FIXTURES = Path(__file__).parent / "fixtures" / "html"
PAGES = sorted(p.stem for p in FIXTURES.glob("*.html"))
RENDERED = [name for name in PAGES if not name.startswith("shell_")]


@cache
def html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@cache
def extracted(name: str) -> Extracted:
    return extract(html(name))


def page_text(name: str) -> str:
    """All text on the page, whitespace-collapsed: proves a phrase is really there to exclude."""
    return " ".join(lxml.html.document_fromstring(html(name)).text_content().split())


# --- main text vs boilerplate ---

# (page, phrases from the main content, phrases from nav/header/footer/sidebar/comments)
CONTENT = [
    (
        "blog_simonwillison",
        ["A lot has happened in the world of Large Language Models over the course of 2024"],
        ["More recent articles", "Part of series LLMs annual review"],
    ),
    (
        "blog_lemire",
        ["greatest common divisor"],
        [
            "Maks Verver says",  # comment byline
            "implements std::gcd() with Stein",  # comment body
            "Your business needs help?",  # sidebar
        ],
    ),
    (
        "marketing_basecamp",
        ["Manage projects, coordinate teams, and master your company with Basecamp."],
        ["Have an account?", "Pricing & sign up", "Have a great day!"],
    ),
    (
        "docs_python_textwrap",
        ["The textwrap module provides some convenience functions"],
        ["Python Software Foundation", "Previous topic", "Theme Auto Light Dark"],
    ),
    (
        "docs_mdn_article",
        ["represents a self-contained composition"],
        ["Your blueprint for a better internet", "Learn web development", "MDN Plus"],
    ),
]


@pytest.mark.parametrize(("name", "kept", "dropped"), CONTENT, ids=[c[0] for c in CONTENT])
def test_main_text_kept_and_boilerplate_excluded(name, kept, dropped):
    text = extracted(name).text
    for phrase in kept:
        assert phrase in text
    for phrase in dropped:
        assert phrase in page_text(name)
        assert phrase not in text


@pytest.mark.parametrize("name", PAGES)
def test_text_is_tidy(name):
    for line in extracted(name).text.splitlines():
        assert line
        assert line == " ".join(line.split())


# --- metadata ---

METADATA = {
    "blog_simonwillison": dict(
        title="Things we learned about LLMs in 2024",
        site_name="Simon Willison\u2019s Weblog",
        canonical_url="https://simonwillison.net/2024/Dec/31/llms-in-2024/",
        author="Simon Willison",
        published=date(2024, 12, 31),
    ),
    "blog_lemire": dict(
        title="Greatest common divisor, the extended Euclidean algorithm, and speed!",
        site_name="Daniel Lemire's blog",
        canonical_url="https://lemire.me/blog/2024/04/13/greatest-common-divisor-the-extended-euclidean-algorithm-and-speed/",
        author="Daniel Lemire",
        published=date(2024, 4, 13),
    ),
    "marketing_basecamp": dict(
        title="Basecamp",
        site_name="Basecamp",
        canonical_url="https://basecamp.com/",
    ),
    "docs_python_textwrap": dict(
        title="textwrap — Text wrapping and filling",
        site_name="Python documentation",
        canonical_url="https://docs.python.org/3/library/textwrap.html",
    ),
    # Fetched from /Web/HTML/Element/article; the page declares a different canonical URL.
    "docs_mdn_article": dict(
        title="<article> HTML article contents element",
        canonical_url="https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/article",
    ),
    "shell_netlify_app": dict(title="Netlify"),
}


@pytest.mark.parametrize("name", sorted(METADATA))
def test_metadata(name):
    result = extracted(name)
    for field, expected in METADATA[name].items():
        assert getattr(result, field) == expected, field


def test_canonical_url_falls_back_to_fetched_url():
    page = "<html><head><title>T</title></head><body><p>" + "word " * 200 + "</p></body></html>"
    assert extract(page, url="https://example.com/a").canonical_url == "https://example.com/a"


# --- needs_render ---


@pytest.mark.parametrize("name", RENDERED)
def test_server_rendered_pages_do_not_need_render(name):
    assert extracted(name).word_count >= MIN_WORDS
    assert not looks_like_js_shell(html(name))
    assert not extracted(name).needs_render


def test_js_shell_is_flagged():
    assert looks_like_js_shell(html("shell_netlify_app"))
    assert extracted("shell_netlify_app").needs_render


def test_short_page_needs_render():
    page = "<html><body><article><p>" + "word " * 40 + "</p></article></body></html>"
    result = extract(page)
    assert result.word_count < MIN_WORDS
    assert not looks_like_js_shell(page)
    assert result.needs_render


def test_shell_heuristic_on_synthetic_pages():
    bundle = "<script src='/static/app.js'></script>"
    assert looks_like_js_shell(f"<html><body><div id='root'></div>{bundle}</body></html>")
    inline = "<script>" + "var x = 1;" * 500 + "</script>"
    assert looks_like_js_shell(f"<html><body><div id='app'></div>{inline}</body></html>")
    # Small body but no scripts: just a short page.
    assert not looks_like_js_shell("<html><body><p>Hello there.</p></body></html>")
    # Plenty of server-rendered text alongside a bundle.
    text = "<p>" + "word " * 100 + "</p>"
    assert not looks_like_js_shell(f"<html><body>{text}{bundle}</body></html>")


def test_empty_document():
    result = extract("")
    assert result.text == ""
    assert result.needs_render
