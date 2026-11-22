"""Lexical similarity between short texts (no embedding model is used)."""

import re

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the "
    "this to was were which with must should will can each all into not".split()
)


def shingles(text: str, size: int = 2) -> set[tuple[str, ...]]:
    """Word n-grams of the text with stop words removed."""
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of word bigrams, in [0, 1]."""
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def overlap(a: str, b: str) -> tuple[float, int]:
    """How much of the shorter text's bigrams appear in the other.

    Returns (share of the smaller bigram set that is shared, number shared).
    Unlike Jaccard this stays high when a short sentence restates part of a
    long one, which is what a give-away looks like.
    """
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0, 0
    shared = len(sa & sb)
    return shared / min(len(sa), len(sb)), shared


def max_similarity(text: str, others: list[str]) -> float:
    return max((similarity(text, other) for other in others), default=0.0)
