"""Unit tests for the polymorphic /lookup streaming-URL post-process (LML#573).

``apply_streaming_url_postprocess`` generalizes the Apple-only
``apply_apple_music_postprocess`` (LML#571) across every configured service.
It runs inside the orchestrator's per-item enrichment: for each service in
``STREAMING_URL_CACHE_CONFIG`` whose per-service flag is on, whose URL field
in the ``update`` dict came out null, and whose client is present, it runs the
cache-backed resolver with the REQUEST's ``(artist, album)``, mutates the
``update`` dict in place, and mints the parsed external_id on
``live_resolved`` outcomes only.

Concurrency model (preempts #594): each service's resolver is wrapped in its
own ``asyncio.wait_for(cfg.probe_timeout_s)`` inside one
``gather(return_exceptions=True)`` — one service timing out does NOT cancel
the others, and a timeout surfaces as the ``live_error`` outcome with no URL
and no cache write.

Sentry: ``streaming_url.persistent_lookup.fired.<service>`` plus
``streaming_url.persistent_lookup.<outcome>.<service>`` via
``scope.transaction.set_data`` — no parallel emission of the old
``apple_music.persistent_lookup.*`` shape.

PG, the streaming clients, the entity store, and the cache resolver are
mocked. Integration coverage is in
``tests/integration/test_streaming_url_persistent_lookup.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from entity.sources import PgSource
from entity.store import EntityStore
from entity.streaming_url_cache import ResolveOutcome, ResolveSource
from identity.release_validation import (
    RELEASE_SOURCE_CONFIG,
    InvalidReleaseExternalIdError,
)
from lookup import streaming_url_postprocess as mod
from lookup.streaming_url_postprocess import (
    STREAMING_URL_CACHE_CONFIG,
    apply_streaming_url_postprocess,
)

_RESOLVE_SOURCE_VALUES: set[str] = set(get_args(ResolveSource))

# Per-service test data. The cache module is service-agnostic; what differs
# per service is the URL field, client attr, per-service flag, and a
# resolved-URL/external-id pair that the registry's url_to_external_id
# extractor + validator round-trip cleanly.
_CASES: dict[str, dict] = {
    "apple_music_album": {
        "url_field": "apple_music_url",
        "client_attr": "apple_music",
        "flag_setting": "lml_persist_streaming_url_apple_music",
        "resolved_url": "https://music.apple.com/us/album/foo/1234567890",
        "external_id": "1234567890",
        # Matches the parser's \d{6,} floor but fails the validator's
        # leading-zero rule — exercises the validate-before-mint guard.
        "unmintable_but_parseable_url": "https://music.apple.com/us/album/foo/000000",
    },
    "spotify_album": {
        "url_field": "spotify_url",
        "client_attr": "spotify",
        "flag_setting": "lml_persist_streaming_url_spotify",
        "resolved_url": "https://open.spotify.com/album/1A2GTWGt0LBTGQAyA3OKAf",
        "external_id": "1A2GTWGt0LBTGQAyA3OKAf",
        # No numeric/22-char ID to parse — surfaces URL, skips mint.
        "unmintable_but_parseable_url": "https://open.spotify.com/album/short",
    },
}

_FLAG_BY_SERVICE = {service: case["flag_setting"] for service, case in _CASES.items()}
_URL_FIELDS = [case["url_field"] for case in _CASES.values()]


def _settings(*enabled_services: str, master: bool = True) -> SimpleNamespace:
    """A Settings-like stub with the AND-gate flags the post-process reads."""
    flags = {
        "lml_persist_streaming_urls": master,
        "lml_persist_streaming_url_apple_music": False,
        "lml_persist_streaming_url_spotify": False,
    }
    for service in enabled_services:
        flags[_FLAG_BY_SERVICE[service]] = True
    return SimpleNamespace(**flags)


def _blank_update() -> dict:
    """An update dict with every service's URL field present and null."""
    return dict.fromkeys(_URL_FIELDS)


