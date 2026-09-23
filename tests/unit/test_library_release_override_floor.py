"""Floor-gated override demotion (LML#1290).

``fetch_artwork_for_items`` binds a hand-verified ``library_release_override``
pin at confidence 1.0 and skips the LML#478 80/80 ARTIST_PLUS_ALBUM floor that
every non-pinned candidate must clear (LML#850). A pin audit measured 30.8% of
the 61,046 pins as unable to clear that floor, and the override exempts all of
them by construction — which is how card 28607 (Grace Jones, *Nightclubbing*, a
2002 card) came to serve a 2014 pressing.

``lml_override_requires_floor`` grades the pin against the same floor, on the
same query-side variant lists, and demotes it only when something better is
available:

===================  =========================  ====================  ==================
pin clears floor     track-validated carried    matcher clears        bind
===================  =========================  ====================  ==================
yes                  --                         --                    the pin
no                   yes                        --                    the carried release
no                   no                         yes                   the matcher
no                   no                         no                    the pin
===================  =========================  ====================  ==================

No row produces a no-match that does not already occur: absence of evidence
against a pin is never evidence against it, so every degrade keeps the pin.

The grading read is cache-only (``DiscogsCacheService.get_release_lean``) and
never enters the read-through's API leg, so no ``DiscogsBreakerOpenError`` can
arise from it -- the reason that arm was chosen over re-using the artwork
read. See ``lookup/album_level_match._rehydrate_from_local_cache`` for the
same shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config.settings import get_settings
from discogs.models import DiscogsSearchResponse, ReleaseMetadataResponse
from lookup.artwork import fetch_artwork_for_items
from lookup.release_resolution import ResolvedRelease
from tests.factories import make_discogs_result, make_library_item

_LIBRARY_ID = 42
_PIN_RELEASE_ID = 5000
_MATCHER_RELEASE_ID = 99999
_CARRIED_RELEASE_ID = 77777

# The card as the librarians filed it.
_CARD_ARTIST = "Jessica Pratt"
_CARD_TITLE = "On Your Own Love Again"


@pytest.fixture
def floor_gate_on(monkeypatch):
    """Turn on ``lml_override_requires_floor`` for one test."""
    monkeypatch.setenv("LML_OVERRIDE_REQUIRES_FLOOR", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _pin_metadata(*, artist: str, title: str) -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=_PIN_RELEASE_ID,
        title=title,
        artist=artist,
        release_url=f"https://www.discogs.com/release/{_PIN_RELEASE_ID}",
        artwork_url="https://i.discogs.com/pinned.jpg",
    )


def _service(
    *,
    pin_artist: str = _CARD_ARTIST,
    pin_title: str = _CARD_TITLE,
    matcher_clears: bool = True,
    lean: object = "default",
) -> AsyncMock:
    """A Discogs service whose pin and matcher answers are both controllable.

    ``get_release`` serves the artwork leg of whichever release ends up bound;
    ``cache_service.get_release_lean`` serves the *grading* read, and the two
    are deliberately distinct so a test can prove which one the floor consulted.
    """
    svc = AsyncMock()
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=_PIN_RELEASE_ID,
            title=pin_title,
            artist=pin_artist,
            release_url=f"https://www.discogs.com/release/{_PIN_RELEASE_ID}",
            artwork_url="https://i.discogs.com/pinned.jpg",
        )
    )
    cache = AsyncMock()
    cache.get_release_lean = AsyncMock(
        return_value=_pin_metadata(artist=pin_artist, title=pin_title)
        if lean == "default"
        else lean
    )
    svc.cache_service = cache

    # The matcher's candidate: an exact card match when it should clear, and a
    # wholly unrelated release when it should not.
    candidate = (
        make_discogs_result(
            release_id=_MATCHER_RELEASE_ID,
            album=_CARD_TITLE,
            artist=_CARD_ARTIST,
            artwork_url="https://example.com/matcher.jpg",
        )
        if matcher_clears
        else make_discogs_result(
            release_id=_MATCHER_RELEASE_ID,
            album="Entirely Different Record",
            artist="Someone Else Entirely",
        )
    )
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[candidate]))
    return svc


def _item(**kwargs):
    return make_library_item(id=_LIBRARY_ID, artist=_CARD_ARTIST, title=_CARD_TITLE, **kwargs)


async def _bind(svc, item, **kwargs):
    results = await fetch_artwork_for_items(
        [item], svc, release_overrides={_LIBRARY_ID: _PIN_RELEASE_ID}, **kwargs
    )
    return results[0][1]


@pytest.mark.asyncio
class TestDecisionTable:
    async def test_pin_clearing_the_floor_binds_the_pin(self, floor_gate_on):
        """Row 1 -- 69.2% of pins. The matcher is never consulted."""
        svc = _service()

        bound = await _bind(svc, _item())

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID
        svc.search.assert_not_awaited()

    async def test_pin_failing_the_floor_yields_to_the_matcher(self, floor_gate_on):
        """Row 3 -- the fix. The pin points at an unrelated release."""
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album")

        bound = await _bind(svc, _item())

        assert bound is not None
        assert bound.release_id == _MATCHER_RELEASE_ID
        svc.search.assert_awaited()

    async def test_pin_failing_with_no_matcher_answer_keeps_the_pin(self, floor_gate_on):
        """Row 4 -- card 929's shape: the card row itself is defective, so the
        matcher scores against the same broken strings and fails too. Dropping
        the pin here would convert a correct binding into a no-match."""
        svc = _service(
            pin_artist="Someone Else", pin_title="A Different Album", matcher_clears=False
        )

        bound = await _bind(svc, _item())

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID

    async def test_flag_off_binds_the_pin_without_grading(self):
        """No gate: byte-for-byte the LML#850 behaviour, and no grading read."""
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album")

        bound = await _bind(svc, _item())

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID
        svc.cache_service.get_release_lean.assert_not_awaited()
        svc.search.assert_not_awaited()


