"""Unit tests for the cache-refresh dispatcher (LML#525).

The dispatcher takes a single ``identity_id``'s ``(source, external_id)`` set
and orchestrates the per-source release refresh + walk-to-artists fan-out. The
pure-logic pieces (sentinel guard, per-id ``status`` rollup) live alongside it
so they can be tested without spinning up the FastAPI app.

The router-level tests (batching, semaphore, client-disconnect) live in
``tests/integration/test_cache_refresh_for_identities.py``.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from cache.dispatch import (
    compute_per_id_status,
    extract_discogs_artist_ids,
    refresh_identity,
)
from cache.models import (
    ArtistRefreshOutcome,
    CacheRefreshResultItem,
    SourceRefreshOutcome,
)
from discogs.breaker import DiscogsBreakerOpenError
from discogs.models import ArtistCredit, ArtistDetails, ReleaseMetadataResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _release(
    release_id: int = 12345,
    artists: list[ArtistCredit] | None = None,
) -> ReleaseMetadataResponse:
    """Construct a minimal release record with the supplied artist credits.

    The remaining fields (year, label, tracklist, …) are left at their
    defaults; the dispatcher only reads ``artists``. ``artists=None`` (the
    Pythonic default) seeds a one-credit list so most tests can omit the
    kwarg; ``artists=[]`` explicitly tests the empty case (the ``is None``
    check below disambiguates).
    """
    if artists is None:
        artists = [ArtistCredit(artist_id=42, name="Juana Molina")]
    return ReleaseMetadataResponse(
        release_id=release_id,
        title="DOGA",
        artist="Juana Molina",
        release_url=f"https://www.discogs.com/release/{release_id}",
        artists=artists,
    )


def _artist_details(artist_id: int = 42, name: str = "Juana Molina") -> ArtistDetails:
    return ArtistDetails(artist_id=artist_id, name=name)


# ---------------------------------------------------------------------------
# extract_discogs_artist_ids — sentinel guard (LML#525's walk-to-artist site)
# ---------------------------------------------------------------------------


class TestExtractDiscogsArtistIds:
    """Walk ``release.artists`` and emit the artist_ids worth refreshing.

    The id=0 / negative / None guard is the LML#525-specific contribution to
    the LML#518 / LML#546 caller-validates audit posture. Without it, the
    dispatcher becomes a fresh unguarded caller of ``get_artist_details(0)``
    every time it walks a compilation release whose "Various Artists" credit
    carries ``artist_id = 0``.
    """

    def test_no_release_returns_empty(self):
        assert extract_discogs_artist_ids(None) == []

    def test_empty_artists_returns_empty(self):
        assert extract_discogs_artist_ids(_release(artists=[])) == []

    def test_single_valid_credit_returns_it(self):
        rel = _release(artists=[ArtistCredit(artist_id=42, name="Juana Molina")])
        assert extract_discogs_artist_ids(rel) == [42]

    def test_skips_artist_id_zero(self):
        """The Discogs 'Various Artists' sentinel must never reach get_artist_details."""
        rel = _release(
            artists=[
                ArtistCredit(artist_id=0, name="Various Artists"),
                ArtistCredit(artist_id=42, name="Juana Molina"),
            ],
        )
        assert extract_discogs_artist_ids(rel) == [42]

    def test_skips_none_artist_id(self):
        """A missing artist_id in the API response must not become a None deref."""
        rel = _release(
            artists=[
                ArtistCredit(artist_id=None, name="Unknown"),
                ArtistCredit(artist_id=42, name="Juana Molina"),
            ],
        )
        assert extract_discogs_artist_ids(rel) == [42]

    def test_skips_negative_artist_id(self):
        rel = _release(
            artists=[
                ArtistCredit(artist_id=-1, name="???"),
                ArtistCredit(artist_id=42, name="Juana Molina"),
            ],
        )
        assert extract_discogs_artist_ids(rel) == [42]

    def test_returns_multiple_valid_credits_in_order(self):
        rel = _release(
            artists=[
                ArtistCredit(artist_id=42, name="Juana Molina"),
                ArtistCredit(artist_id=99, name="Sessa"),
            ],
        )
        assert extract_discogs_artist_ids(rel) == [42, 99]


# ---------------------------------------------------------------------------
# compute_per_id_status — rollup rules (issue's priority order)
# ---------------------------------------------------------------------------


class TestComputePerIdStatus:
    """Translate per-source outcomes into the per-id four-value enum.

    Priority order from LML#525:

    1. No row in entity.release_identity → ``not_found`` (handled before
       compute_per_id_status is called; tested at the router level).
    2. Any source ``release_outcome == "success"`` → ``warmed`` (regardless
       of nested artist outcomes — issue's release-leg-gated rule).
    3. Any source ``release_outcome == "not_implemented"`` and no success
       → ``not_implemented``.
    4. All dispatched sources errored → ``error``.
    """

    def test_any_success_rolls_up_to_warmed(self):
        sources = {
            "discogs_release": SourceRefreshOutcome(release_outcome="success"),
            "discogs_master": SourceRefreshOutcome(release_outcome="not_implemented"),
        }
        assert compute_per_id_status(sources) == "warmed"

    def test_artist_failures_do_not_demote_warmed(self):
        """Issue rule: artist 404 inside a release walk is partial value, not retry trigger."""
        sources = {
            "discogs_release": SourceRefreshOutcome(
                release_outcome="success",
                artists=[
                    ArtistRefreshOutcome(external_id="42", outcome="error", message="ConnectError")
                ],
            ),
        }
        assert compute_per_id_status(sources) == "warmed"

    def test_only_not_implemented_rolls_up(self):
        sources = {
            "discogs_master": SourceRefreshOutcome(release_outcome="not_implemented"),
            "musicbrainz_release": SourceRefreshOutcome(release_outcome="not_implemented"),
        }
        assert compute_per_id_status(sources) == "not_implemented"

    def test_all_error_rolls_up_to_error(self):
        sources = {
            "discogs_release": SourceRefreshOutcome(
                release_outcome="error", message="ConnectError"
            ),
        }
        assert compute_per_id_status(sources) == "error"

    def test_mix_of_error_and_not_implemented_picks_not_implemented(self):
        """Any not_implemented over all-error keeps the dashboard signal legible."""
        sources = {
            "discogs_master": SourceRefreshOutcome(release_outcome="not_implemented"),
            "discogs_release": SourceRefreshOutcome(release_outcome="error"),
        }
        assert compute_per_id_status(sources) == "not_implemented"

    def test_empty_sources_rolls_up_to_not_implemented(self):
        """A row whose per-source columns are all NULL — legally possible — has
        no dispatched legs, so there's no success and no error to roll up.
        Treating it as not_implemented matches the "no leg ran" semantics."""
        assert compute_per_id_status({}) == "not_implemented"

    def test_breaker_shed_error_rolls_up_to_retriable_error(self):
        """LML#814: a saturation-breaker shed is recorded as the ``error``
        outcome (tagged ``message="DiscogsBreakerOpenError"``), so with no warmed
        leg it rolls up to the retriable ``error`` bucket — the cron retries the
        identity on its next pass rather than treating it as done."""
        sources = {
            "discogs_release": SourceRefreshOutcome(
                release_outcome="error", message="DiscogsBreakerOpenError"
            ),
        }
        assert compute_per_id_status(sources) == "error"


# ---------------------------------------------------------------------------
# refresh_identity — orchestration; AsyncMock-driven DiscogsService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRefreshIdentity:
    """End-to-end dispatch for a single identity_id."""

    async def test_discogs_release_happy_path_warms_release_and_walks_artists(self):
        rel = _release(
            artists=[
                ArtistCredit(artist_id=42, name="Juana Molina"),
                ArtistCredit(artist_id=99, name="Sessa"),
            ]
        )
        discogs = AsyncMock()
        discogs.get_release = AsyncMock(return_value=rel)
        discogs.get_artist_details = AsyncMock(
            side_effect=[_artist_details(42), _artist_details(99)]
        )

        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("discogs_release", "12345")],
            discogs_service=discogs,
        )

        assert isinstance(result, CacheRefreshResultItem)
        assert result.identity_id == 1
        assert result.status == "warmed"
        assert result.sources is not None
        assert result.sources["discogs_release"].release_outcome == "success"
        artist_outcomes = result.sources["discogs_release"].artists
        assert {a.external_id for a in artist_outcomes} == {"42", "99"}
        assert all(a.outcome == "success" for a in artist_outcomes)
        discogs.get_release.assert_awaited_once_with(12345)
        # Both walk-targets dispatched.
        artist_ids_called = {c.args[0] for c in discogs.get_artist_details.await_args_list}
        assert artist_ids_called == {42, 99}

    async def test_walk_to_artist_skips_id_zero_sentinel(self):
        """LML#525 walk-to-artist guard: VA compilation must not refresh artist_id=0."""
        rel = _release(
            artists=[
                ArtistCredit(artist_id=0, name="Various Artists"),
                ArtistCredit(artist_id=42, name="Juana Molina"),
            ]
        )
        discogs = AsyncMock()
        discogs.get_release = AsyncMock(return_value=rel)
        discogs.get_artist_details = AsyncMock(return_value=_artist_details(42))

        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("discogs_release", "12345")],
            discogs_service=discogs,
        )

        assert result.status == "warmed"
        artist_outcomes = result.sources["discogs_release"].artists
        assert [a.external_id for a in artist_outcomes] == ["42"]
        # Critically: get_artist_details was NEVER called with 0.
        artist_ids_called = [c.args[0] for c in discogs.get_artist_details.await_args_list]
        assert artist_ids_called == [42]
        assert 0 not in artist_ids_called

    async def test_discogs_release_tombstone_counts_as_success(self):
        """Public boundary returns None for a tombstone — that's still 'cache is current'."""
        discogs = AsyncMock()
        discogs.get_release = AsyncMock(return_value=None)
        discogs.get_artist_details = AsyncMock()

        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("discogs_release", "12345")],
            discogs_service=discogs,
        )

        assert result.status == "warmed"
        assert result.sources["discogs_release"].release_outcome == "success"
        assert result.sources["discogs_release"].artists == []
        # No artists to walk → get_artist_details never called.
        discogs.get_artist_details.assert_not_awaited()

    async def test_discogs_master_only_returns_not_implemented(self):
        discogs = AsyncMock()
        discogs.get_release = AsyncMock()

        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("discogs_master", "789")],
            discogs_service=discogs,
        )

        assert result.status == "not_implemented"
        assert result.sources["discogs_master"].release_outcome == "not_implemented"
        discogs.get_release.assert_not_awaited()

    async def test_dual_populated_release_and_master_warms_via_release_leg(self):
        rel = _release(artists=[ArtistCredit(artist_id=42, name="Juana Molina")])
        discogs = AsyncMock()
        discogs.get_release = AsyncMock(return_value=rel)
        discogs.get_artist_details = AsyncMock(return_value=_artist_details(42))

        result = await refresh_identity(
            identity_id=1,
            source_pairs=[
                ("discogs_release", "12345"),
                ("discogs_master", "789"),
            ],
            discogs_service=discogs,
        )

        assert result.status == "warmed"
        assert result.sources["discogs_release"].release_outcome == "success"
        assert result.sources["discogs_master"].release_outcome == "not_implemented"

    async def test_release_leg_exception_records_error_outcome(self):
        discogs = AsyncMock()
        discogs.get_release = AsyncMock(side_effect=RuntimeError("boom"))
        discogs.get_artist_details = AsyncMock()

        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("discogs_release", "12345")],
            discogs_service=discogs,
        )

        assert result.status == "error"
        assert result.sources["discogs_release"].release_outcome == "error"
        # Exception class name surfaced in message, not the bare str() which
        # could include SQL fragments or upstream error bodies.
        assert result.sources["discogs_release"].message == "RuntimeError"
        # No artists walked when release leg errored.
        discogs.get_artist_details.assert_not_awaited()

    async def test_release_leg_breaker_shed_records_error_without_error_log(self, caplog):
        """LML#814: ``get_release`` re-raises a saturation-breaker shed (LML#755
        FIX-1). The cache-warmer must treat that shed as expected degrade — NOT
        route it through the generic ``except`` that logs at ERROR via
        ``logger.exception`` (the Sentry LoggingIntegration promotes each such
        record to a discrete event, reproducing the #755 flood on the
        backfill-cron-driven refresh path).

        The shed must: emit no ERROR-level record; record the retriable ``error``
        outcome (not a tombstone-as-``success`` — the cache is not warm), tagged
        ``message="DiscogsBreakerOpenError"`` so a dashboard can tell it apart
        from a hard leg failure; and walk no artists. ``error`` (rather than a
        new ``skipped`` value) keeps ``release_outcome`` in lockstep with the
        api.yaml ``CacheRefreshSourceOutcome`` wire enum BS also generates from.
        """
        discogs = AsyncMock()
        discogs.get_release = AsyncMock(
            side_effect=DiscogsBreakerOpenError("Discogs saturation breaker open")
        )
        discogs.get_artist_details = AsyncMock()

        with caplog.at_level(logging.DEBUG):
            result = await refresh_identity(
                identity_id=1,
                source_pairs=[("discogs_release", "12345")],
                discogs_service=discogs,
            )

        # A shed rolls up to the retriable ``error`` bucket so the cron retries.
        assert result.status == "error"
        assert result.sources["discogs_release"].release_outcome == "error"
        # The breaker exception name distinguishes a shed from a hard leg failure
        # (e.g. the generic path's "RuntimeError") without a new wire value.
        assert result.sources["discogs_release"].message == "DiscogsBreakerOpenError"
        # Not a tombstone-as-success: the cache is NOT warm after a shed.
        assert result.sources["discogs_release"].release_outcome != "success"
        # No artists walked — the shed returned no release record.
        discogs.get_artist_details.assert_not_awaited()
        # The heart of #814: the shed produced zero ERROR-level records (which
        # would each become a Sentry event under event_level=ERROR).
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], f"breaker shed emitted ERROR record(s): {error_records}"
        # It IS logged, at DEBUG, so the shed is still observable in local logs.
        assert any(
            r.levelno == logging.DEBUG and "breaker" in r.getMessage().lower()
            for r in caplog.records
        )

    async def test_artist_leg_exception_does_not_demote_release_warmed(self):
        """Issue rule: artist 404 inside a release walk does NOT promote per-id to error."""
        rel = _release(artists=[ArtistCredit(artist_id=42, name="Juana Molina")])
        discogs = AsyncMock()
        discogs.get_release = AsyncMock(return_value=rel)
        discogs.get_artist_details = AsyncMock(side_effect=RuntimeError("boom"))

        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("discogs_release", "12345")],
            discogs_service=discogs,
        )

        assert result.status == "warmed"
        artist_outcomes = result.sources["discogs_release"].artists
        assert len(artist_outcomes) == 1
        assert artist_outcomes[0].outcome == "error"
        assert artist_outcomes[0].external_id == "42"

    async def test_empty_source_pairs_rolls_up_to_not_implemented(self):
        """A row with no per-source columns populated still emits a result."""
        discogs = AsyncMock()
        result = await refresh_identity(identity_id=1, source_pairs=[], discogs_service=discogs)
        assert result.status == "not_implemented"
        assert result.sources == {}

    @pytest.mark.parametrize(
        "source",
        [
            "musicbrainz_release",
            "spotify_album",
            "apple_music_album",
            "bandcamp",
        ],
    )
    async def test_unwired_sources_return_not_implemented(self, source):
        discogs = AsyncMock()
        result = await refresh_identity(
            identity_id=1,
            source_pairs=[(source, "some-external-id")],
            discogs_service=discogs,
        )
        assert result.status == "not_implemented"
        assert result.sources[source].release_outcome == "not_implemented"

    # ----------------------------------------------------------------------
    # LML#589 — signature-prep commit
    #
    # The four cache legs (musicbrainz_release / spotify_album /
    # apple_music_album / bandcamp) each get their own implementation issue
    # (LML#547, #548, #549, #550) blocked on #589. This prep commit extends
    # the dispatcher signature now so all four leg PRs can be developed in
    # parallel without colliding on signature edits, while preserving the
    # existing "not_implemented" behavior when the corresponding client is
    # None (the dependency-injection short-circuit for missing creds).
    # ----------------------------------------------------------------------

    async def test_musicbrainz_release_still_not_implemented_when_mb_pg_is_none(self):
        discogs = AsyncMock()
        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("musicbrainz_release", "ext-id")],
            discogs_service=discogs,
            mb_pg=None,
        )
        assert result.status == "not_implemented"
        assert result.sources["musicbrainz_release"].release_outcome == "not_implemented"

    async def test_spotify_album_still_not_implemented_when_spotify_client_is_none(self):
        discogs = AsyncMock()
        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("spotify_album", "ext-id")],
            discogs_service=discogs,
            spotify_client=None,
        )
        assert result.status == "not_implemented"
        assert result.sources["spotify_album"].release_outcome == "not_implemented"

    async def test_apple_music_album_still_not_implemented_when_apple_music_client_is_none(self):
        discogs = AsyncMock()
        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("apple_music_album", "ext-id")],
            discogs_service=discogs,
            apple_music_client=None,
        )
        assert result.status == "not_implemented"
        assert result.sources["apple_music_album"].release_outcome == "not_implemented"

    async def test_bandcamp_still_not_implemented_when_bandcamp_client_is_none(self):
        discogs = AsyncMock()
        result = await refresh_identity(
            identity_id=1,
            source_pairs=[("bandcamp", "ext-id")],
            discogs_service=discogs,
            bandcamp_client=None,
        )
        assert result.status == "not_implemented"
        assert result.sources["bandcamp"].release_outcome == "not_implemented"
