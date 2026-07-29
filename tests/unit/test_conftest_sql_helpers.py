"""Unit tests for the shared SQL test helpers in ``tests/unit/conftest.py`` (LML#887).

Pins the behavior of ``strip_sql_comments``, ``extract_create_table``, and
``normalize_sql`` now that the schema-parity test files import a single shared
copy instead of each carrying its own. ``normalize_sql`` is a distinct,
stricter helper (drops ``;`` and collapses whitespace) kept separate from
``strip_sql_comments`` per the exact-equality parity test in
``test_streaming_catalog_schema.py``.
"""

from __future__ import annotations

from tests.unit.conftest import extract_create_table, normalize_sql, strip_sql_comments


class TestStripSqlComments:
    def test_drops_line_comments(self):
        sql = "CREATE TABLE foo (\n    id INT -- primary key\n);"
        assert "--" not in strip_sql_comments(sql)
        assert "primary key" not in strip_sql_comments(sql)

    def test_collapses_to_single_line(self):
        sql = "CREATE TABLE foo (\n    id INT,\n    name TEXT\n);"
        result = strip_sql_comments(sql)
        assert "\n" not in result
        assert result == "CREATE TABLE foo ( id INT, name TEXT );"

    def test_drops_blank_lines(self):
        sql = "CREATE TABLE foo (\n\n    id INT\n);"
        assert strip_sql_comments(sql) == "CREATE TABLE foo ( id INT );"


class TestExtractCreateTable:
    def test_pulls_create_table_statement(self):
        ddl = "-- header comment\nCREATE TABLE foo (id INT);\nCREATE TABLE bar (id INT);"
        assert extract_create_table(ddl) == "CREATE TABLE foo (id INT)"

    def test_strips_comments_within_extracted_statement(self):
        ddl = "CREATE TABLE foo (\n    id INT -- comment\n);"
        assert extract_create_table(ddl) == "CREATE TABLE foo ( id INT );".rstrip(";").strip()


class TestNormalizeSql:
    def test_drops_line_comments(self):
        assert "--" not in normalize_sql("SELECT 1; -- a comment")

    def test_drops_semicolons(self):
        assert ";" not in normalize_sql("CREATE TABLE foo (id INT);")

    def test_collapses_whitespace(self):
        assert normalize_sql("CREATE  TABLE\nfoo (id INT)") == "CREATE TABLE foo (id INT)"

    def test_distinct_from_strip_sql_comments(self):
        sql = "CREATE TABLE foo (id INT);"
        assert normalize_sql(sql) != strip_sql_comments(sql)
