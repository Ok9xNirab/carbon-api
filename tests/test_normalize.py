from hypothesis import given
from hypothesis import strategies as st

from carbon.core.normalize import Token, normalize, tokenize


def texts(s: str) -> list[str]:
    return [t.text for t in tokenize(s)]


# --- basics ---


def test_lowercases_strips_punctuation_and_collapses_whitespace():
    assert normalize("  Hello,   World!\n\tIt's\u00a0fine.  ") == "hello world its fine"


def test_empty_and_punctuation_only():
    assert tokenize("") == []
    assert tokenize(" ... !? \n") == []


def test_offsets_point_into_original():
    original = "  Hello, World!"
    assert tokenize(original) == [Token("hello", 2, 7), Token("world", 9, 14)]


def test_hyphen_and_slash_split_words():
    assert texts("well-known and/or") == ["well", "known", "and", "or"]


def test_apostrophes_join_but_are_not_in_the_span():
    original = "'don’t'"
    assert tokenize(original) == [Token("dont", 1, 6)]


def test_non_latin_scripts_are_kept():
    assert texts("Ünïcödé café naïve") == ["ünïcödé", "café", "naïve"]
    assert texts("नमस्ते दुनिया") == ["नमस्ते", "दुनिया"]


# --- NFKC ---


def test_nfkc_folds_fullwidth_ligatures_and_math_letters():
    assert texts("Ｐｌａｇｉａｒｉｓｍ") == ["plagiarism"]
    assert texts("ﬁnal") == ["final"]
    assert texts("𝐛𝐨𝐥𝐝") == ["bold"]


def test_decomposed_accents_compose_and_span_the_whole_cluster():
    original = "cafe\u0301 x"
    assert tokenize(original) == [Token("café", 0, 5), Token("x", 6, 7)]


def test_expanding_char_maps_back_to_its_single_original_char():
    original = "the ﬁ"
    [_, fi] = tokenize(original)
    assert fi == Token("fi", 4, 5)
    assert original[fi.start : fi.end] == "ﬁ"


# --- evasion ---


def test_zero_width_chars_do_not_split_words():
    for zw in ["\u200b", "\u200c", "\u200d", "\ufeff"]:
        original = f"pla{zw}gia{zw}rism"
        assert tokenize(original) == [Token("plagiarism", 0, len(original))]


def test_leading_and_trailing_zero_width_chars_are_outside_the_span():
    assert tokenize("\u200bword\ufeff") == [Token("word", 1, 5)]


def test_cyrillic_homoglyphs_fold_to_latin():
    # а, е, о, р, с are Cyrillic.
    assert normalize("Тhе рrоblеm оf ассеss") == normalize("The problem of access")


def test_greek_homoglyphs_fold_to_latin():
    # Α, ο, κ, ε, ι are Greek.
    assert normalize("Αnother bοοκ εxample") == "another book example"


def test_uppercase_homoglyphs_fold_like_lowercase():
    # Н and Т are Cyrillic capitals; their lowercase forms fold the same way.
    assert normalize("НЕLLО ТНЕRЕ") == normalize("hello there")


def test_combined_evasion_matches_plain_text():
    sneaky = "Ｔhе\u200b qu\u200dісk ｂrоwn fох"
    assert normalize(sneaky) == "the quick brown fox"


# --- properties ---

INTERESTING = (
    "aZ09 \t\n.,'’-\u00a0\u200b\u200c\u200d\ufeff\u00ad\u2060"
    "аеорсНТαοκΣσς"  # homoglyphs, Greek final sigma
    "\u0301\u0308\u0316é½ﬁＡ𝐛İß"  # combining marks, NFKC expansions, case oddities
    "\u1100\u1161\u11a8가"  # Hangul jamo that compose
    "न\u093f\u094dक"  # Devanagari with vowel sign and virama
)
text_strategy = st.text(
    alphabet=st.one_of(st.sampled_from(INTERESTING), st.characters(codec="utf-8"))
)


@given(text_strategy)
def test_every_token_span_normalizes_back_to_the_token(original: str):
    for t in tokenize(original):
        assert normalize(original[t.start : t.end]) == t.text


@given(text_strategy)
def test_tokens_are_ordered_non_overlapping_and_non_empty(original: str):
    prev_end = 0
    for t in tokenize(original):
        assert t.text
        assert " " not in t.text
        assert prev_end <= t.start < t.end <= len(original)
        prev_end = t.end