def _clients(**overrides) -> dict:
    """A clients dict mapping each service's client_attr to an AsyncMock.

    Pass ``apple_music=None`` (etc.) to simulate an unconfigured client — the
    post-process must skip that service, not error.
    """
    clients = {case["client_attr"]: AsyncMock() for case in _CASES.values()}
    clients.update(overrides)
    return clients


def _entity_store() -> MagicMock:
    return MagicMock(spec=EntityStore)


def _sentry_scope():
    transaction = Mock()
    transaction.set_data = Mock()
    scope = Mock()
    scope.transaction = transaction
    return scope, transaction


@pytest.mark.asyncio
class TestSkipConditions:
    async def test_master_flag_off_short_circuits_all_services(self):
        update = _blank_update()
        with patch.object(mod, "resolve_streaming_url_with_cache") as resolve:
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=_entity_store(),
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                # master off, both per-service ON — master must still win.
                settings=_settings("apple_music_album", "spotify_album", master=False),
            )
        resolve.assert_not_called()
        assert update == _blank_update()

    async def test_master_on_but_all_per_service_off_does_nothing(self):
        # The subtle AND-gate: master defaulting True alone enables nothing.
        update = _blank_update()
        with patch.object(mod, "resolve_streaming_url_with_cache") as resolve:
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=_entity_store(),
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(master=True),  # no per-service enabled
            )
        resolve.assert_not_called()

    @pytest.mark.parametrize("missing", ["pg", "entity_store", "artist", "album"])
    async def test_skips_when_precondition_missing(self, missing):
        update = _blank_update()
        kwargs = {
            "clients": _clients(),
            "pg": AsyncMock(spec=PgSource),
            "entity_store": _entity_store(),
            "request_artist": "Hyd",
            "request_album": "Hold Onto Me Infinity",
            "settings": _settings("apple_music_album", "spotify_album"),
        }
        if missing == "pg":
            kwargs["pg"] = None
        elif missing == "entity_store":
            kwargs["entity_store"] = None
        elif missing == "artist":
            kwargs["request_artist"] = ""
        elif missing == "album":
            kwargs["request_album"] = None
        with patch.object(mod, "resolve_streaming_url_with_cache") as resolve:
            await apply_streaming_url_postprocess(update, **kwargs)
        resolve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(_CASES))