@pytest.mark.asyncio
class TestDegradesKeepThePin:
    """Absence of metadata is never evidence against a pin."""

    async def test_cache_service_absent(self, floor_gate_on):
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album")
        svc.cache_service = None

        bound = await _bind(svc, _item())

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID

    async def test_read_returns_none(self, floor_gate_on):
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album", lean=None)

        bound = await _bind(svc, _item())

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID

    async def test_read_returns_a_tombstone(self, floor_gate_on):
        """LML#510: a 404 tombstone carries ``title = ""`` / ``artist = ""`` as
        identifier sentinels. Reading the cache service directly bypasses the
        service boundary's tombstone->None translation, so empty strings arrive
        here intact -- and scoring them would demote a pin on the strength of a
        Discogs outage."""
        svc = _service(
            pin_artist="Someone Else",
            pin_title="A Different Album",
            lean=ReleaseMetadataResponse(
                release_id=_PIN_RELEASE_ID,
                title="",
                artist="",
                release_url=f"https://www.discogs.com/release/{_PIN_RELEASE_ID}",
                not_found=True,
            ),
        )

        bound = await _bind(svc, _item())

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID

    async def test_read_raises(self, floor_gate_on):
        """``get_release_lean``'s catch-all routes through ``_classify_cache_error``,
        which is typed ``NoReturn`` -- it always raises and never degrades to
        ``None``, so the bare except is mandatory, not stylistic."""
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album")
        svc.cache_service.get_release_lean = AsyncMock(side_effect=RuntimeError("PG down"))

        bound = await _bind(svc, _item())

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID


@pytest.mark.asyncio
class TestCarriedReleasePrecedence:
    async def test_demoted_pin_yields_to_a_track_validated_carried_release(
        self, floor_gate_on, monkeypatch
    ):
        """Row 2, and the LML#956 regression guard. A pin failing the *title*
        floor is no evidence against a release ``validate_release_for_track``
        confirmed carries the track; routing past it hands the decision back to
        the title pick that bound Plaza House's 13332759 over the validated
        605487."""
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "true")
        get_settings.cache_clear()
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album")
        carried = ResolvedRelease(
            release_id=_CARRIED_RELEASE_ID,
            release_url=f"https://www.discogs.com/release/{_CARRIED_RELEASE_ID}",
            is_compilation=False,
            album_title=_CARD_TITLE,
            track_confirmed=True,
        )

        bound = await _bind(
            svc, _item(), discogs_titles={_LIBRARY_ID: carried}, found_on_compilation=True
        )

        assert bound is not None
        assert bound.release_id == _CARRIED_RELEASE_ID

    async def test_demoted_pin_bypasses_a_carried_release_with_no_track_validation(
        self, floor_gate_on, monkeypatch
    ):
        """The carried release is an album-ranked carry-through, not a track
        confirmation, so it has no claim over the floored matcher. Without the
        bypass the demoted pin would be replaced by *another* unfloored
        confidence-1.0 bind rather than by a floored result."""
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "true")
        get_settings.cache_clear()
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album")
        carried = ResolvedRelease(
            release_id=_CARRIED_RELEASE_ID,
            release_url=f"https://www.discogs.com/release/{_CARRIED_RELEASE_ID}",
            is_compilation=False,
            album_title=_CARD_TITLE,
        )

        bound = await _bind(svc, _item(), discogs_titles={_LIBRARY_ID: carried})

        assert bound is not None
        assert bound.release_id == _MATCHER_RELEASE_ID


