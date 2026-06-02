"""Shared text utilities."""

import re

LEADING_ARTICLES = frozenset({"the", "a", "an"})
"""English articles stripped from normalized artist names."""

LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)(\s+|$)")


def strip_leading_article(name: str) -> str:
    """Strip a leading article from a lowercase-normalized name."""
    return LEADING_ARTICLE_RE.sub("", name, count=1)