class TestPerServiceGating:
    async def test_skips_service_when_url_already_set(self, service):
        case = _CASES[service]
        update = _blank_update()
        update[case["url_field"]] = "https://existing.test/x"
        with patch.object(mod, "resolve_streaming_url_with_cache") as resolve:
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=_entity_store(),
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )
        resolve.assert_not_called()
        assert update[case["url_field"]] == "https://existing.test/x"

    async def test_empty_string_sentinel_is_respected(self, service):
        # "" means "explicitly checked, nothing to surface" — must NOT trigger
        # a fresh probe (``is not None``, not truthiness).
        case = _CASES[service]
        update = _blank_update()
        update[case["url_field"]] = ""
        with patch.object(mod, "resolve_streaming_url_with_cache") as resolve:
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=_entity_store(),
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )
        resolve.assert_not_called()
        assert update[case["url_field"]] == ""

    async def test_skips_service_when_client_is_none(self, service):
        # clients dict carries None for the unconfigured service — skip it,
        # don't error.
        case = _CASES[service]
        update = _blank_update()
        with patch.object(mod, "resolve_streaming_url_with_cache") as resolve:
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(**{case["client_attr"]: None}),
                pg=AsyncMock(spec=PgSource),
                entity_store=_entity_store(),
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )
        resolve.assert_not_called()
        assert update[case["url_field"]] is None

    async def test_per_service_flag_off_skips_only_that_service(self, service):
        # Enable the OTHER service only; the parametrized service stays off.
        case = _CASES[service]
        other = next(s for s in _CASES if s != service)
        update = _blank_update()
        calls: list[str] = []

        async def fake_resolve(pg, client, *, service, artist, album, **kwargs):
            calls.append(service)
            return ResolveOutcome(url=_CASES[service]["resolved_url"], source="live_resolved")

        with patch.object(
            mod, "resolve_streaming_url_with_cache", new=AsyncMock(side_effect=fake_resolve)
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=_entity_store(),
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(other),
            )
        assert calls == [other]
        assert update[case["url_field"]] is None
        assert update[_CASES[other]["url_field"]] == _CASES[other]["resolved_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(_CASES))
class TestUrlUpdateAndMint:
    async def test_live_resolved_writes_url_and_mints(self, service):
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))

        with patch.object(
            mod,
            "resolve_streaming_url_with_cache",
            new=AsyncMock(
                return_value=ResolveOutcome(url=case["resolved_url"], source="live_resolved")
            ),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )

        assert update[case["url_field"]] == case["resolved_url"]
        entity_store.mint_or_get_release_identity.assert_awaited_once_with(
            source=service, external_id=case["external_id"]
        )

    async def test_cache_hit_writes_url_without_minting(self, service):
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, False))

        with patch.object(
            mod,
            "resolve_streaming_url_with_cache",
            new=AsyncMock(
                return_value=ResolveOutcome(url=case["resolved_url"], source="cache_hit")
            ),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )

        assert update[case["url_field"]] == case["resolved_url"]
        entity_store.mint_or_get_release_identity.assert_not_called()

    @pytest.mark.parametrize("source", ["cache_miss_recent", "live_miss", "live_error"])
    async def test_no_url_outcomes_leave_field_null_and_do_not_mint(self, service, source):
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))

        with patch.object(
            mod,
            "resolve_streaming_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=None, source=source)),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )

        assert update[case["url_field"]] is None
        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_unparseable_url_surfaces_but_skips_mint(self, service):
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))
        url = case["unmintable_but_parseable_url"]

        with patch.object(
            mod,
            "resolve_streaming_url_with_cache",
            new=AsyncMock(return_value=ResolveOutcome(url=url, source="live_resolved")),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )

        assert update[case["url_field"]] == url
        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_mint_validation_rejection_swallowed_url_surfaces(self, service):
        # Best-effort mint: a validation rejection must not roll back the URL.
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))

        with (
            patch.object(
                mod,
                "resolve_streaming_url_with_cache",
                new=AsyncMock(
                    return_value=ResolveOutcome(url=case["resolved_url"], source="live_resolved")
                ),
            ),
            patch.object(
                mod,
                "validate_and_canonicalize_external_id",
                side_effect=InvalidReleaseExternalIdError("nope"),
            ),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )

        assert update[case["url_field"]] == case["resolved_url"]
        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_mint_pg_error_swallowed_url_surfaces(self, service):
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(side_effect=RuntimeError("PG down"))

        with patch.object(
            mod,
            "resolve_streaming_url_with_cache",
            new=AsyncMock(
                return_value=ResolveOutcome(url=case["resolved_url"], source="live_resolved")
            ),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(service),
            )

        entity_store.mint_or_get_release_identity.assert_awaited_once()
        assert update[case["url_field"]] == case["resolved_url"]


