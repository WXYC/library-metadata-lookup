"""Unit tests for scripts/api_keys (seed/revoke/list CLI).

Mirrors scripts/seed_library_release_overrides.py's test shape: PG is mocked
end-to-end here (via a patched ``_require_pg``); there is no ``-m pg`` layer
for this script because it has no SQL of its own -- it only calls the
already-integration-tested ``entity/api_keys.py`` functions.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from scripts.api_keys.__main__ import (
    _build_arg_parser,
    list_keys,
    main,
    revoke,
    seed,
)

_PLAINTEXT = "lml_do-not-use-this-exact-token"


@pytest.mark.asyncio
class TestSeed:
    async def test_mints_and_inserts_a_hashed_row(self):
        pg = AsyncMock()
        with (
            patch("scripts.api_keys.__main__._require_pg", return_value=pg),
            patch("scripts.api_keys.__main__.set_up_api_keys_schema", AsyncMock()) as bootstrap,
            patch("scripts.api_keys.__main__.generate_token", return_value=_PLAINTEXT),
            patch(
                "scripts.api_keys.__main__.insert_api_key",
                AsyncMock(return_value={"id": 4, "created_at": "2026-07-23T00:00:00Z"}),
            ) as insert,
        ):
            await seed(caller_name="wxyc-canary", note="initial seed")

        bootstrap.assert_awaited_once_with(pg)
        insert.assert_awaited_once()
        kwargs = insert.await_args.kwargs
        assert kwargs["caller_name"] == "wxyc-canary"
        assert kwargs["note"] == "initial seed"
        # Only the hash is ever passed to the entity layer -- never the plaintext.
        assert kwargs["key_hash"] != _PLAINTEXT
        pg.close.assert_awaited_once()

    async def test_prints_the_plaintext_exactly_once(self, capsys):
        pg = AsyncMock()
        with (
            patch("scripts.api_keys.__main__._require_pg", return_value=pg),
            patch("scripts.api_keys.__main__.set_up_api_keys_schema", AsyncMock()),
            patch("scripts.api_keys.__main__.generate_token", return_value=_PLAINTEXT),
            patch(
                "scripts.api_keys.__main__.insert_api_key",
                AsyncMock(return_value={"id": 4, "created_at": "2026-07-23T00:00:00Z"}),
            ),
        ):
            await seed(caller_name="wxyc-canary", note=None)

        out = capsys.readouterr().out
        assert out.count(_PLAINTEXT) == 1

    async def test_never_logs_the_plaintext(self, caplog):
        pg = AsyncMock()
        with caplog.at_level(logging.DEBUG):
            with (
                patch("scripts.api_keys.__main__._require_pg", return_value=pg),
                patch("scripts.api_keys.__main__.set_up_api_keys_schema", AsyncMock()),
                patch("scripts.api_keys.__main__.generate_token", return_value=_PLAINTEXT),
                patch(
                    "scripts.api_keys.__main__.insert_api_key",
                    AsyncMock(return_value={"id": 4, "created_at": "2026-07-23T00:00:00Z"}),
                ),
            ):
                await seed(caller_name="wxyc-canary", note=None)

        for record in caplog.records:
            assert _PLAINTEXT not in record.getMessage()

    async def test_closes_pool_even_on_insert_failure(self):
        pg = AsyncMock()
        with (
            patch("scripts.api_keys.__main__._require_pg", return_value=pg),
            patch("scripts.api_keys.__main__.set_up_api_keys_schema", AsyncMock()),
            patch("scripts.api_keys.__main__.generate_token", return_value=_PLAINTEXT),
            patch(
                "scripts.api_keys.__main__.insert_api_key",
                AsyncMock(side_effect=RuntimeError("pg blip")),
            ),
        ):
            with pytest.raises(RuntimeError):
                await seed(caller_name="wxyc-canary", note=None)

        pg.close.assert_awaited_once()


@pytest.mark.asyncio
class TestRevoke:
    async def test_revokes_and_reports_caller(self, caplog):
        pg = AsyncMock()
        with caplog.at_level(logging.INFO):
            with (
                patch("scripts.api_keys.__main__._require_pg", return_value=pg),
                patch("scripts.api_keys.__main__.set_up_api_keys_schema", AsyncMock()),
                patch(
                    "scripts.api_keys.__main__.revoke_api_key",
                    AsyncMock(return_value="wxyc-canary"),
                ) as revoke_fn,
            ):
                await revoke(key_id=4)

        revoke_fn.assert_awaited_once_with(pg, key_id=4)
        pg.close.assert_awaited_once()
        assert any("wxyc-canary" in r.getMessage() for r in caplog.records)

    async def test_already_revoked_or_missing_logs_a_warning_not_an_error(self, caplog):
        pg = AsyncMock()
        with caplog.at_level(logging.WARNING):
            with (
                patch("scripts.api_keys.__main__._require_pg", return_value=pg),
                patch("scripts.api_keys.__main__.set_up_api_keys_schema", AsyncMock()),
                patch(
                    "scripts.api_keys.__main__.revoke_api_key",
                    AsyncMock(return_value=None),
                ),
            ):
                await revoke(key_id=999)  # must not raise

        assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
class TestListKeys:
    async def test_prints_rows_without_key_hash(self, capsys):
        pg = AsyncMock()
        rows = [
            {
                "id": 1,
                "caller_name": "wxyc-canary",
                "created_at": "2026-07-01T00:00:00Z",
                "last_used_at": None,
                "revoked_at": None,
            },
            {
                "id": 2,
                "caller_name": "backend-service",
                "created_at": "2026-07-10T00:00:00Z",
                "last_used_at": "2026-07-20T00:00:00Z",
                "revoked_at": "2026-07-21T00:00:00Z",
            },
        ]
        with (
            patch("scripts.api_keys.__main__._require_pg", return_value=pg),
            patch("scripts.api_keys.__main__.set_up_api_keys_schema", AsyncMock()),
            patch("scripts.api_keys.__main__.list_api_keys", AsyncMock(return_value=rows)),
        ):
            await list_keys()

        out = capsys.readouterr().out
        assert "wxyc-canary" in out
        assert "backend-service" in out
        assert "key_hash" not in out.lower()
        pg.close.assert_awaited_once()

    async def test_prints_a_message_when_no_keys_exist(self, capsys):
        pg = AsyncMock()
        with (
            patch("scripts.api_keys.__main__._require_pg", return_value=pg),
            patch("scripts.api_keys.__main__.set_up_api_keys_schema", AsyncMock()),
            patch("scripts.api_keys.__main__.list_api_keys", AsyncMock(return_value=[])),
        ):
            await list_keys()

        out = capsys.readouterr().out
        assert "no keys" in out.lower()


class TestArgParser:
    def test_seed_requires_caller(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["seed", "--caller", "wxyc-canary", "--note", "x"])
        assert args.command == "seed"
        assert args.caller == "wxyc-canary"
        assert args.note == "x"

    def test_revoke_requires_id(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["revoke", "--id", "4"])
        assert args.command == "revoke"
        assert args.key_id == 4

    def test_list_takes_no_extra_args(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["list"])
        assert args.command == "list"

    def test_no_command_is_an_error(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


class TestMainDispatch:
    def test_main_seed_invokes_seed(self):
        with patch("scripts.api_keys.__main__.seed", AsyncMock()) as seed_fn:
            main(["seed", "--caller", "wxyc-canary"])
        seed_fn.assert_awaited_once_with(caller_name="wxyc-canary", note=None)

    def test_main_revoke_invokes_revoke(self):
        with patch("scripts.api_keys.__main__.revoke", AsyncMock()) as revoke_fn:
            main(["revoke", "--id", "7"])
        revoke_fn.assert_awaited_once_with(key_id=7)

    def test_main_list_invokes_list_keys(self):
        with patch("scripts.api_keys.__main__.list_keys", AsyncMock()) as list_fn:
            main(["list"])
        list_fn.assert_awaited_once_with()
