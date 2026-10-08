"""Normalize text for matching while remembering where every word came from.

Matching runs on normalized tokens, but highlights are drawn on the user's original text, so each
token carries the char offsets of the original span it was built from.

Per piece of text: NFKC, drop zero-width chars, lowercase, fold Cyrillic/Greek lookalikes to
Latin. Then letters, digits and combining marks form words; apostrophes and zero-width chars
vanish without splitting a word ("don't" -> "dont", "pla<ZWSP>giarism" -> "plagiarism"); any other
punctuation, symbol or whitespace separates words.

The text is processed in clusters (a char plus whatever NFKC would combine with it, such as
accents or Hangul jamo), so normalizing any token's original span on its own gives that token
back: ``normalize(text[t.start:t.end]) == t.text``.
"""

import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Token:
    text: str  # normalized
    start: int  # char offsets into the original text: original[start:end] produced `text`
    end: int


# U+00AD (soft hyphen) and U+2060 (word joiner) are invisible too and used the same way.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00ad\u2060"))
# Dropped without ending the word they sit in.
_APOSTROPHES = frozenset("'’ʼ")

# Applied after lowercasing. A letter is folded when either of its cases passes for Latin
# (Cyrillic "н" because "Н" looks like "H"); where the cases disagree the lowercase look wins,
# since body text is mostly lowercase (Greek "η" -> "n", not "h").
_HOMOGLYPHS = str.maketrans(
    {
        # Cyrillic
        "а": "a",
        "в": "b",
        "е": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "ѕ": "s",
        "і": "i",
        "ј": "j",
        "һ": "h",
        "ԁ": "d",
        "ԛ": "q",
        "ԝ": "w",
        "ӏ": "l",
        # Greek
        "α": "a",
        "β": "b",
        "γ": "y",
        "ε": "e",
        "ζ": "z",
        "η": "n",
        "ι": "i",
        "κ": "k",
        "μ": "u",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "ω": "w",
    }
)


def tokenize(text: str) -> list[Token]:
    """The normalized words of `text`, each with the span of `text` it came from."""
    tokens: list[Token] = []
    parts: list[str] = []
    start = end = 0
    for c_start, c_end in _clusters(text):
        folded = _fold(text[c_start:c_end])
        if folded is None:  # separator: ends the current word
            if parts:
                tokens.append(Token("".join(parts), start, end))
                parts = []
        elif folded:
            if not parts:
                start = c_start
            parts.append(folded)
            end = c_end
        # "" is invisible: it neither ends the word nor extends its span.
    if parts:
        tokens.append(Token("".join(parts), start, end))
    return tokens


def normalize(text: str) -> str:
    """`text` normalized to single-space-separated words."""
    return " ".join(t.text for t in tokenize(text))


def _fold(cluster: str) -> str | None:
    """A cluster's word chars, "" if it is invisible, or None if it separates words."""
    s = unicodedata.normalize("NFKC", cluster).translate(_ZERO_WIDTH).lower().translate(_HOMOGLYPHS)
    word = "".join(c for c in s if c.isalnum() or unicodedata.category(c)[0] == "M")
    if word:
        # A cluster can mix kinds ("½" is "1⁄2" after NFKC); word chars win, so it stays whole.
        return word
    if all(c in _APOSTROPHES for c in s):
        return ""
    return None


def _clusters(text: str) -> Iterator[tuple[int, int]]:
    """Split `text` into spans that NFKC normalizes independently of their neighbours."""
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and _joins(text[i:j], text[j]):
            j += 1
        yield i, j
        i = j


def _joins(cluster: str, nxt: str) -> bool:
    if nxt.isascii():
        return False
    if unicodedata.combining(nxt):
        return True
    nfkc = unicodedata.normalize
    return nfkc("NFKC", cluster + nxt) != nfkc("NFKC", cluster) + nfkc("NFKC", nxt)
