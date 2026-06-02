import pytest

from core.text import strip_leading_article


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("the black dog", "black dog"),
        ("a microphones", "microphones"),
        ("therapy?", "therapy?"),
        ("thee silver mt. zion", "thee silver mt. zion"),
        ("the", ""),
    ],
)
def test_strip_leading_article(name: str, expected: str):
    assert strip_leading_article(name) == expected