@pytest.mark.asyncio
class TestValidatedOverridesAreExempt:
    async def test_same_request_derived_pins_skip_the_floor(self, floor_gate_on):
        """The LML#1332 shelf-rebind probe exists *to* bypass the 80/80 title
        floor (51/100 for the Broadcast pair) and returns its result through
        the same ``release_overrides`` channel. Re-imposing the floor on it
        would land on the release_id=0 sentinel with no artwork or tracklist."""
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album")

        bound = await _bind(svc, _item(), release_overrides_validated=True)

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID
        svc.cache_service.get_release_lean.assert_not_awaited()


@pytest.mark.asyncio
class TestFloorSymmetry:
    async def test_pin_and_matcher_are_scored_on_the_same_variant_lists(
        self, floor_gate_on, monkeypatch
    ):
        """Widening the card-side variants for one side only would bias the
        gate systematically. Both sides must reach the floor through the same
        call with the same list objects."""
        seen: list[tuple[int, object, object]] = []
        import lookup.artwork as artwork_module

        original = artwork_module._floor_candidates

        def spy(candidates, *, artist_variants, album_variants):
            seen.append((len(list(candidates)), artist_variants, album_variants))
            return original(
                candidates, artist_variants=artist_variants, album_variants=album_variants
            )

        monkeypatch.setattr(artwork_module, "_floor_candidates", spy)
        svc = _service(pin_artist="Someone Else", pin_title="A Different Album")

        await _bind(svc, _item())

        assert len(seen) == 2, "expected one grading call and one matcher call"
        (_, pin_artists, pin_albums), (_, matcher_artists, matcher_albums) = seen
        assert pin_artists is matcher_artists
        assert pin_albums is matcher_albums


@pytest.mark.asyncio
class TestCoFiledCard:
    async def test_real_artist_only_in_alternate_name_keeps_the_pin(self, floor_gate_on):
        """A card whose real artist appears in no scored field fails the floor --
        but so does the matcher, on the same variants, so row 4 retains the pin.
        The safety property is the decision table's, not the variant set's."""
        svc = _service(
            pin_artist="Someone Else", pin_title="A Different Album", matcher_clears=False
        )

        bound = await _bind(svc, _item(alternate_artist_name="A Co-Filed Band"))

        assert bound is not None
        assert bound.release_id == _PIN_RELEASE_ID


class TestExemptionInvariant:
    """The exemption is a single boolean, and this is what makes that sound.

    ``fetch_artwork_for_items`` receives one ``release_overrides_validated``
    flag for the whole map rather than a per-id provenance tag. That is only
    correct because the map is all-or-nothing: ``Step3bResult`` narrows
    ``library_results`` to the single shelf row it pinned, so
    ``_prefetch_release_overrides`` always takes its full-coverage lane and
    returns those pins verbatim instead of re-reading the catalog table.

    Asserted over the AST rather than by driving the pipeline, because the
    claim is about *every* producer -- including one a future change adds. A
    behavioural test can only cover the producers that exist today.
    """

    def test_step3b_release_overrides_imply_a_single_shelf_row(self):
        import ast
        import pathlib

        source = pathlib.Path("lookup/validation.py").read_text()
        tree = ast.parse(source)

        producers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Step3bResult"
            and any(kw.arg == "release_overrides" for kw in node.keywords)
        ]
        assert producers, "no Step3bResult(..., release_overrides=...) found — did it move?"

        for node in producers:
            results_arg = node.args[0] if node.args else None
            assert isinstance(results_arg, ast.List) and len(results_arg.elts) == 1, (
                f"lookup/validation.py:{node.lineno} returns release_overrides with "
                "library_results that is not a single-row list. The all-or-nothing "
                "property behind `release_overrides_validated` no longer holds; the "
                "exemption must become a tagged map before this widens."
            )
