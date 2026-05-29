"""Unit tests for entity_resolution source transports."""

from unittest.mock import AsyncMock

import pytest

from entity.sources import SparqlSource


class TestQueryBatchedTemplateSubstitution:
    """``query_batched`` must substitute ``{values}`` without choking on the
    literal ``{`` and ``}`` that appear in every SPARQL block."""

    @pytest.mark.asyncio
    async def test_template_with_literal_braces_is_substituted(self, monkeypatch):
        """A raw SPARQL template (literal braces, single ``{values}`` placeholder)
        must reach the underlying ``query()`` call with ``{values}`` replaced
        and the literal SPARQL braces preserved.

        Regression: WXYC/library-metadata-lookup#182. ``str.format`` raises
        ``ValueError: unexpected '{' in field name`` on the first literal brace
        after the placeholder.
        """
        sparql = SparqlSource(batch_size=10)
        captured: list[str] = []

        async def fake_query(query_str: str):
            captured.append(query_str)
            return []

        monkeypatch.setattr(sparql, "query", fake_query)

        template = (
            "SELECT ?item ?discogsId WHERE {\n"
            "  VALUES ?discogsId { {values} }\n"
            "  ?item wdt:P1953 ?discogsId .\n"
            "}"
        )
        await sparql.query_batched(template, ['"12"', '"34"'])

        assert len(captured) == 1
        rendered = captured[0]
        assert '"12" "34"' in rendered
        assert "{values}" not in rendered
        # Literal SPARQL braces survive untouched.
        assert "WHERE {" in rendered
        assert "VALUES ?discogsId {" in rendered

    @pytest.mark.asyncio
    async def test_batches_split_by_batch_size(self, monkeypatch):
        """Items longer than ``batch_size`` produce one ``query`` call per batch,
        each carrying its own substituted VALUES list."""
        sparql = SparqlSource(batch_size=2)
        captured: list[str] = []

        async def fake_query(query_str: str):
            captured.append(query_str)
            return []

        monkeypatch.setattr(sparql, "query", fake_query)

        template = "SELECT ?x WHERE { VALUES ?x { {values} } }"
        await sparql.query_batched(template, ['"1"', '"2"', '"3"', '"4"', '"5"'])

        assert len(captured) == 3
        assert '"1" "2"' in captured[0]
        assert '"3" "4"' in captured[1]
        assert '"5"' in captured[2]

    @pytest.mark.asyncio
    async def test_combines_bindings_from_all_batches(self, monkeypatch):
        """``query_batched`` returns the concatenated bindings from every batch."""
        sparql = SparqlSource(batch_size=2)
        call_count = {"n": 0}

        async def fake_query(query_str: str):
            call_count["n"] += 1
            return [{"row": call_count["n"]}]

        monkeypatch.setattr(sparql, "query", fake_query)

        template = "SELECT ?x WHERE { VALUES ?x { {values} } }"
        bindings = await sparql.query_batched(template, ["a", "b", "c"])

        assert bindings == [{"row": 1}, {"row": 2}]

    @pytest.mark.asyncio
    async def test_empty_items_makes_no_queries(self, monkeypatch):
        """Empty ``items`` list short-circuits without calling ``query``."""
        sparql = SparqlSource()
        query_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(sparql, "query", query_mock)

        bindings = await sparql.query_batched("template", [])

        assert bindings == []
        query_mock.assert_not_called()