@pytest.mark.asyncio
class TestConcurrencyAndTimeout:
    async def test_one_service_timeout_does_not_cancel_the_other(self):
        # Both services active. The slow one exceeds its per-service
        # probe_timeout_s and surfaces as live_error (no URL); the healthy one
        # still resolves and updates its URL field.
        slow = "apple_music_album"
        fast = "spotify_album"
        update = _blank_update()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))
        scope, transaction = _sentry_scope()

        async def fake_resolve(pg, client, *, service, artist, album, **kwargs):
            if service == slow:
                await asyncio.sleep(1.0)  # cancelled by wait_for before this returns
                return ResolveOutcome(url="unreachable", source="live_resolved")
            return ResolveOutcome(url=_CASES[fast]["resolved_url"], source="live_resolved")

        # Shrink the per-service timeouts so the slow leg trips fast. Also
        # strip timeout_env_var: Apple's entry honors LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS
        # via _effective_probe_timeout_s, so an ambient env var would otherwise
        # override the 0.05 ceiling and make this test environment-coupled.
        fast_config = {
            k: dataclasses.replace(v, probe_timeout_s=0.05, timeout_env_var=None)
            for k, v in STREAMING_URL_CACHE_CONFIG.items()
        }
        with (
            patch.object(mod, "STREAMING_URL_CACHE_CONFIG", fast_config),
            patch.object(
                mod, "resolve_streaming_url_with_cache", new=AsyncMock(side_effect=fake_resolve)
            ),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings(slow, fast),
            )

        # Fast service surfaced; slow service timed out → no URL.
        assert update[_CASES[fast]["url_field"]] == _CASES[fast]["resolved_url"]
        assert update[_CASES[slow]["url_field"]] is None
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        # The timed-out service projects live_error; the healthy one live_resolved.
        assert calls.get(f"streaming_url.persistent_lookup.live_error.{slow}") is True
        assert calls.get(f"streaming_url.persistent_lookup.live_resolved.{fast}") is True


def _patch_resolver_returning(source: ResolveSource, url: str | None):
    return patch.object(
        mod,
        "resolve_streaming_url_with_cache",
        new=AsyncMock(return_value=ResolveOutcome(url=url, source=source)),
    )


_SENTRY_CASES = [
    ("cache_hit", _CASES["apple_music_album"]["resolved_url"]),
    ("cache_miss_recent", None),
    ("live_resolved", _CASES["apple_music_album"]["resolved_url"]),
    ("live_miss", None),
    ("live_error", None),
]


def test_sentry_cases_cover_every_resolve_source():
    parametrized = {source for source, _ in _SENTRY_CASES}
    assert parametrized == _RESOLVE_SOURCE_VALUES, (
        f"Sentry parametrize list drifted from ResolveSource: "
        f"missing {_RESOLVE_SOURCE_VALUES - parametrized}; extra {parametrized - _RESOLVE_SOURCE_VALUES}"
    )


@pytest.mark.asyncio
class TestSentryProjection:
    @pytest.mark.parametrize(("source", "resolved_url"), _SENTRY_CASES)
    async def test_fired_and_outcome_keys_set_for_apple(self, source, resolved_url):
        update = _blank_update()
        scope, transaction = _sentry_scope()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))

        with (
            _patch_resolver_returning(source, resolved_url),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=entity_store,
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings("apple_music_album"),
            )

        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get("streaming_url.persistent_lookup.fired.apple_music_album") is True
        assert calls.get(f"streaming_url.persistent_lookup.{source}.apple_music_album") is True
        # No old-namespace emission.
        assert not any(k.startswith("apple_music.persistent_lookup") for k in calls)
        # Only the observed outcome key for this service is set.
        for other in _RESOLVE_SOURCE_VALUES - {source}:
            assert f"streaming_url.persistent_lookup.{other}.apple_music_album" not in calls

    async def test_no_active_transaction_does_not_crash(self):
        update = _blank_update()
        scope = Mock()
        scope.transaction = None
        with (
            _patch_resolver_returning("live_resolved", _CASES["apple_music_album"]["resolved_url"]),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=_entity_store(),
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings("apple_music_album"),
            )
        assert update["apple_music_url"] == _CASES["apple_music_album"]["resolved_url"]

    async def test_set_data_failure_does_not_break_request(self):
        update = _blank_update()
        transaction = Mock()
        transaction.set_data = Mock(side_effect=RuntimeError("boom"))
        scope = Mock()
        scope.transaction = transaction
        with (
            _patch_resolver_returning("live_resolved", _CASES["apple_music_album"]["resolved_url"]),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=AsyncMock(spec=PgSource),
                entity_store=_entity_store(),
                request_artist="Hyd",
                request_album="Hold Onto Me Infinity",
                settings=_settings("apple_music_album"),
            )
        assert update["apple_music_url"] == _CASES["apple_music_album"]["resolved_url"]


