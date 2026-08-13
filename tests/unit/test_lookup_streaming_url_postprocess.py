"""Unit tests for the polymorphic /lookup streaming-URL post-process.

``apply_streaming_url_postprocess`` generalizes the Apple-only
``apply_apple_music_postprocess`` (LML#571) across every configured service.
It runs inside ``lookup/enrichment``'s per-item enrichment: for each service in
``STREAMING_URL_CACHE_CONFIG`` whose per-service flag is on, whose URL field in
the ``update`` dict came out null, and whose client is present.

**Hot path vs. background warm (LML#706).** The response path is
**cache-read-only**: it calls ``peek_cached_streaming_url`` (a pure SELECT) per
service and makes the same four-way choice the resolver makes, without probing:

* cache **hit** → fill ``update[cfg.url_field]`` synchronously, no mint (cache
  hits already minted);
* known **recent GENUINE miss** (no url, fresh "not found" within ``miss_ttl``)
  → do nothing (a warm would just re-derive the same verdict);
* fresh **ERROR row** (LML#1115: no url, ``is_error`` true, within the shorter
  ``error_ttl``) → UNLIKE a genuine miss, this DOES enqueue a warm — re-asking
  after a transient outage is the point — via the same bounded path below;
* genuine **miss** (absent/stale) — or a fresh error row per the point above —
  → leave the field null and enqueue **one** bounded, deduplicated background
  task that runs the live probe (``resolve_streaming_url_with_cache``) → cache
  UPSERT → mint-on-``live_resolved``.

Streaming URLs are therefore *eventually consistent*: the first lookup of an
uncached album returns without that service's URL; the background warm fills the
cache so the next lookup is warm. The response path does **zero** synchronous
external HTTP — the cure for the cold-tail latency in #706. The warm is
suppressed for the whole context on the ``/lookup/bulk`` path
(``should_suppress_streaming_warm()``), which then does cache read-fill only —
uniformly for a genuine miss or a fresh error row.

Dedup is process-global, keyed ``(service, to_match_form(artist),
to_match_form(album))``. Background concurrency is bounded by
``LML_STREAMING_WARM_CONCURRENCY`` (separate from the bio warm's bound).

Sentry (hot-path only): ``streaming_url.persistent_lookup.fired.<service>`` plus
``streaming_url.persistent_lookup.<outcome>.<service>`` where ``<outcome>`` is
``cache_hit`` / ``cache_miss_recent`` / ``cache_error_recent`` /
``cache_miss_enqueued`` / ``cache_miss_unwarmed``. ``cache_error_recent``
(LML#1115) covers a fresh error row's enqueue/suppress/shed sub-cases
uniformly — the couldn't-ask signal itself is what matters. The background
task never writes to the active scope (the request scope has closed by then)
— it logs its ``live_*`` outcome.

PG, the streaming clients, the entity store, and the cache layer are mocked. The
cache-layer behavior itself is covered in
``tests/unit/test_streaming_resolve_with_cache.py`` and
``tests/unit/test_streaming_url_cache.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest

from clients.bandcamp import BandcampClient
from entity.sources import PgSource
from entity.store import EntityStore
from entity.streaming_url_cache import ResolveOutcome
from generated.api_models import StreamingResolutionStatus
from identity.release_validation import (
    RELEASE_SOURCE_CONFIG,
)
from lookup import streaming_url_postprocess as mod
from lookup import streaming_warm as warm_mod
from lookup import streaming_warm_admission
from lookup.streaming_url_postprocess import (
    STREAMING_URL_CACHE_CONFIG,
    apply_streaming_url_postprocess,
    set_suppress_streaming_warm,
)
from streaming.models import SourceMatch
from streaming.service import ALBUM_CACHE_KEYS, SERVICE_BY_ALBUM_CACHE_KEY, StreamingService
from tests.conftest import drain_streaming_warm_tasks, reset_streaming_warm_state

# Maps this file's storage-key test data (``_CASES``, string-keyed) onto the
# StreamingService enum member that owns each key (LML#1037:
# STREAMING_URL_CACHE_CONFIG is keyed by the enum, not the bare storage
# string).
_SERVICE_BY_STORAGE_KEY = SERVICE_BY_ALBUM_CACHE_KEY

# Per-service test data. The cache module is service-agnostic; what differs per
# service is the URL field, client attr, per-service flag, and a
# resolved-URL/external-id pair that the registry's url_to_external_id extractor
# + validator round-trip cleanly.
_CASES: dict[str, dict] = {
    "apple_music_album": {
        "url_field": "apple_music_url",
        "client_attr": "apple_music",
        "flag_setting": "lml_persist_streaming_url_apple_music",
        "probe_timeout_s": 4.0,
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
        "probe_timeout_s": 4.0,
        "resolved_url": "https://open.spotify.com/album/1A2GTWGt0LBTGQAyA3OKAf",
        "external_id": "1A2GTWGt0LBTGQAyA3OKAf",
        # No numeric/22-char ID to parse — surfaces URL, skips mint.
        "unmintable_but_parseable_url": "https://open.spotify.com/album/short",
    },
    "bandcamp": {
        "url_field": "bandcamp_url",
        "client_attr": "bandcamp",
        "flag_setting": "lml_persist_streaming_url_bandcamp",
        # Looser than Apple/Spotify (4.0): Bandcamp's 1 req/s rate limit makes
        # burst queue waits the dominant cost; tightened to 4.0 once the offline
        # warmer (#548) lands. See WXYC/library-metadata-lookup#573.
        "probe_timeout_s": 9.0,
        # Bandcamp's external_id IS the canonical album URL (no opaque ID), so
        # resolved_url == external_id after canonicalization.
        "resolved_url": "https://juanamolina.bandcamp.com/album/doga",
        "external_id": "https://juanamolina.bandcamp.com/album/doga",
        # A non-Bandcamp URL: the extractor returns None (parse_url rejects the
        # host) so the URL surfaces but the mint is skipped.
        "unmintable_but_parseable_url": "https://example.com/album/doga",
    },
}

_FLAG_BY_SERVICE = {service: case["flag_setting"] for service, case in _CASES.items()}
_URL_FIELDS = [case["url_field"] for case in _CASES.values()]

_ARTIST = "Hyd"
_ALBUM = "Hold Onto Me Infinity"

_BANDCAMP_AUTOCOMPLETE_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


class _InstantLimiter:
    """A rate limiter stand-in with a no-op ``acquire`` -- see the identical
    fixture in ``test_bandcamp_fail_fast_probe.py`` and
    ``test_streaming_resolve_with_cache.py``. Duplicated per this repo's
    convention for private test helpers; without it a real
    ``BandcampClient()``'s ``AsyncLimiter(1, 1)`` would pace real HTTP calls.
    """

    async def acquire(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_warm_state():
    """Isolate the module-global warm state around every test.

    Delegates to the shared ``tests.conftest.reset_streaming_warm_state`` so
    this file and the endpoint-level suite in
    ``tests/integration/test_api_lookup_hard_timeout.py`` reset the exact same
    globals — the two used to carry diverging per-file copies.
    """
    reset_streaming_warm_state()
    yield
    reset_streaming_warm_state()


def _settings(
    *enabled_services: str,
    master: bool = True,
    bulk_bandcamp_warm: bool = False,
    bulk_spotify_warm: bool = False,
) -> SimpleNamespace:
    """A Settings-like stub with the AND-gate flags the post-process reads.

    ``bulk_bandcamp_warm`` / ``bulk_spotify_warm`` back
    ``lml_bulk_bandcamp_streaming_warm`` / ``lml_bulk_spotify_streaming_warm``
    — the per-service dedicated flags that exempt a ROWLESS item from the
    /lookup/bulk warm suppression. They are deliberately independent knobs:
    neither one exempts the other's service.
    """
    flags = {
        "lml_persist_streaming_urls": master,
        "lml_persist_streaming_url_apple_music": False,
        "lml_persist_streaming_url_spotify": False,
        "lml_persist_streaming_url_bandcamp": False,
        "lml_bulk_bandcamp_streaming_warm": bulk_bandcamp_warm,
        "lml_bulk_spotify_streaming_warm": bulk_spotify_warm,
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
    store = MagicMock(spec=EntityStore)
    store.mint_or_get_release_identity = AsyncMock(return_value=(7, True))
    return store


def _sentry_scope():
    transaction = Mock()
    transaction.set_data = Mock()
    scope = Mock()
    scope.transaction = transaction
    return scope, transaction


def _patch_cache_peek(url, has_fresh_decision=None, is_error=False):
    """Patch the hot-path cache peek, returning ``(url, has_fresh_decision, is_error)``.

    ``has_fresh_decision`` defaults to ``url is not None`` — a hit always has a
    fresh row. For a url-less peek, pass ``True`` for a known-recent-miss (the
    cache holds a fresh "not found") or leave it ``None`` (→ ``False``) for a
    genuine absent/stale miss. ``is_error`` (LML#1115) marks a known-recent-miss
    as a fresh LML#1121 transport-failure row rather than a genuine confirmed
    absence — only meaningful alongside ``has_fresh_decision=True``.
    """
    fresh = has_fresh_decision if has_fresh_decision is not None else (url is not None)
    return patch.object(
        mod, "peek_cached_streaming_url", new=AsyncMock(return_value=(url, fresh, is_error))
    )


def _patch_resolver(**kwargs):
    """Patch the background-warm probe (``resolve_streaming_url_with_cache``)."""
    return patch.object(warm_mod, "resolve_streaming_url_with_cache", new=AsyncMock(**kwargs))


# Bounded drain shared with the endpoint-level suite (single source of truth
# for the subtle while-loop-because-done-callbacks-run-late semantics).
_drain_background_tasks = drain_streaming_warm_tasks


async def _run(update, *, settings, clients=None, entity_store=None, pg=None, is_rowless=False):
    """Invoke the post-process with this module's standard request shape.

    ``is_rowless`` mirrors ``enrich_one``'s ``item.id == ROWLESS_LIBRARY_ID``
    signal — the bulk-Bandcamp warm exemption only applies to rowless
    (non-library) items.
    """
    return await apply_streaming_url_postprocess(
        update,
        clients=clients if clients is not None else _clients(),
        pg=pg if pg is not None else AsyncMock(spec=PgSource),
        entity_store=entity_store if entity_store is not None else _entity_store(),
        request_artist=_ARTIST,
        request_album=_ALBUM,
        settings=settings,
        is_rowless=is_rowless,
    )


# ---------------------------------------------------------------------------
# Skip conditions: nothing reads the cache, nothing is enqueued.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSkipConditions:
    async def test_master_flag_off_short_circuits_all_services(self):
        update = _blank_update()
        with _patch_cache_peek(None) as peek, _patch_resolver() as resolve:
            await _run(
                update,
                # master off, per-service ON — master must still win.
                settings=_settings("apple_music_album", "spotify_album", master=False),
            )
        peek.assert_not_called()
        resolve.assert_not_called()
        assert update == _blank_update()
        assert not warm_mod._background_tasks

    async def test_master_on_but_all_per_service_off_does_nothing(self):
        update = _blank_update()
        with _patch_cache_peek(None) as peek, _patch_resolver() as resolve:
            await _run(update, settings=_settings(master=True))
        peek.assert_not_called()
        resolve.assert_not_called()
        assert not warm_mod._background_tasks

    @pytest.mark.parametrize("missing", ["pg", "entity_store", "artist", "album"])
    async def test_skips_when_precondition_missing(self, missing):
        update = _blank_update()
        kwargs = {
            "clients": _clients(),
            "pg": AsyncMock(spec=PgSource),
            "entity_store": _entity_store(),
            "request_artist": _ARTIST,
            "request_album": _ALBUM,
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
        with _patch_cache_peek(None) as peek, _patch_resolver() as resolve:
            await apply_streaming_url_postprocess(update, **kwargs)
        peek.assert_not_called()
        resolve.assert_not_called()
        assert not warm_mod._background_tasks


# ---------------------------------------------------------------------------
# Per-service gating: a skipped service neither reads the cache nor enqueues.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(_CASES))
class TestPerServiceGating:
    async def test_skips_service_when_url_already_set(self, service):
        case = _CASES[service]
        update = _blank_update()
        update[case["url_field"]] = "https://existing.test/x"
        with _patch_cache_peek(None) as peek, _patch_resolver():
            await _run(update, settings=_settings(service))
        peek.assert_not_called()
        assert update[case["url_field"]] == "https://existing.test/x"
        assert not warm_mod._background_tasks

    async def test_empty_string_sentinel_is_respected(self, service):
        # "" means "explicitly checked, nothing to surface" — must NOT trigger a
        # fresh cache read/probe (``is not None``, not truthiness).
        case = _CASES[service]
        update = _blank_update()
        update[case["url_field"]] = ""
        with _patch_cache_peek(None) as peek, _patch_resolver():
            await _run(update, settings=_settings(service))
        peek.assert_not_called()
        assert update[case["url_field"]] == ""
        assert not warm_mod._background_tasks

    async def test_skips_service_when_client_is_none(self, service):
        case = _CASES[service]
        update = _blank_update()
        with _patch_cache_peek(None) as peek, _patch_resolver():
            await _run(
                update,
                clients=_clients(**{case["client_attr"]: None}),
                settings=_settings(service),
            )
        peek.assert_not_called()
        assert update[case["url_field"]] is None
        assert not warm_mod._background_tasks

    async def test_per_service_flag_off_reads_only_the_enabled_service(self, service):
        # Enable the OTHER service only; the parametrized service stays off. The
        # enabled service hits the cache (sync fill); the disabled one is never
        # read.
        case = _CASES[service]
        other = next(s for s in _CASES if s != service)
        other_url = _CASES[other]["resolved_url"]
        update = _blank_update()
        read_services: list[str] = []

        async def fake_peek(pg, *, service, artist, album, **kwargs):
            read_services.append(service)
            return other_url, True, False

        with (
            patch.object(mod, "peek_cached_streaming_url", new=AsyncMock(side_effect=fake_peek)),
            _patch_resolver(),
        ):
            await _run(update, settings=_settings(other))
        assert read_services == [other]
        assert update[case["url_field"]] is None
        assert update[_CASES[other]["url_field"]] == other_url
        assert not warm_mod._background_tasks


# ---------------------------------------------------------------------------
# Cache hit: synchronous fill, no probe, no task, no mint.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(_CASES))
class TestCacheHit:
    async def test_hit_fills_url_synchronously_without_probe_task_or_mint(self, service):
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        scope, transaction = _sentry_scope()

        with (
            _patch_cache_peek(case["resolved_url"]),
            _patch_resolver() as resolve,
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(update, settings=_settings(service), entity_store=entity_store)

        assert update[case["url_field"]] == case["resolved_url"]
        resolve.assert_not_called()
        entity_store.mint_or_get_release_identity.assert_not_called()
        assert not warm_mod._background_tasks
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.fired.{service}") is True
        assert calls.get(f"streaming_url.persistent_lookup.cache_hit.{service}") is True


# ---------------------------------------------------------------------------
# Known recent miss: the cache already holds a fresh "not found" — no warm.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(_CASES))
class TestKnownRecentMiss:
    async def test_recent_miss_does_not_enqueue_a_no_op_warm(self, service):
        case = _CASES[service]
        update = _blank_update()
        scope, transaction = _sentry_scope()

        with (
            _patch_cache_peek(None, has_fresh_decision=True),
            _patch_resolver() as resolve,
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(update, settings=_settings(service))

        assert update[case["url_field"]] is None
        resolve.assert_not_called()
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_recent.{service}") is True


# ---------------------------------------------------------------------------
# LML#1115: a fresh LML#1121 ERROR row (couldn't ask) must not be treated like
# a genuine known miss above -- it DOES enqueue a bounded, deduplicated warm
# (re-asking after a transient outage is the point), gated by the SAME bulk
# suppression a genuine miss uses, and always reports the single
# ``cache_error_recent`` Sentry outcome regardless of which of
# enqueued/suppressed/shed it lands in.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(_CASES))
class TestFreshErrorRowMiss:
    async def test_fresh_error_row_warm_bypasses_the_row_and_performs_the_live_call(self, service):
        # LML#1121 FIX 2: the warm enqueued BECAUSE of a fresh error row must
        # bypass that SAME row on its OWN read (error_ttl clamped to zero for
        # that one read) and actually probe -- otherwise the warm's own
        # resolve_streaming_url_with_cache call re-reads the identical row a
        # few milliseconds later and short-circuits on it, a guaranteed
        # no-op that still consumes a warm slot and never performs the retry
        # its comment promises. This drives the REAL resolver (not
        # _patch_resolver, which would agree with whatever contract the bug
        # left behind) against a fake pg.fetchone that emulates the SQL
        # error-TTL cutoff: fresh under the live ~5-minute error_ttl, but
        # stale the instant error_ttl is clamped to zero -- so this only
        # passes when the warm actually threads the zeroed override through.
        case = _CASES[service]
        update = _blank_update()
        scope, transaction = _sentry_scope()
        row_last_checked_at = datetime.now(UTC) - timedelta(seconds=5)

        async def fake_fetchone(
            _sql, _service, _artist_key, _album_key, _miss_cutoff, error_cutoff
        ):
            # Mirrors _SELECT_SQL's "is_error AND last_checked_at > $5"
            # branch: fresh against the default ~5-minute error_ttl, but
            # stale the instant the caller clamps error_ttl to zero
            # (error_cutoff == now).
            if row_last_checked_at > error_cutoff:
                return {"url": None, "is_error": True}
            return None

        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=fake_fetchone)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        entity_store = _entity_store()
        clients = _clients()
        clients[case["client_attr"]].find_album_match = AsyncMock(
            return_value=SourceMatch(url=case["resolved_url"], confidence=95.0)
        )

        with (
            _patch_cache_peek(None, has_fresh_decision=True, is_error=True),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            result = await _run(
                update,
                settings=_settings(service),
                clients=clients,
                entity_store=entity_store,
                pg=pg,
            )
            # Unlike a genuine recent miss, a fresh error row DOES schedule a
            # warm -- exactly one, same as a genuine absent/stale miss would.
            assert len(warm_mod._background_tasks) == 1
            await _drain_background_tasks()

        # The live call actually happened -- not a guaranteed short-circuit
        # on the same error row that triggered the warm.
        clients[case["client_attr"]].find_album_match.assert_awaited_once()
        entity_store.mint_or_get_release_identity.assert_awaited_once_with(
            source=service, external_id=case["external_id"]
        )
        assert update[case["url_field"]] is None
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_error_recent.{service}") is True
        # LML#1121 FIX 7: the enqueue sub-outcome is tagged ALONGSIDE
        # cache_error_recent now, not masked by it.
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_enqueued.{service}") is True
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_recent.{service}") is None
        assert result == {case["client_attr"]: StreamingResolutionStatus.unresolved}

    async def test_fresh_error_row_stays_unwarmed_when_bulk_suppressed(self, service):
        case = _CASES[service]
        update = _blank_update()
        scope, transaction = _sentry_scope()
        set_suppress_streaming_warm(True)

        with (
            _patch_cache_peek(None, has_fresh_decision=True, is_error=True),
            _patch_resolver() as resolve,
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            result = await _run(update, settings=_settings(service))

        # Bulk suppression gates a fresh error row exactly like a genuine
        # miss -- an outage during a bulk drain must not spawn request-time
        # warms either.
        resolve.assert_not_called()
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_error_recent.{service}") is True
        # LML#1121 FIX 7: cache_miss_unwarmed is the depth-shed/suppression
        # signal docs/env-vars.md documents; it must fire ALONGSIDE
        # cache_error_recent for a suppressed error row, not be masked by it.
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_unwarmed.{service}") is True
        assert result == {case["client_attr"]: StreamingResolutionStatus.unresolved}

    async def test_fresh_error_row_is_still_subject_to_the_depth_bound(self, service):
        # LML#1108's depth bound must apply to this branch too -- the whole
        # point of reusing ``_enqueue_streaming_warm`` (rather than a bespoke
        # unbounded enqueue) instead of writing a parallel warm path for error
        # rows. Fill the in-flight set to the bound first so a healthy enqueue
        # would shed; an error-row peek must shed here too, not bypass it.
        case = _CASES[service]
        bound = warm_mod._streaming_warm_queue_depth_bound()
        for i in range(bound):
            warm_mod._streaming_warm_in_flight.add(("dummy", f"artist{i}", f"album{i}"))

        update = _blank_update()
        scope, transaction = _sentry_scope()
        with (
            _patch_cache_peek(None, has_fresh_decision=True, is_error=True),
            _patch_resolver() as resolve,
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            result = await _run(update, settings=_settings(service))

        assert not warm_mod._background_tasks
        resolve.assert_not_awaited()
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_error_recent.{service}") is True
        # LML#1121 FIX 7: cache_miss_shed is docs/env-vars.md's designated
        # depth-shed signal; it must not go silent for an error-triggered
        # shed -- exactly the dominant-service-under-outage case where
        # shedding peaks and the signal matters most.
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_shed.{service}") is True
        assert result == {case["client_attr"]: StreamingResolutionStatus.unresolved}


# ---------------------------------------------------------------------------
# Genuine miss: response returns without the URL; one bounded background warm
# is enqueued; the probe runs (and mints) only in that background task.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCacheMissEnqueuesBackgroundWarm:
    async def test_miss_does_not_probe_on_the_response_path(self):
        # The architectural invariant for #706: a miss must NOT await the live
        # probe synchronously. Gate the probe on an Event so even if the task
        # starts it cannot complete; assert the response returns with the field
        # still null and the probe not yet awaited-to-completion.
        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        release = asyncio.Event()
        probed: list[str] = []

        async def gated_resolve(pg, client, *, service, artist, album, **kwargs):
            probed.append(service)
            await release.wait()
            return ResolveOutcome(url=case["resolved_url"], source="live_resolved")

        with (
            _patch_cache_peek(None),
            patch.object(
                warm_mod,
                "resolve_streaming_url_with_cache",
                new=AsyncMock(side_effect=gated_resolve),
            ),
        ):
            await _run(update, settings=_settings(service))
            # Response path completed: field still null, exactly one warm queued,
            # and the probe has NOT returned a URL onto the response.
            assert update[case["url_field"]] is None
            assert len(warm_mod._background_tasks) == 1

            # Let the gated warm finish; it writes the cache (mocked) but must
            # NOT backfill the already-returned response dict.
            release.set()
            await _drain_background_tasks()

        assert probed == [service]
        assert update[case["url_field"]] is None
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight

    async def test_response_path_records_zero_probe_awaits_even_when_probe_raises(self):
        # PR2 of #706 hardens the Event-gated ordering test above into a
        # raise-if-awaited spy. The distinction matters: the post-process
        # contains ``return_exceptions=True`` and the warm swallows every
        # exception, so a regression that re-inlines the probe would NOT
        # surface as a raised error — the request would silently block again.
        # The spy therefore RECORDS each await; the load-bearing assertion is
        # that the record is empty when the response path returns, regardless
        # of what the probe does (here: fail fast, the cheapest probe there is
        # — if even a raise-immediately probe leaves a record before the
        # response completes, the probe is back on the hot path).
        service = "apple_music_album"
        update = _blank_update()
        probe_awaits: list[str] = []

        async def raise_if_awaited(pg, client, *, service, artist, album, **kwargs):
            probe_awaits.append(service)
            raise AssertionError("streaming probe awaited on the /lookup response path")

        with (
            _patch_cache_peek(None),
            patch.object(
                warm_mod,
                "resolve_streaming_url_with_cache",
                new=AsyncMock(side_effect=raise_if_awaited),
            ),
        ):
            await _run(update, settings=_settings(service))
            # Response path complete: the probe was never awaited, not even a
            # variant that would have returned control immediately. NOTE: this
            # also pins that the enqueue loop is the function's tail — a
            # benign trailing await added after the loop would give the warm
            # task a slot to start and trip this assertion even though the hot
            # path stayed probe-free. That's deliberate strictness; if you add
            # such an await, update this assertion knowingly (the Event-gated
            # test above still carries the pure does-not-WAIT invariant).
            assert probe_awaits == []
            assert len(warm_mod._background_tasks) == 1

            await _drain_background_tasks()

        # The warm DID run the probe off-path and swallowed its failure.
        assert probe_awaits == [service]
        assert update["apple_music_url"] is None
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight

    @pytest.mark.parametrize("service", list(_CASES))
    async def test_background_warm_runs_probe_and_mints_on_live_resolved(self, service):
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        pg = AsyncMock(spec=PgSource)

        with (
            _patch_cache_peek(None),
            _patch_resolver(
                return_value=ResolveOutcome(url=case["resolved_url"], source="live_resolved")
            ) as resolve,
        ):
            await _run(update, settings=_settings(service), entity_store=entity_store, pg=pg)
            # Nothing minted or probed synchronously.
            resolve.assert_not_awaited()
            entity_store.mint_or_get_release_identity.assert_not_called()

            await _drain_background_tasks()

        # The background task ran the live probe with the shared pool + request
        # values, then minted the parsed external_id.
        resolve.assert_awaited_once()
        _, kwargs = resolve.await_args
        assert kwargs["service"] == service
        assert kwargs["artist"] == _ARTIST
        assert kwargs["album"] == _ALBUM
        entity_store.mint_or_get_release_identity.assert_awaited_once_with(
            source=service, external_id=case["external_id"]
        )

    @pytest.mark.parametrize("source", ["cache_miss_recent", "live_miss", "live_error"])
    async def test_background_warm_no_url_outcomes_do_not_mint(self, source):
        service = "apple_music_album"
        update = _blank_update()
        entity_store = _entity_store()

        with (
            _patch_cache_peek(None),
            _patch_resolver(return_value=ResolveOutcome(url=None, source=source)),
        ):
            await _run(update, settings=_settings(service), entity_store=entity_store)
            await _drain_background_tasks()

        assert update["apple_music_url"] is None
        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_background_warm_unparseable_url_skips_mint(self):
        # The off-path warm never backfills ``update``; here we only assert the
        # validate-before-mint guard: an Apple id of '000000' parses (\d{6,})
        # but fails the leading-zero validator, so the warm skips the mint.
        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        url = case["unmintable_but_parseable_url"]

        with (
            _patch_cache_peek(None),
            _patch_resolver(return_value=ResolveOutcome(url=url, source="live_resolved")),
        ):
            await _run(update, settings=_settings(service), entity_store=entity_store)
            await _drain_background_tasks()

        entity_store.mint_or_get_release_identity.assert_not_called()

    async def test_background_warm_mint_failure_is_swallowed(self):
        # The mint is best-effort: a PG failure inside the background task must
        # not escape (it is a fire-and-forget task that must never crash the loop).
        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        entity_store = _entity_store()
        entity_store.mint_or_get_release_identity = AsyncMock(side_effect=RuntimeError("PG down"))

        with (
            _patch_cache_peek(None),
            _patch_resolver(
                return_value=ResolveOutcome(url=case["resolved_url"], source="live_resolved")
            ),
        ):
            await _run(update, settings=_settings(service), entity_store=entity_store)
            await _drain_background_tasks()

        # Reached the mint, swallowed the error, left the task set clean.
        entity_store.mint_or_get_release_identity.assert_awaited_once()
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight

    async def test_background_warm_probe_exception_is_swallowed(self):
        # resolve_streaming_url_with_cache swallows its own client errors, but a
        # wait_for timeout (or any surprise) must not escape the task either.
        service = "apple_music_album"
        update = _blank_update()

        with (
            _patch_cache_peek(None),
            _patch_resolver(side_effect=TimeoutError("probe timed out")),
        ):
            await _run(update, settings=_settings(service))
            await _drain_background_tasks()

        assert update["apple_music_url"] is None
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight

    async def test_background_warm_bandcamp_transport_failure_no_mint_writes_short_ttl_error_row(
        self,
    ):
        # LML#1115 + LML#1121: end-to-end through the REAL BandcampClient
        # (mocked only at the HTTP transport) and the REAL
        # resolve_streaming_url_with_cache -- not _patch_resolver's stand-in,
        # which would agree with whatever the client's contract used to be. A
        # default-mode Bandcamp transport failure (5xx here) must not crash
        # this fire-and-forget task and must not mint. #1115's headline fix
        # (must not UPSERT a 7-day known-miss row for an outage) still holds.
        # #1121 review found that dropping the write ENTIRELY also left no
        # row for a *reader* to consult -- the dedup key in
        # `_streaming_warm_in_flight` is discarded once this task finishes,
        # so every subsequent /lookup for the same album still re-enqueues a
        # fresh warm for as long as the outage lasts, unchanged by this fix
        # (see the canonical rationale in lookup/streaming_url_postprocess.py;
        # LML#1141 tracks throttling that). What #1121 fixes: it still writes
        # a row, marked `is_error=True` so it ages on the short `error_ttl`
        # (default 5 minutes) instead of the 7-day `miss_ttl`, letting a
        # reader tell a couldn't-ask apart from a confirmed absence.
        service = "bandcamp"
        update = _blank_update()
        entity_store = _entity_store()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        bandcamp = BandcampClient()
        bandcamp._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
        bandcamp._http = AsyncMock(spec=httpx.AsyncClient)
        bandcamp._http.request = AsyncMock(
            return_value=httpx.Response(
                503, request=httpx.Request("GET", _BANDCAMP_AUTOCOMPLETE_URL)
            )
        )

        with _patch_cache_peek(None):
            await _run(
                update,
                settings=_settings(service),
                clients=_clients(bandcamp=bandcamp),
                entity_store=entity_store,
                pg=pg,
            )
            await _drain_background_tasks()

        assert update["bandcamp_url"] is None
        entity_store.mint_or_get_release_identity.assert_not_called()
        pg.execute.assert_awaited_once()
        call = pg.execute.await_args
        assert call.args[4] is None
        assert call.args[5] is True
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight

    async def test_background_warm_slow_bandcamp_response_still_writes_an_error_row(self):
        # LML#1121 FIX 4: the dominant Cloudflare-under-load failure mode --
        # a Bandcamp response slow enough to outlast the warm's OWN
        # asyncio.wait_for ceiling -- must still write a short-TTL error
        # row. Pre-fix, the coroutine got cancelled by the OUTER wait_for (a
        # BaseException that sails past resolve_streaming_url_with_cache's
        # except Exception and the whole try block, so no UPSERT ever ran).
        # This proves BandcampClient now honors the LML#1108 probe deadline
        # and raises a normal BandcampTransportError well inside that
        # ceiling, giving the resolver a chance to write the row before the
        # outer cancellation could ever fire. Shrinks Bandcamp's normal 9.0s
        # ceiling to 1.5s (via a config override) so the test stays fast --
        # the fake response hangs for 10s, far longer than either ceiling.
        service = "bandcamp"
        update = _blank_update()
        entity_store = _entity_store()
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        bandcamp = BandcampClient()
        bandcamp._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
        bandcamp._http = AsyncMock(spec=httpx.AsyncClient)

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(10.0)
            return httpx.Response(  # pragma: no cover
                200, json={"results": []}, request=httpx.Request("GET", _BANDCAMP_AUTOCOMPLETE_URL)
            )

        bandcamp._http.request = AsyncMock(side_effect=_hang)

        small_cfg = dataclasses.replace(
            mod.STREAMING_URL_CACHE_CONFIG[StreamingService.BANDCAMP], probe_timeout_s=1.5
        )
        with (
            _patch_cache_peek(None),
            patch.dict(mod.STREAMING_URL_CACHE_CONFIG, {StreamingService.BANDCAMP: small_cfg}),
        ):
            await _run(
                update,
                settings=_settings(service),
                clients=_clients(bandcamp=bandcamp),
                entity_store=entity_store,
                pg=pg,
            )
            await _drain_background_tasks()

        assert update["bandcamp_url"] is None
        entity_store.mint_or_get_release_identity.assert_not_called()
        # The load-bearing assertion: a row WAS written (is_error=True), not
        # silently dropped by an uncaught cancellation.
        pg.execute.assert_awaited_once()
        call = pg.execute.await_args
        assert call.args[4] is None
        assert call.args[5] is True
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight


# ---------------------------------------------------------------------------
# Hot-path robustness: a cache-peek error degrades to a skip, never a 500.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCachePeekError:
    async def test_peek_exception_skips_service_without_raising_or_enqueuing(self):
        # peek normally swallows PG errors, but the gather must contain any
        # surprise so the post-process never propagates out of /lookup.
        update = _blank_update()
        with (
            patch.object(
                mod,
                "peek_cached_streaming_url",
                new=AsyncMock(side_effect=RuntimeError("normalization blew up")),
            ),
            _patch_resolver() as resolve,
        ):
            await _run(update, settings=_settings("apple_music_album"))

        assert update["apple_music_url"] is None
        resolve.assert_not_called()
        assert not warm_mod._background_tasks

    async def test_one_service_peek_error_does_not_block_the_other(self):
        # Apple peek raises; Spotify hits. The healthy service must still fill.
        update = _blank_update()

        async def flaky_peek(pg, *, service, artist, album, **kwargs):
            if service == "apple_music_album":
                raise RuntimeError("boom")
            return _CASES["spotify_album"]["resolved_url"], True, False

        with (
            patch.object(mod, "peek_cached_streaming_url", new=AsyncMock(side_effect=flaky_peek)),
            _patch_resolver(),
        ):
            await _run(update, settings=_settings("apple_music_album", "spotify_album"))

        assert update["apple_music_url"] is None
        assert update["spotify_url"] == _CASES["spotify_album"]["resolved_url"]
        assert not warm_mod._background_tasks


# ---------------------------------------------------------------------------
# Bulk suppression: the /lookup/bulk context fills from cache but never warms.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBulkSuppression:
    async def test_genuine_miss_does_not_enqueue_when_suppressed(self):
        service = "apple_music_album"
        update = _blank_update()
        scope, transaction = _sentry_scope()
        set_suppress_streaming_warm(True)

        with (
            _patch_cache_peek(None),
            _patch_resolver() as resolve,
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(update, settings=_settings(service))

        assert update["apple_music_url"] is None
        resolve.assert_not_called()
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_unwarmed.{service}") is True

    async def test_cache_hit_still_fills_when_suppressed(self):
        # Suppression disables the warm, NOT the synchronous cache read-fill.
        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        set_suppress_streaming_warm(True)

        with _patch_cache_peek(case["resolved_url"]), _patch_resolver():
            await _run(update, settings=_settings(service))

        assert update[case["url_field"]] == case["resolved_url"]
        assert not warm_mod._background_tasks


# ---------------------------------------------------------------------------
# Bulk ROWLESS warm exemption (LML#1052 for Spotify, LML#1087 for Bandcamp): a
# per-service dedicated flag lets a cold miss schedule its bounded background
# warm on the otherwise-suppressed /lookup/bulk path, so a NON-LIBRARY (rowless)
# album gets a DIRECT URL warmed into the cache instead of Backend-Service
# synthesizing a keyword-search fallback on every future play of the same
# (artist, album). Scoped to rowless (item.id == 0) items only — library albums
# are already covered by the offline drains (#1069 Bandcamp, #831 Spotify), so
# warming them at request time would re-introduce the serialized backfill the
# bulk suppression exists to prevent. Both flags ship default-off (gated on the
# BS#642 backfill drain) and are INDEPENDENT knobs: one service's flag must never
# exempt another's, and Apple (which has its own synchronous probe on this path)
# is never exempt. The TRIGGERING response still returns null either way — the
# warm is off the response path by design (#1052's self-heal boundary); the yield
# accrues to the next lookup of the same (artist, album).
# ---------------------------------------------------------------------------

# (storage key, the ``_settings`` kwarg backing that service's dedicated bulk
# flag). A future exempt service (YouTube Music, #1103) is one more row here and
# one more row in the module-level mapping the post-process consults.
_BULK_ROWLESS_WARM_CASES = [
    pytest.param("spotify_album", "bulk_spotify_warm", id="spotify"),
    pytest.param("bandcamp", "bulk_bandcamp_warm", id="bandcamp"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "flag_kwarg"), _BULK_ROWLESS_WARM_CASES)
class TestBulkRowlessWarmExemption:
    async def test_flag_on_rowless_miss_enqueues_warm_despite_suppression(
        self, service, flag_kwarg
    ):
        # The load-bearing behavior: suppress_warm is set (bulk), but for a
        # ROWLESS item with this service's dedicated flag on, a genuine miss
        # schedules the warm anyway — projecting cache_miss_enqueued (not
        # cache_miss_unwarmed).
        update = _blank_update()
        scope, transaction = _sentry_scope()
        set_suppress_streaming_warm(True)

        with (
            _patch_cache_peek(None),
            _patch_resolver(return_value=ResolveOutcome(url=None, source="live_miss")),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            result = await _run(
                update, settings=_settings(service, **{flag_kwarg: True}), is_rowless=True
            )
            assert len(warm_mod._background_tasks) == 1
            await _drain_background_tasks()

        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight
        # Sentry keys use the storage key (``spotify_album``), not the catalog key.
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_enqueued.{service}") is True
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_unwarmed.{service}") is None
        # The warm is off the response path: the triggering pass still returns
        # no URL for this service (#1052's AC — do not assert otherwise).
        assert update[_CASES[service]["url_field"]] is None
        catalog_key = _SERVICE_BY_STORAGE_KEY[service].catalog_key
        assert result == {catalog_key: StreamingResolutionStatus.unresolved}

    async def test_flag_on_nonrowless_library_item_stays_suppressed(self, service, flag_kwarg):
        # The guard the #1087 review added, held for every exempt service: a
        # NON-rowless (library, id != 0) item must NOT trip the exemption even
        # with the flag on — library albums are warmed by the offline drains, so
        # a runtime warm per bulk miss would re-create the serialized backfill the
        # bulk suppression exists to prevent. Suppressed => cache_miss_unwarmed,
        # no warm.
        update = _blank_update()
        scope, transaction = _sentry_scope()
        set_suppress_streaming_warm(True)

        with (
            _patch_cache_peek(None),
            _patch_resolver() as resolve,
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(update, settings=_settings(service, **{flag_kwarg: True}), is_rowless=False)

        resolve.assert_not_called()
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_unwarmed.{service}") is True

    async def test_flag_off_stays_suppressed_and_inert(self, service, flag_kwarg):
        # The client is present in ``clients`` and the per-service persist flag is
        # on, and the item is even ROWLESS — but with the dedicated bulk flag OFF
        # the suppression still applies: no live probe, no background warm —
        # exactly today's bulk behavior. The flag dominates the rowless signal.
        update = _blank_update()
        scope, transaction = _sentry_scope()
        set_suppress_streaming_warm(True)

        with (
            _patch_cache_peek(None),
            _patch_resolver() as resolve,
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(update, settings=_settings(service, **{flag_kwarg: False}), is_rowless=True)

        resolve.assert_not_called()
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_unwarmed.{service}") is True

    async def test_master_kill_switch_restores_suppression(self, service, flag_kwarg):
        # AND-gate term 1 (``lml_persist_streaming_urls``): the master kill switch
        # short-circuits the whole post-process before the cache peek, so the
        # dedicated flag cannot revive a warm on its own.
        update = _blank_update()
        set_suppress_streaming_warm(True)

        with _patch_cache_peek(None) as peek, _patch_resolver() as resolve:
            result = await _run(
                update,
                settings=_settings(service, master=False, **{flag_kwarg: True}),
                is_rowless=True,
            )

        peek.assert_not_called()
        resolve.assert_not_called()
        assert not warm_mod._background_tasks
        assert result == {}

    async def test_per_service_kill_switch_restores_suppression(self, service, flag_kwarg):
        # AND-gate term 2 (``lml_persist_streaming_url_<service>``): with the
        # per-service persist flag off the service never enters ``active``, so the
        # dedicated bulk flag has nothing to exempt. Apple is enabled here only to
        # keep the post-process past its early return.
        update = _blank_update()
        set_suppress_streaming_warm(True)

        with _patch_cache_peek(None), _patch_resolver() as resolve:
            result = await _run(
                update,
                settings=_settings("apple_music_album", **{flag_kwarg: True}),
                is_rowless=True,
            )

        resolve.assert_not_called()
        assert not warm_mod._background_tasks
        assert _SERVICE_BY_STORAGE_KEY[service].catalog_key not in result

    async def test_flag_only_consulted_under_suppression(self, service, flag_kwarg):
        # The interactive /lookup path (no suppression) already warms on a miss;
        # the dedicated flag must not gate that path — a miss with suppression OFF
        # warms regardless of the flag's value.
        update = _blank_update()
        with (
            _patch_cache_peek(None),
            _patch_resolver(return_value=ResolveOutcome(url=None, source="live_miss")),
        ):
            await _run(update, settings=_settings(service, **{flag_kwarg: False}))
            assert len(warm_mod._background_tasks) == 1
            await _drain_background_tasks()
        assert not warm_mod._background_tasks


@pytest.mark.asyncio
class TestBulkRowlessWarmExemptionIsPerService:
    @pytest.mark.parametrize(
        ("service", "other_flag_kwarg"),
        [
            pytest.param("spotify_album", "bulk_bandcamp_warm", id="bandcamp-flag-vs-spotify"),
            pytest.param("bandcamp", "bulk_spotify_warm", id="spotify-flag-vs-bandcamp"),
            pytest.param("apple_music_album", "bulk_bandcamp_warm", id="bandcamp-flag-vs-apple"),
            pytest.param("apple_music_album", "bulk_spotify_warm", id="spotify-flag-vs-apple"),
        ],
    )
    async def test_another_services_flag_does_not_exempt(self, service, other_flag_kwarg):
        # The dedicated flags are independent: turning one on must not lift the
        # bulk suppression for any other service — the #1052 regression guard on
        # #1087's Bandcamp behavior and vice versa. Apple is never exempt at all
        # (it has its own synchronous probe on the enrichment path).
        update = _blank_update()
        scope, transaction = _sentry_scope()
        set_suppress_streaming_warm(True)

        with (
            _patch_cache_peek(None),
            _patch_resolver() as resolve,
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(
                update, settings=_settings(service, **{other_flag_kwarg: True}), is_rowless=True
            )

        resolve.assert_not_called()
        assert not warm_mod._background_tasks
        assert not warm_mod._streaming_warm_in_flight
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get(f"streaming_url.persistent_lookup.cache_miss_unwarmed.{service}") is True


# ---------------------------------------------------------------------------
# LML#1053: the per-service resolution-status return value. The hot-path
# disposition already computed for the Sentry projection (cache_hit /
# cache_miss_recent / cache_miss_enqueued / cache_miss_unwarmed / peek error)
# maps onto the StreamingResolutionStatus vocabulary consumed by
# ``lookup/enrichment/item.py`` to build ``DiscogsSearchResult.streaming_status``.
# Keyed by ``cfg.client_attr`` (``apple_music`` / ``spotify`` / ``bandcamp``) —
# the same keys the wire contract's ``StreamingResolution`` object uses — NOT
# the registry's ``service_key`` (``apple_music_album`` / ``spotify_album`` /
# ``bandcamp``). A service the post-process never actually consulted (skipped
# for any reason) must be absent from the returned dict, not mapped to a
# status — the caller's never-consulted omission relies on key absence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStreamingStatusReturnValue:
    @pytest.mark.parametrize("service", list(_CASES))
    async def test_cache_hit_returns_verified(self, service):
        case = _CASES[service]
        update = _blank_update()
        with _patch_cache_peek(case["resolved_url"]), _patch_resolver():
            result = await _run(update, settings=_settings(service))
        assert result == {case["client_attr"]: StreamingResolutionStatus.verified}

    @pytest.mark.parametrize("service", list(_CASES))
    async def test_known_recent_miss_returns_absent(self, service):
        case = _CASES[service]
        update = _blank_update()
        with _patch_cache_peek(None, has_fresh_decision=True), _patch_resolver():
            result = await _run(update, settings=_settings(service))
        assert result == {case["client_attr"]: StreamingResolutionStatus.absent}

    @pytest.mark.parametrize("service", list(_CASES))
    async def test_fresh_error_row_returns_unresolved_not_absent(self, service):
        # LML#1115: the core bug this fix closes -- a fresh LML#1121 error row
        # (couldn't ask) must NOT report the confirmed-negative ``absent``
        # verdict a genuine known miss reports just above. It reports
        # ``unresolved``, same as any other "didn't reach a verdict" case.
        case = _CASES[service]
        update = _blank_update()
        with (
            _patch_cache_peek(None, has_fresh_decision=True, is_error=True),
            _patch_resolver(return_value=ResolveOutcome(url=None, source="live_error")),
        ):
            result = await _run(update, settings=_settings(service))
            assert result == {case["client_attr"]: StreamingResolutionStatus.unresolved}
            await _drain_background_tasks()

    @pytest.mark.parametrize("service", list(_CASES))
    async def test_genuine_miss_enqueued_returns_unresolved(self, service):
        case = _CASES[service]
        update = _blank_update()
        with _patch_cache_peek(None), _patch_resolver():
            result = await _run(update, settings=_settings(service))
            assert result == {case["client_attr"]: StreamingResolutionStatus.unresolved}
            await _drain_background_tasks()

    async def test_bulk_suppressed_miss_returns_unresolved(self):
        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        set_suppress_streaming_warm(True)
        with _patch_cache_peek(None), _patch_resolver():
            result = await _run(update, settings=_settings(service))
        assert result == {case["client_attr"]: StreamingResolutionStatus.unresolved}

    async def test_peek_exception_returns_unresolved(self):
        # A swallowed cache-peek error is an attempted-but-inconclusive
        # consult, same bucket as a timeout — not a silent never-consulted.
        update = _blank_update()
        with (
            patch.object(
                mod,
                "peek_cached_streaming_url",
                new=AsyncMock(side_effect=RuntimeError("normalization blew up")),
            ),
            _patch_resolver(),
        ):
            result = await _run(update, settings=_settings("apple_music_album"))
        assert result == {"apple_music": StreamingResolutionStatus.unresolved}

    @pytest.mark.parametrize(
        "missing", ["master_off", "per_service_off", "client_none", "url_already_set"]
    )
    async def test_skipped_service_is_absent_from_the_returned_dict(self, missing):
        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        clients = _clients()
        settings = _settings(service)
        if missing == "master_off":
            settings = _settings(service, master=False)
        elif missing == "per_service_off":
            settings = _settings()
        elif missing == "client_none":
            clients = _clients(**{case["client_attr"]: None})
        elif missing == "url_already_set":
            update[case["url_field"]] = "https://existing.test/x"

        with _patch_cache_peek(None), _patch_resolver():
            result = await _run(update, settings=settings, clients=clients)
        assert case["client_attr"] not in result

    async def test_master_flag_off_returns_empty_dict_for_every_service(self):
        update = _blank_update()
        with _patch_cache_peek(None), _patch_resolver():
            result = await _run(
                update, settings=_settings("apple_music_album", "spotify_album", master=False)
            )
        assert result == {}

    async def test_missing_precondition_returns_empty_dict(self):
        update = _blank_update()
        with _patch_cache_peek(None), _patch_resolver():
            result = await apply_streaming_url_postprocess(
                update,
                clients=_clients(),
                pg=None,  # missing precondition
                entity_store=_entity_store(),
                request_artist=_ARTIST,
                request_album=_ALBUM,
                settings=_settings("apple_music_album"),
            )
        assert result == {}

    async def test_mixed_outcomes_report_independently_per_service(self):
        # Apple hits, Spotify is a genuine unwarmed miss (bulk) — the return
        # value must carry both verdicts independently, keyed by client_attr.
        update = _blank_update()
        set_suppress_streaming_warm(True)

        async def fake_peek(pg, *, service, artist, album, **kwargs):
            if service == "apple_music_album":
                return _CASES["apple_music_album"]["resolved_url"], True, False
            return None, False, False

        with (
            patch.object(mod, "peek_cached_streaming_url", new=AsyncMock(side_effect=fake_peek)),
            _patch_resolver(),
        ):
            result = await _run(update, settings=_settings("apple_music_album", "spotify_album"))

        assert result == {
            "apple_music": StreamingResolutionStatus.verified,
            "spotify": StreamingResolutionStatus.unresolved,
        }


# ---------------------------------------------------------------------------
# Dedup: identical concurrent misses enqueue a single probe.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDedup:
    async def test_two_identical_concurrent_misses_enqueue_one_probe(self):
        service = "apple_music_album"
        case = _CASES[service]
        release = asyncio.Event()
        probed: list[str] = []

        async def gated_resolve(pg, client, *, service, artist, album, **kwargs):
            probed.append(service)
            await release.wait()
            return ResolveOutcome(url=case["resolved_url"], source="live_resolved")

        with (
            _patch_cache_peek(None),
            patch.object(
                warm_mod,
                "resolve_streaming_url_with_cache",
                new=AsyncMock(side_effect=gated_resolve),
            ),
        ):
            # Fire two concurrent lookups for the SAME (artist, album); the
            # enqueue is synchronous so only the first wins the dedup set.
            await asyncio.gather(
                _run(_blank_update(), settings=_settings(service)),
                _run(_blank_update(), settings=_settings(service)),
            )
            assert len(warm_mod._background_tasks) == 1

            release.set()
            await _drain_background_tasks()

        assert probed == [service]
        assert not warm_mod._streaming_warm_in_flight

    async def test_dedup_key_clears_after_warm_completes(self):
        # A second lookup AFTER the first warm drained must enqueue afresh
        # (the dedup set is cleaned in the done-callback, not leaked).
        service = "apple_music_album"
        case = _CASES[service]
        with (
            _patch_cache_peek(None),
            _patch_resolver(
                return_value=ResolveOutcome(url=case["resolved_url"], source="live_resolved")
            ) as resolve,
        ):
            await _run(_blank_update(), settings=_settings(service))
            await _drain_background_tasks()
            assert not warm_mod._streaming_warm_in_flight

            await _run(_blank_update(), settings=_settings(service))
            await _drain_background_tasks()

        assert resolve.await_count == 2


# ---------------------------------------------------------------------------
# Sentry: the hot path tags the active transaction; the background task does not.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSentryProjection:
    async def test_miss_projects_enqueued_and_background_never_touches_active_scope(self):
        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        scope, transaction = _sentry_scope()

        with (
            _patch_cache_peek(None),
            _patch_resolver(
                return_value=ResolveOutcome(url=case["resolved_url"], source="live_resolved")
            ),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(update, settings=_settings(service))
            await _drain_background_tasks()

        keys = {c.args[0] for c in transaction.set_data.call_args_list}
        assert f"streaming_url.persistent_lookup.fired.{service}" in keys
        assert f"streaming_url.persistent_lookup.cache_miss_enqueued.{service}" in keys
        # The background ``live_resolved`` outcome must NOT land on the active
        # transaction — the request scope has closed; a tag here would
        # mis-attribute to the next request (mirrors _warm_bio_cache).
        assert not any(".live_resolved." in k for k in keys)
        assert not any(".cache_hit." in k for k in keys)

    async def test_no_active_transaction_does_not_crash(self):
        update = _blank_update()
        scope = Mock()
        scope.transaction = None
        with (
            _patch_cache_peek(_CASES["apple_music_album"]["resolved_url"]),
            _patch_resolver(),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(update, settings=_settings("apple_music_album"))
        assert update["apple_music_url"] == _CASES["apple_music_album"]["resolved_url"]

    async def test_set_data_failure_does_not_break_request(self):
        update = _blank_update()
        transaction = Mock()
        transaction.set_data = Mock(side_effect=RuntimeError("boom"))
        scope = Mock()
        scope.transaction = transaction
        with (
            _patch_cache_peek(_CASES["apple_music_album"]["resolved_url"]),
            _patch_resolver(),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            await _run(update, settings=_settings("apple_music_album"))
        assert update["apple_music_url"] == _CASES["apple_music_album"]["resolved_url"]


# ---------------------------------------------------------------------------
# Background-warm concurrency bound (LML_STREAMING_WARM_CONCURRENCY).
# ---------------------------------------------------------------------------


class TestWarmConcurrencyBound:
    def test_env_var_name_and_default(self):
        assert warm_mod._STREAMING_WARM_CONCURRENCY_ENV_VAR == "LML_STREAMING_WARM_CONCURRENCY"
        assert warm_mod._STREAMING_WARM_CONCURRENCY_DEFAULT == 4

    def test_semaphore_uses_env_override(self, monkeypatch):
        monkeypatch.setenv(warm_mod._STREAMING_WARM_CONCURRENCY_ENV_VAR, "2")
        warm_mod._streaming_warm_semaphore = None
        sem = warm_mod._get_streaming_warm_semaphore()
        assert sem._value == 2
        warm_mod._streaming_warm_semaphore = None

    def test_semaphore_falls_back_to_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(warm_mod._STREAMING_WARM_CONCURRENCY_ENV_VAR, raising=False)
        warm_mod._streaming_warm_semaphore = None
        sem = warm_mod._get_streaming_warm_semaphore()
        assert sem._value == warm_mod._STREAMING_WARM_CONCURRENCY_DEFAULT
        warm_mod._streaming_warm_semaphore = None


# ---------------------------------------------------------------------------
# LML#1108: pending-warm depth bound. The semaphore above bounds *concurrency*;
# nothing previously bounded how many warms could be QUEUED behind it --
# LIBRARY-METADATA-LOOKUP-1B saw a 232-deep backlog under a Spotify 429 storm.
# ---------------------------------------------------------------------------


class TestWarmQueueDepthBoundResolution:
    def test_depth_bound_is_concurrency_times_the_default_multiplier(self, monkeypatch):
        monkeypatch.delenv(streaming_warm_admission.QUEUE_DEPTH_MULTIPLIER_ENV_VAR, raising=False)
        monkeypatch.setenv(warm_mod._STREAMING_WARM_CONCURRENCY_ENV_VAR, "1")
        warm_mod._streaming_warm_semaphore = None
        warm_mod._get_streaming_warm_semaphore()
        assert warm_mod._streaming_warm_concurrency == 1
        assert (
            warm_mod._streaming_warm_queue_depth_bound()
            == 1 * streaming_warm_admission.QUEUE_DEPTH_MULTIPLIER_DEFAULT
        )
        warm_mod._streaming_warm_semaphore = None

    def test_depth_bound_honors_the_multiplier_env_override(self, monkeypatch):
        """LML#1108 review finding 6: with #1094 forbidding a runtime bump to
        ``LML_STREAMING_WARM_CONCURRENCY``, this multiplier is now the only
        no-redeploy lever available for the depth bound -- confirm it's
        actually live-tunable, read fresh (not cached at semaphore-build
        time like the concurrency itself)."""
        monkeypatch.setenv(warm_mod._STREAMING_WARM_CONCURRENCY_ENV_VAR, "1")
        warm_mod._streaming_warm_semaphore = None
        warm_mod._get_streaming_warm_semaphore()

        monkeypatch.setenv(streaming_warm_admission.QUEUE_DEPTH_MULTIPLIER_ENV_VAR, "32")
        assert warm_mod._streaming_warm_queue_depth_bound() == 32

        monkeypatch.setenv(streaming_warm_admission.QUEUE_DEPTH_MULTIPLIER_ENV_VAR, "8")
        assert warm_mod._streaming_warm_queue_depth_bound() == 8

        warm_mod._streaming_warm_semaphore = None


@pytest.mark.asyncio
class TestWarmQueueDepthBound:
    async def test_enqueue_succeeds_one_below_the_bound(self):
        bound = warm_mod._streaming_warm_queue_depth_bound()
        for i in range(bound - 1):
            warm_mod._streaming_warm_in_flight.add(("dummy", f"artist{i}", f"album{i}"))

        service = "apple_music_album"
        update = _blank_update()
        with (
            _patch_cache_peek(None),
            _patch_resolver(return_value=ResolveOutcome(url=None, source="live_error")),
        ):
            statuses = await _run(update, settings=_settings(service))
            assert len(warm_mod._background_tasks) == 1
            await _drain_background_tasks()

        assert statuses["apple_music"] == StreamingResolutionStatus.unresolved

    async def test_enqueue_sheds_at_the_bound_instead_of_queueing(self):
        bound = warm_mod._streaming_warm_queue_depth_bound()
        for i in range(bound):
            warm_mod._streaming_warm_in_flight.add(("dummy", f"artist{i}", f"album{i}"))

        service = "apple_music_album"
        update = _blank_update()
        with _patch_cache_peek(None), _patch_resolver() as resolve:
            statuses = await _run(update, settings=_settings(service))

        # Shed, not queued: no new task, the probe never scheduled.
        assert not warm_mod._background_tasks
        resolve.assert_not_awaited()
        # The response contract is unaffected by shedding (LML#1108 constraint):
        # still "unresolved", exactly like a healthy enqueue would report.
        assert statuses["apple_music"] == StreamingResolutionStatus.unresolved

    async def test_shed_projects_distinct_sentry_outcome_and_counts_via_cache_stats(self):
        bound = warm_mod._streaming_warm_queue_depth_bound()
        for i in range(bound):
            warm_mod._streaming_warm_in_flight.add(("dummy", f"artist{i}", f"album{i}"))

        service = "apple_music_album"
        update = _blank_update()
        scope, transaction = _sentry_scope()
        recorder = Mock()

        with (
            _patch_cache_peek(None),
            _patch_resolver(),
            patch.object(mod.sentry_sdk, "get_current_scope", return_value=scope),
            patch.object(
                streaming_warm_admission, "get_cache_stats_recorder", return_value=recorder
            ),
        ):
            await _run(update, settings=_settings(service))

        keys = {c.args[0] for c in transaction.set_data.call_args_list}
        assert f"streaming_url.persistent_lookup.cache_miss_shed.{service}" in keys
        assert f"streaming_url.persistent_lookup.cache_miss_enqueued.{service}" not in keys
        recorder.record.assert_called_once_with(mod.STREAMING_WARM_DEPTH_SHED_STAT_KEY)

    async def test_dedup_hit_is_not_shed_by_the_depth_bound(self):
        # A duplicate of an ALREADY-in-flight (service, artist, album) is a
        # no-op regardless of depth -- it adds no new load, so the depth bound
        # must not treat it as a shed.
        service = "apple_music_album"
        from wxyc_etl.text import to_match_form

        key = (service, to_match_form(_ARTIST), to_match_form(_ALBUM))
        bound = warm_mod._streaming_warm_queue_depth_bound()
        for i in range(bound):
            warm_mod._streaming_warm_in_flight.add((f"dummy{i}", f"artist{i}", f"album{i}"))
        warm_mod._streaming_warm_in_flight.add(key)

        update = _blank_update()
        with _patch_cache_peek(None), _patch_resolver() as resolve:
            await _run(update, settings=_settings(service))

        # No task created (dedup short-circuits before the depth check), and
        # no shed telemetry -- this path returns True (enqueued/deduped) from
        # ``_enqueue_streaming_warm``, never reaching the shed branch.
        assert not warm_mod._background_tasks
        resolve.assert_not_awaited()


# ---------------------------------------------------------------------------
# LML#1108: the background warm arms a probe deadline (clients.streaming.base)
# around its live-probe call so a client's own retry/backoff loop can bound
# itself against the SAME wall-clock ceiling `asyncio.wait_for` will enforce.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestProbeDeadlinePropagation:
    """LML#1108 review finding 3: ``_warm_streaming_url_cache`` runs in a task
    from ``asyncio.create_task``, which COPIES the calling context -- a
    mutation the child task makes to a ``ContextVar`` (arming, then resetting,
    the probe deadline) is invisible from the PARENT context these tests
    otherwise run in. Asserting ``get_probe_deadline() is None`` from the test
    function itself after the task completes is trivially true regardless of
    whether ``reset_probe_deadline`` actually ran -- proven by mutation: it
    passes even with the ``finally: reset_probe_deadline(...)`` deleted.

    Both tests below instead observe from A POINT STILL INSIDE THE SAME WARM
    TASK, after the reset has (or hasn't) happened: ``_mint_identity`` on the
    happy path (called after the probe's ``finally`` exits, still in the same
    coroutine), and ``logger.exception`` on the raise path (same reasoning).
    """

    async def test_probe_deadline_is_armed_during_the_probe(self):
        from clients.streaming.base import get_probe_deadline

        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        observed_during_probe: list[float | None] = []

        async def observing_resolve(pg, client, *, service, artist, album, **kwargs):
            observed_during_probe.append(get_probe_deadline())
            return ResolveOutcome(url=case["resolved_url"], source="live_resolved")

        assert get_probe_deadline() is None
        with (
            _patch_cache_peek(None),
            patch.object(
                warm_mod,
                "resolve_streaming_url_with_cache",
                new=AsyncMock(side_effect=observing_resolve),
            ),
            patch.object(warm_mod, "_mint_identity", new=AsyncMock()),
        ):
            await _run(update, settings=_settings(service))
            await _drain_background_tasks()

        assert len(observed_during_probe) == 1
        # A deadline WAS active during the probe, and it's a plausible
        # near-future monotonic value (roughly now + the 4.0s Apple ceiling).
        assert observed_during_probe[0] is not None
        assert observed_during_probe[0] > time.monotonic()
        # No leak into the TEST's own (parent) context either -- this alone
        # would pass even without a working reset (see class docstring), so
        # it is a supplementary check, not the load-bearing one below.
        assert get_probe_deadline() is None

    async def test_probe_deadline_is_reset_after_the_probe_within_the_same_task(self):
        from clients.streaming.base import get_probe_deadline

        service = "apple_music_album"
        case = _CASES[service]
        update = _blank_update()
        observed_after_reset: list[float | None] = []

        async def observing_mint(*args, **kwargs):
            # Runs AFTER `_warm_streaming_url_cache`'s `finally:
            # reset_probe_deadline(...)` exits, but still inside the SAME
            # warm task -- the one in-task observation point after the reset
            # on the happy path.
            observed_after_reset.append(get_probe_deadline())

        with (
            _patch_cache_peek(None),
            _patch_resolver(
                return_value=ResolveOutcome(url=case["resolved_url"], source="live_resolved")
            ),
            patch.object(warm_mod, "_mint_identity", new=AsyncMock(side_effect=observing_mint)),
        ):
            await _run(update, settings=_settings(service))
            await _drain_background_tasks()

        # The load-bearing assertion: deleting the `finally:
        # reset_probe_deadline(...)` line makes this observe the still-armed
        # deadline instead of `None`.
        assert observed_after_reset == [None]

    async def test_probe_deadline_is_reset_even_when_the_probe_raises(self):
        from clients.streaming.base import get_probe_deadline

        service = "apple_music_album"
        update = _blank_update()
        observed_in_except_handler: list[float | None] = []

        def observing_log_exception(*args, **kwargs):
            # `_warm_streaming_url_cache`'s `except Exception:` handler runs
            # AFTER the inner `finally: reset_probe_deadline(...)` has
            # already unwound, still inside the SAME warm task.
            observed_in_except_handler.append(get_probe_deadline())

        with (
            _patch_cache_peek(None),
            _patch_resolver(side_effect=RuntimeError("boom")),
            patch.object(warm_mod.logger, "exception", side_effect=observing_log_exception),
        ):
            await _run(update, settings=_settings(service))
            await _drain_background_tasks()

        # The load-bearing assertion: deleting the reset would leave this
        # observing the still-armed deadline instead of `None`.
        assert observed_in_except_handler == [None]
        assert get_probe_deadline() is None


# ---------------------------------------------------------------------------
# Registry invariants (unchanged by #706) + per-service timeout resolution.
# ---------------------------------------------------------------------------


class TestRegistryInvariants:
    """The cache registry is a subset of the identity registry; guards keep it
    from drifting.

    LML#1037: ``STREAMING_URL_CACHE_CONFIG`` is now keyed by the
    ``StreamingService`` enum, not the bare album-cache-key string this file's
    own ``_CASES`` test data (and ``RELEASE_SOURCE_CONFIG``, out of this
    ticket's scope) still use — every comparison below maps one side through
    ``ALBUM_CACHE_KEYS`` / ``SERVICE_BY_ALBUM_CACHE_KEY`` rather than
    comparing the registry's keys directly against a string.
    """

    def test_parametrize_keys_match_cache_config(self):
        assert set(_CASES) == {ALBUM_CACHE_KEYS[s] for s in STREAMING_URL_CACHE_CONFIG}

    def test_cache_config_is_subset_of_release_source_config(self):
        assert {ALBUM_CACHE_KEYS[s] for s in STREAMING_URL_CACHE_CONFIG} <= set(
            RELEASE_SOURCE_CONFIG.keys()
        )

    def test_cache_config_matches_table_check_constraint_allowlist(self):
        from entity.streaming_url_cache import _SERVICES

        assert {ALBUM_CACHE_KEYS[s] for s in STREAMING_URL_CACHE_CONFIG} == set(_SERVICES)

    def test_cache_config_ships_apple_spotify_and_bandcamp(self):
        assert set(STREAMING_URL_CACHE_CONFIG.keys()) == {
            StreamingService.APPLE_MUSIC,
            StreamingService.SPOTIFY,
            StreamingService.BANDCAMP,
        }

    @pytest.mark.parametrize("service", list(_CASES))
    def test_cache_config_fields_match_expectations(self, service):
        case = _CASES[service]
        enum_service = _SERVICE_BY_STORAGE_KEY[service]
        cfg = STREAMING_URL_CACHE_CONFIG[enum_service]
        assert cfg.url_field == case["url_field"]
        assert enum_service.catalog_key == case["client_attr"]
        assert cfg.flag_setting == case["flag_setting"]
        assert cfg.probe_timeout_s == case["probe_timeout_s"]
        assert service in RELEASE_SOURCE_CONFIG

    def test_every_flag_setting_is_a_real_settings_attribute(self):
        from config.settings import Settings

        settings = Settings()
        for service, cfg in STREAMING_URL_CACHE_CONFIG.items():
            assert hasattr(settings, cfg.flag_setting), (
                f"{service}: flag_setting {cfg.flag_setting!r} is not a Settings attribute"
            )


class TestPerServiceTimeoutResolution:
    """The per-service wall-clock ceiling (now applied to the background probe)
    is env-overridable only for services whose registry entry carries a
    ``timeout_env_var`` (Apple, for LML#449/#450 back-compat)."""

    def test_apple_entry_carries_back_compat_env_var(self):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        assert (
            STREAMING_URL_CACHE_CONFIG[StreamingService.APPLE_MUSIC].timeout_env_var
            == APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR
        )

    def test_spotify_entry_has_no_env_var(self):
        assert STREAMING_URL_CACHE_CONFIG[StreamingService.SPOTIFY].timeout_env_var is None

    def test_bandcamp_entry_has_no_env_var(self):
        assert STREAMING_URL_CACHE_CONFIG[StreamingService.BANDCAMP].timeout_env_var is None

    def test_bandcamp_effective_timeout_is_static_nine_seconds(self, monkeypatch):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        monkeypatch.setenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, "1500")
        assert (
            warm_mod._effective_probe_timeout_s(
                STREAMING_URL_CACHE_CONFIG[StreamingService.BANDCAMP]
            )
            == 9.0
        )

    def test_apple_effective_timeout_honors_env_override(self, monkeypatch):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        monkeypatch.setenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, "1500")
        assert (
            warm_mod._effective_probe_timeout_s(
                STREAMING_URL_CACHE_CONFIG[StreamingService.APPLE_MUSIC]
            )
            == 1.5
        )

    def test_apple_effective_timeout_defaults_without_env(self, monkeypatch):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        monkeypatch.delenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, raising=False)
        assert (
            warm_mod._effective_probe_timeout_s(
                STREAMING_URL_CACHE_CONFIG[StreamingService.APPLE_MUSIC]
            )
            == 4.0
        )

    def test_spotify_effective_timeout_ignores_apple_env(self, monkeypatch):
        from lookup.timeouts import APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR

        monkeypatch.setenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, "1500")
        assert (
            warm_mod._effective_probe_timeout_s(
                STREAMING_URL_CACHE_CONFIG[StreamingService.SPOTIFY]
            )
            == 4.0
        )