class TestRegistryInvariants:
    """The cache registry is the 2-entry subset; guards keep it from drifting."""

    def test_parametrize_keys_match_cache_config(self):
        assert set(_CASES) == set(STREAMING_URL_CACHE_CONFIG.keys())

    def test_cache_config_is_subset_of_release_source_config(self):
        # PR-1 parity invariant: every cached service must be identity-mintable.
        assert set(STREAMING_URL_CACHE_CONFIG.keys()) <= set(RELEASE_SOURCE_CONFIG.keys())

    def test_cache_config_matches_table_check_constraint_allowlist(self):
        # The cache table's named CHECK constraint pins the allowed ``service``
        # values from ``_PR1_SERVICES``. A registry service NOT in that
        # allowlist would fail every UPSERT at runtime; an allowlist value with
        # no registry entry would be dead. Lock them together.
        from entity.streaming_url_cache import _PR1_SERVICES

        assert set(STREAMING_URL_CACHE_CONFIG.keys()) == set(_PR1_SERVICES)

    def test_cache_config_ships_exactly_apple_and_spotify(self):
        assert set(STREAMING_URL_CACHE_CONFIG.keys()) == {"apple_music_album", "spotify_album"}

    @pytest.mark.parametrize("service", list(_CASES))
    def test_cache_config_fields_match_expectations(self, service):
        case = _CASES[service]
        cfg = STREAMING_URL_CACHE_CONFIG[service]
        assert cfg.url_field == case["url_field"]
        assert cfg.client_attr == case["client_attr"]
        assert cfg.flag_setting == case["flag_setting"]
        # mint_source keys back into RELEASE_SOURCE_CONFIG (same as the service key).
        assert cfg.mint_source == service
        assert cfg.mint_source in RELEASE_SOURCE_CONFIG
        assert cfg.probe_timeout_s == 4.0

    def test_every_flag_setting_is_a_real_settings_attribute(self):
        # `apply_streaming_url_postprocess` reads `getattr(settings,
        # cfg.flag_setting, False)` — a typo'd flag_setting would silently
        # default to False and disable the service with no error. Assert each
        # names a real Settings field so a typo fails CI, not prod.
        from config.settings import Settings

        settings = Settings()
        for service, cfg in STREAMING_URL_CACHE_CONFIG.items():
            assert hasattr(settings, cfg.flag_setting), (
                f"{service}: flag_setting {cfg.flag_setting!r} is not a Settings attribute"
            )


class TestPerServiceTimeoutResolution:
    """LML#573 preserves the `LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS` back-compat
    knob for the Apple post-process leg (the old apply_apple_music_postprocess
    honored it). The effective per-service ceiling is env-overridable only for
    services whose registry entry carries a `timeout_env_var`."""

    def test_apple_entry_carries_back_compat_env_var(self):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        assert (
            STREAMING_URL_CACHE_CONFIG["apple_music_album"].timeout_env_var
            == APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR
        )

    def test_spotify_entry_has_no_env_var(self):
        # New service — no back-compat env knob; static per-service default.
        assert STREAMING_URL_CACHE_CONFIG["spotify_album"].timeout_env_var is None

    def test_apple_effective_timeout_honors_env_override(self, monkeypatch):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        monkeypatch.setenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, "1500")
        assert (
            mod._effective_probe_timeout_s(STREAMING_URL_CACHE_CONFIG["apple_music_album"]) == 1.5
        )

    def test_apple_effective_timeout_defaults_without_env(self, monkeypatch):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        monkeypatch.delenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, raising=False)
        assert (
            mod._effective_probe_timeout_s(STREAMING_URL_CACHE_CONFIG["apple_music_album"]) == 4.0
        )

    def test_spotify_effective_timeout_ignores_apple_env(self, monkeypatch):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        # Apple's env must not bleed into a service without a timeout_env_var.
        monkeypatch.setenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, "1500")
        assert mod._effective_probe_timeout_s(STREAMING_URL_CACHE_CONFIG["spotify_album"]) == 4.0
