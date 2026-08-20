"""Loader, fake upstreams, and verdict vocabulary for the LML#1233 golden corpus.

Three checked-in files make a corpus case self-contained, and nothing else is
read at run time -- no `library.db` (gitignored, absent in CI), no PostgreSQL,
no network:

- `library.json` -- real WXYC catalog rows, verbatim from production
  `library.db` (see `scripts/build_golden_corpus.py`). Seeded into an on-disk
  SQLite built with the repo's own `LIBRARY_FTS_CREATE_SQL`, then opened
  through the real `LibraryDB.connect()` so column detection, the FTS
  tokenizer, and the LIKE/fuzzy fallback chain are all the production ones.
- `discogs.json` -- the Discogs universe: releases with tracklists, plus
  routing tables saying which release ids each search returns.
- `cases.json` -- the queries and their recorded verdicts.

**What is faked and what is not.** :class:`FakeDiscogsService` fakes exactly
the I/O boundary -- the methods that would otherwise reach the Discogs HTTP
API or the discogs-cache PostgreSQL -- and nothing above it. In particular
`validate_track_on_release` delegates to the real
`discogs.service._scan_tracklist_for_match`, so `discogs/matching.py`'s
title/coverage/artist gates run for real against fixture tracklists. That is
deliberate and load-bearing: LML#1225 lives in exactly that kernel, and a fake
that answered validation questions itself would test the fake.

The routing tables are the honest model of the other half. A Discogs search --
whether served by the API's keyword index or the cache's pg_trgm scan -- is a
*candidate generator*, and being loose is its job (LML#1225 root cause 3). The
fixture states which candidates came back; the code under test decides what to
do with them.

**Determinism.** `library/db.py` holds two module-level TTLCaches keyed only on
query text, so a case's verdict could otherwise depend on which case (or which
other e2e module) ran before it in the same xdist worker. The fixtures clear
them per case. Verdicts must be order-independent or the corpus reports a
different answer under `-p no:randomly` than under `-n auto`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wxyc_etl.schema import library_columns

from core.dependencies import _DISCOGS_POOL_MAX_SIZE_DEFAULT, _DISCOGS_POOL_MAX_SIZE_ENV_VAR
from core.search import (
    DEFAULT_SEARCH_BUDGET_MS,
    DEFAULT_SEARCH_HARD_TIMEOUT_MS,
    SEARCH_BUDGET_ENV_VAR,
    SEARCH_HARD_TIMEOUT_ENV_VAR,
)
from discogs.models import (
    ArtistDetails,
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    DiscogsSearchResult,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)
from library.db import LIBRARY_FTS_CREATE_SQL
from lookup.admission import (
    ADMISSION_LOOP_LAG_SHED_ENV_VAR,
    ADMISSION_SHED_ENFORCE_ENV_VAR,
    DEFAULT_ADMISSION_LOOP_LAG_SHED_MS,
)
from lookup.matching import MAX_SEARCH_RESULTS
from lookup.miss_kind import MISS_CLEAN, MISS_DEGRADED, MISS_TIMEOUT, OUTCOME_HIT
from lookup.timeouts import (
    _APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S,
    _BANDCAMP_LIVE_PROBE_TIMEOUT_DEFAULT_S,
    APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
    BANDCAMP_LIVE_PROBE_TIMEOUT_ENV_VAR,
)

GOLDEN_DIR = Path(__file__).resolve().parent
LIBRARY_PATH = GOLDEN_DIR / "library.json"
DISCOGS_PATH = GOLDEN_DIR / "discogs.json"
CASES_PATH = GOLDEN_DIR / "cases.json"

KNOWN_MISS_KINDS = frozenset({OUTCOME_HIT, MISS_CLEAN, MISS_TIMEOUT, MISS_DEGRADED})

#: Floor on total cases. Not a target -- a floor, so a corpus gutted by a bad
#: merge or an over-eager rebaseline fails instead of reporting green.
MIN_CASES = 120

#: Per-shape floors, proportional to the production query-shape mix measured
#: over 21 days of alert-scope `lookup_completed` (PostHog 551103,
#: `environment='production' AND endpoint_family='lookup' AND low_priority=false`,
#: excluding `library-enrich-artwork`; 5,413 requests, pulled 2026-08-20):
#:
#:   artist+album        4,115 (76.1%)   13.0% zero-result
#:   artist+album+song     971 (17.9%)    2.2%
#:   artist+song           207 ( 3.8%)    0.0%
#:   artist only           104 ( 1.9%)   24.0%
#:   song only              15 ( 0.3%)   73.3%
#:   album only              1 ( 0.0%)  100.0%
#:
#: The small shapes are deliberately over-represented against that mix. Three
#: of the four regressions this tier is calibrated against (LML#801, #1184,
#: #1225) live in the artist+song and song-only lanes, which together are 4%
#: of traffic; sampling them proportionally would put one or two cases on the
#: code most likely to break.
SHAPE_FLOORS: dict[str, int] = {
    "artist_album": 60,
    "artist_album_song": 16,
    "artist_song": 12,
    "artist_only": 10,
    "song_only": 6,
}

#: `/lookup` truncates to `lookup.matching.MAX_SEARCH_RESULTS`. Imported rather
#: than restated: a hand-copied literal here would drift silently the moment
#: the production cap changed, which is exactly the regression this guard
#: exists to catch (LML#1233 review -- the two values had already drifted,
#: 8 vs 5). Recording more than the cap would mean it moved; the guard test
#: says so rather than letting a case quietly grow a 50-line expectation.
MAX_RECORDED_RESULTS = MAX_SEARCH_RESULTS

#: Marker on a recorded result that has no WXYC catalog row behind it -- the
#: LML#628/#631 row-less shape, `LibraryItem(id=0)`. Kept visible in the
#: recorded identity because "we found it, but you cannot pull it off the
#: shelf" is a different answer from a shelf hit, and LML#1184 is precisely a
#: case of one silently replacing the other.
ROWLESS_MARKER = "row-less: "


#: Environment pins applied around every case, on top of the per-field
#: defaults derived below. Settings are read through `config.settings`'s
#: `lru_cache`d `get_settings()`, which most of the pipeline calls *directly*
#: rather than through FastAPI's `Depends` -- so a `dependency_overrides` entry
#: reaches the router and nothing under it. Env vars plus a `cache_clear()`
#: reach every call site -- but only for `Settings` fields. A second class of
#: knob bypasses `Settings` entirely and is read straight off `os.environ` at
#: call time (`core.search.resolve_positive_int_env` and friends): the search
#: budget/hard-timeout, the admission loop-lag shed, the lookup in-flight cap's
#: pool size, and the Apple/Bandcamp per-probe timeouts. A corpus that pinned
#: only `Settings` was NOT immune to a developer's `.env` moving a verdict --
#: `LML_SEARCH_BUDGET_MS=1` starves the strategy cascade and flips
#: `lml1184-arabian-prince-strange-life` from hit to a clean miss (LML#1233
#: review). These are pinned explicitly below, each to the literal default its
#: own module declares, so this list can drift only if someone edits it by
#: hand -- never silently, by a knob moving without the corresponding pin
#: moving too.
_PINNED_ENV: dict[str, str] = {
    "DISCOGS_TOKEN": "",
    "DISCOGS_API_KEY": "",
    "DISCOGS_API_SECRET": "",
    "DATABASE_URL_DISCOGS": "",
    "POSTHOG_API_KEY": "",
    "SENTRY_DSN": "",
    # Not a default (it ships True): telemetry is pinned off so a corpus run
    # can never emit a `lookup_completed` event into a real project.
    "ENABLE_TELEMETRY": "false",
    # -- non-Settings knobs read straight from os.environ (see the docstring
    # above). Each value is the literal default the owning module declares,
    # imported rather than restated so this dict can't drift from it silently.
    SEARCH_BUDGET_ENV_VAR: str(DEFAULT_SEARCH_BUDGET_MS),
    SEARCH_HARD_TIMEOUT_ENV_VAR: str(DEFAULT_SEARCH_HARD_TIMEOUT_MS),
    ADMISSION_LOOP_LAG_SHED_ENV_VAR: str(DEFAULT_ADMISSION_LOOP_LAG_SHED_MS),
    ADMISSION_SHED_ENFORCE_ENV_VAR: "false",
    # Not `LML_LOOKUP_MAX_CONCURRENT` itself: its declared default is computed
    # (`min(ceiling, pool_max)`, `lookup/router.py`), not a literal, so pinning
    # a number here could silently exceed the pool and log a spurious warning
    # if the pool default ever changed independently. Pinning the pool size
    # its computation depends on makes the *result* deterministic without
    # hand-picking a number that has to be kept in sync with two modules.
    _DISCOGS_POOL_MAX_SIZE_ENV_VAR: str(_DISCOGS_POOL_MAX_SIZE_DEFAULT),
    APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR: str(round(_APPLE_MUSIC_LOOKUP_TIMEOUT_DEFAULT_S * 1000)),
    BANDCAMP_LIVE_PROBE_TIMEOUT_ENV_VAR: str(round(_BANDCAMP_LIVE_PROBE_TIMEOUT_DEFAULT_S * 1000)),
}


def pinned_environment(overrides: dict[str, Any] | None = None) -> dict[str, str]:
    """The full environment a corpus case runs under.

    Every boolean `Settings` field is pinned to its declared default, derived
    from the model rather than listed by hand -- a flag added to `Settings`
    joins the pin automatically instead of quietly entering the corpus with
    whatever the host's environment says. A case's own `settings` block then
    overrides individual entries, which is how a case that only means anything
    with a flag on (LML#1184) states that dependency in the reviewed diff.
    """
    from config.settings import Settings

    env = {
        name.upper(): ("true" if field.default else "false")
        for name, field in Settings.model_fields.items()
        if field.annotation is bool
    }
    env.update(_PINNED_ENV)
    for name, value in (overrides or {}).items():
        if name not in Settings.model_fields:
            raise KeyError(f"unknown Settings field in a case's settings block: {name!r}")
        env[name.upper()] = "true" if value is True else "false" if value is False else str(value)
    return env


@dataclass(frozen=True)
class Verdict:
    """What a case asserts. Deliberately narrower than the response.

    Excluded on purpose: artwork, streaming URLs, enrichment, identities,
    timings, `search_type`, and `context_message`. `search_type` is excluded
    because it is a lane-derived label with no reference to what was found
    (LML#1233 Finding 1) -- pinning it would fail on a strategy-ordering
    refactor that changed no answer. The rest are excluded because a corpus
    that fails on an unrelated enrichment change teaches its readers to
    rebaseline without looking, which is the failure mode that makes a corpus
    worthless.

    Included on purpose: `miss_kind` is the shared Layer 1/2 vocabulary, and
    the ordered result identities are what three of the four calibration
    regressions actually moved -- LML#801 (right rows, wrong five), LML#717
    (a wrong-artist row appearing at all), LML#1184 (real rows silently
    replaced by a row-less one). `song_not_found` and `found_on_compilation`
    are included because a caller reads them to decide whether to caveat
    (LML#1225), so a flip there is a user-visible change even when the rows
    are identical.
    """

    miss_kind: str
    song_not_found: bool
    found_on_compilation: bool
    results: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "miss_kind": self.miss_kind,
            "song_not_found": self.song_not_found,
            "found_on_compilation": self.found_on_compilation,
            "results": list(self.results),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Verdict:
        return cls(
            miss_kind=raw["miss_kind"],
            song_not_found=bool(raw["song_not_found"]),
            found_on_compilation=bool(raw["found_on_compilation"]),
            results=tuple(raw.get("results") or ()),
        )


@dataclass(frozen=True)
class Case:
    """One frozen query plus the verdict it produced when it was recorded."""

    id: str
    shape: str
    note: str
    query: dict[str, str]
    #: `None` only between `scripts/build_golden_corpus.py` emitting a new case
    #: skeleton and `scripts/rebaseline_golden_corpus.py` recording its verdict.
    #: A committed corpus never carries one -- `test_every_case_has_an_expectation`
    #: is the guard.
    expect: Verdict | None
    frozen: bool = False
    issue: str = ""
    #: The recorded verdict is believed to be WRONG, and is pinned anyway.
    #: A corpus records behavior, not correctness, and those are not always the
    #: same thing -- see `docs/testing.md`. Marking a case suspect keeps the
    #: judgement attached to the data instead of living in someone's memory,
    #: and makes the day the verdict moves read as "the fix landed" rather than
    #: "something broke".
    suspect: bool = False
    requires_discogs: bool = False
    #: Exact fixture routes (`"track:<track>|<artist>"`, `"search:<artist>|<album>"`,
    #: `"album_title:<album>"`) that must have served a candidate during the run.
    #: The precise form of `requires_discogs`, for cases where a *specific*
    #: upstream answer is the point. LML#801's healthy-cache case is reachable
    #: two ways -- the track route, and per-row validation over the shelf
    #: routes -- so `requires_discogs` alone cannot tell that its track route
    #: drifted; naming the route can.
    requires_routes: tuple[str, ...] = ()
    requires_rows: tuple[tuple[str, str], ...] = ()
    requires_absent_artists: tuple[str, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict)

    def request_body(self) -> dict[str, Any]:
        """The `/api/v1/lookup` payload for this case.

        `raw_message` is synthesized rather than stored, but it is NOT read
        for logging alone: `core.search.detect_ambiguous_format` parses it to
        decide whether SWAPPED_INTERPRETATION should retry an "X - Y" / "X, Y"
        / "X. Y" reading, and `TRACK_ON_COMPILATION`
        (`lookup/strategies/track_on_compilation.py`) greps it for a
        parenthetical remix/mix/version/edit tag. A synthesized value can
        therefore change which strategy runs, not just what gets logged.

        The one raw_message shape this corpus can honestly call
        production-realistic is the two-field one CLAUDE.md documents
        (`"Jessica Pratt - On Your Own Love Again"`): a single dash between
        exactly the two typed fields. Joining a THIRD field the same way --
        `"Artist - Album - Song"` -- does not extend that shape, it fabricates
        one: `detect_ambiguous_format`'s dash pattern is non-greedy and splits
        at the FIRST dash, so SWAPPED_INTERPRETATION would read that string as
        `("Artist", "Album - Song")`, a split no real raw message ever
        produces for a 3-field query. There is no established production
        mapping from three structured fields back to one raw message to
        synthesize instead (unlike the two-field case, nothing here says what
        a DJ would have typed), so one- and three-field queries get a
        space-joined `raw_message` that never trips `detect_ambiguous_format`
        at all, rather than a fabricated shape that might.
        """
        body: dict[str, Any] = {k: v for k, v in self.query.items() if v}
        present = [
            v
            for v in (self.query.get("artist"), self.query.get("album"), self.query.get("song"))
            if v
        ]
        body["raw_message"] = " - ".join(present) if len(present) == 2 else " ".join(present)
        return body

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "shape": self.shape,
            "frozen": self.frozen,
        }
        if self.issue:
            out["issue"] = self.issue
        if self.suspect:
            out["suspect"] = True
        out["note"] = self.note
        out["query"] = dict(self.query)
        if self.settings:
            out["settings"] = dict(self.settings)
        if self.requires_discogs:
            out["requires_discogs"] = True
        if self.requires_routes:
            out["requires_routes"] = list(self.requires_routes)
        if self.requires_rows:
            out["requires_rows"] = [list(pair) for pair in self.requires_rows]
        if self.requires_absent_artists:
            out["requires_absent_artists"] = list(self.requires_absent_artists)
        out["expect"] = self.expect.to_json() if self.expect is not None else None
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Case:
        return cls(
            id=raw["id"],
            shape=raw["shape"],
            note=raw.get("note", ""),
            query=dict(raw["query"]),
            expect=(Verdict.from_json(raw["expect"]) if raw.get("expect") is not None else None),
            frozen=bool(raw.get("frozen", False)),
            issue=raw.get("issue", ""),
            suspect=bool(raw.get("suspect", False)),
            requires_discogs=bool(raw.get("requires_discogs", False)),
            requires_routes=tuple(raw.get("requires_routes", ())),
            requires_rows=tuple(tuple(pair) for pair in raw.get("requires_rows", ())),
            requires_absent_artists=tuple(raw.get("requires_absent_artists", ())),
            settings=dict(raw.get("settings", {})),
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_library_rows() -> list[dict[str, Any]]:
    """The seeded catalog, as plain dicts in `wxyc_etl.schema.library_columns()` order."""
    return _read_json(LIBRARY_PATH)


def load_discogs_fixture() -> dict[str, Any]:
    """Releases plus the search routing tables."""
    return _read_json(DISCOGS_PATH)


def load_cases() -> list[Case]:
    """Every corpus case, in file order.

    Collected at import time by the parameterized test, so this must stay
    cheap and must not touch anything an xdist worker could contend on.
    """
    return [Case.from_json(raw) for raw in _read_json(CASES_PATH)]


def count_by_shape(cases: list[Case]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.shape] = counts.get(case.shape, 0) + 1
    return counts


def dump_cases(cases: list[dict[str, Any]]) -> str:
    """Canonical serialization of `cases.json`.

    One writer, shared by the rebaseline tool and the formatting guard, so a
    machine rewrite never produces a whitespace diff that buries the one
    verdict that actually moved.
    """
    return json.dumps(cases, indent=2, ensure_ascii=False) + "\n"


def explain_mismatch(case: Case, actual: Verdict) -> str:
    """The failure message a contributor reads before deciding to rebaseline."""
    lines = [
        f"golden case {case.id!r} ({case.shape}) no longer produces its recorded verdict.",
        f"  query:    {case.query}",
    ]
    if case.issue:
        lines.append(f"  pins:     {case.issue}")
    if case.note:
        lines.append(f"  note:     {case.note}")
    expected = case.expect
    if expected is None:
        # Only reachable if `test_every_case_has_a_recorded_expectation` was
        # somehow skipped; say so plainly rather than rendering `None` fields.
        lines.append("  This case has NO recorded verdict. Run scripts.rebaseline_golden_corpus.")
        return "\n".join(lines)
    for label, exp, act in (
        ("miss_kind", expected.miss_kind, actual.miss_kind),
        ("song_not_found", expected.song_not_found, actual.song_not_found),
        ("found_on_compilation", expected.found_on_compilation, actual.found_on_compilation),
    ):
        if exp != act:
            lines.append(f"  {label}: expected {exp!r}, got {act!r}")
    if expected.results != actual.results:
        lines.append("  results: expected")
        lines.extend(f"      {r}" for r in expected.results or ["(none)"])
        lines.append("           got")
        lines.extend(f"      {r}" for r in actual.results or ["(none)"])
    if case.suspect:
        lines.append(
            "  This case is marked SUSPECT: its recorded verdict was believed to be "
            "WRONG when it was recorded (see the note above). A change here is more "
            "likely the fix landing than a regression -- read the note, then rebaseline."
        )
    if case.frozen:
        lines.append(
            "  This case is FROZEN: it pins a regression that already reached production "
            "once. The rebaseline tool will not rewrite it. If the new behavior is "
            "genuinely correct, hand-edit the case and say why in the commit message."
        )
    else:
        lines.append(
            "  If this change is intended, rebaseline with "
            "`uv run python -m scripts.rebaseline_golden_corpus` and commit the corpus "
            "diff on its own, with the reason. See docs/testing.md, 'Golden corpus'."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verdict extraction
# ---------------------------------------------------------------------------


def result_identity(result: dict[str, Any]) -> str:
    """Render one `LookupResultItem` as a stable, diff-readable identity.

    Catalog id first because it is the row's actual identity -- two Cat Power
    rows are both "Moon Pix" and differ only by id and format -- and because
    `#0` is the row-less sentinel, which the marker then spells out.
    """
    item = result.get("library_item") or {}
    library_id = item.get("id", -1)
    marker = ROWLESS_MARKER if library_id == 0 else ""
    return f"#{library_id} {marker}{item.get('artist')} — {item.get('title')}"


def verdict_from_payload(payload: dict[str, Any]) -> Verdict:
    """Project a `/lookup` response body down to the asserted verdict.

    `miss_kind` is re-derived here from the same three fields
    `lookup.miss_kind.derive_miss_kind` reads, rather than imported from the
    response: the wire response does not carry `miss_kind` (it is a telemetry
    property, not a contract field), and reading `results`/`timeout`/`degraded`
    off the JSON keeps the corpus asserting what a *caller* sees.
    """
    results = payload.get("results") or []
    if results:
        miss_kind = OUTCOME_HIT
    elif payload.get("timeout"):
        miss_kind = MISS_TIMEOUT
    elif payload.get("degraded"):
        miss_kind = MISS_DEGRADED
    else:
        miss_kind = MISS_CLEAN
    return Verdict(
        miss_kind=miss_kind,
        song_not_found=bool(payload.get("song_not_found")),
        found_on_compilation=bool(payload.get("found_on_compilation")),
        results=tuple(result_identity(r) for r in results),
    )


def verdict_shape_is_consistent(verdict: Verdict) -> bool:
    """`results` is non-empty iff `miss_kind` is a hit.

    Mirrors :func:`verdict_from_payload`'s own contract exactly: every miss
    kind -- `miss_clean`, `miss_timeout`, `miss_degraded` alike -- is derived
    from an empty `results` list (see the `elif`/`else` chain above), never a
    populated one. The only bifurcation that can ever be genuinely
    contradictory is hit-vs-any-miss, not clean-vs-not-clean -- a case
    expecting `miss_timeout` or `miss_degraded` with no results is well-formed,
    not a contradiction (LML#1233 review).
    """
    if verdict.miss_kind == OUTCOME_HIT:
        return bool(verdict.results)
    return not verdict.results


# ---------------------------------------------------------------------------
# Library seeding
# ---------------------------------------------------------------------------

#: The code under test's own column list (`library/db.py` imports the same
#: name to validate `library.db`'s schema), not a hand-copied second tuple --
#: exactly the reason `LIBRARY_FTS_CREATE_SQL` below is imported rather than
#: restated too. `scripts/build_golden_corpus.py::LIBRARY_FIELDS` imports it
#: too, so there is one column list, not two that can drift apart (LML#1233
#: review).
LIBRARY_COLUMNS = tuple(library_columns())

_CREATE_LIBRARY_SQL = """
    CREATE TABLE library (
        id INTEGER PRIMARY KEY,
        title TEXT,
        artist TEXT,
        call_letters TEXT,
        artist_call_number INTEGER,
        release_call_number INTEGER,
        genre TEXT,
        format TEXT,
        alternate_artist_name TEXT
    )
"""

#: Present but empty. `LibraryDB.connect()` probes for this table and skips the
#: streaming-status step entirely when it is missing, so creating it keeps that
#: step on the exercised path; leaving it empty keeps every row's
#: `has_streaming` False without a second fixture file to maintain.
_CREATE_STREAMING_LINKS_SQL = """
    CREATE TABLE streaming_links (
        library_id INTEGER PRIMARY KEY,
        spotify_url TEXT,
        apple_music_url TEXT,
        deezer_url TEXT,
        bandcamp_url TEXT,
        tidal_url TEXT,
        youtube_music_url TEXT,
        soundcloud_url TEXT
    )
"""


def build_library_db(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write a production-shaped `library.db` at `path` and return it.

    Uses the repo's own :data:`library.db.LIBRARY_FTS_CREATE_SQL` rather than a
    hand-copied `CREATE VIRTUAL TABLE`, so the corpus's index -- tokenizer
    included -- moves in lockstep with the code under test instead of pinning
    whatever the schema looked like the day the fixture was written.

    Synchronous `sqlite3` on purpose: this runs once per xdist worker from a
    session fixture, outside any event loop.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(_CREATE_LIBRARY_SQL)
        conn.execute(LIBRARY_FTS_CREATE_SQL)
        conn.execute(_CREATE_STREAMING_LINKS_SQL)
        conn.execute("CREATE INDEX idx_artist ON library(artist)")
        conn.execute("CREATE INDEX idx_title ON library(title)")
        placeholders = ", ".join("?" for _ in LIBRARY_COLUMNS)
        conn.executemany(
            f"INSERT INTO library ({', '.join(LIBRARY_COLUMNS)}) VALUES ({placeholders})",
            # `row[col]`, not `row.get(col)`: a row missing an expected column
            # is a fixture bug (library.json regenerated by an older column
            # list, or a column added to LIBRARY_COLUMNS without regenerating
            # the fixture) and must raise, not silently insert NULL for every
            # row and let the corpus record that as though it meant something
            # (LML#1233 review).
            [tuple(row[col] for col in LIBRARY_COLUMNS) for row in rows],
        )
        conn.execute("INSERT INTO library_fts(library_fts) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------------------
# The faked upstream
# ---------------------------------------------------------------------------


def _route_key(*parts: str | None) -> str:
    return "|".join((p or "").strip().lower() for p in parts)


class FakeDiscogsService:
    """A `DiscogsService` stand-in that serves `discogs.json` and nothing else.

    A concrete class rather than an `AsyncMock`: an `AsyncMock` invents a
    coroutine for any attribute touched, so a pipeline change that starts
    calling a new upstream method would keep passing while silently receiving
    a `MagicMock`. Here it raises `AttributeError`, and the corpus goes red on
    the commit that introduced the call -- which is the whole point of the
    tier.

    Every method is a lookup, never a matcher, with one deliberate exception:
    :meth:`validate_track_on_release` runs the real tracklist-match kernel.
    See this module's docstring.
    """

    def __init__(self, fixture: dict[str, Any]):
        self._releases: dict[int, ReleaseMetadataResponse] = {}
        self._compilations: set[int] = set()
        for raw in fixture.get("releases", []):
            release = ReleaseMetadataResponse(
                release_id=raw["release_id"],
                title=raw["title"],
                artist=raw["artist"],
                artist_id=raw.get("artist_id"),
                year=raw.get("year"),
                release_url=f"https://www.discogs.com/release/{raw['release_id']}",
                tracklist=[
                    TrackItem(
                        position=t.get("position", ""),
                        title=t["title"],
                        artists=t.get("artists", []),
                    )
                    for t in raw.get("tracklist", [])
                ],
            )
            self._releases[release.release_id] = release
            if raw.get("is_compilation"):
                self._compilations.add(release.release_id)
        self._track_searches: dict[str, list[int]] = fixture.get("track_searches", {})
        self._album_searches: dict[str, list[int]] = fixture.get("album_searches", {})
        self._album_title_searches: dict[str, list[int]] = fixture.get("album_title_searches", {})
        self.calls: list[str] = []
        self.served_candidates = False
        self.served_keys: set[str] = set()
        self.cache_service = None

    # -- helpers ----------------------------------------------------------

    def _record(self, call: str, served: bool, *, candidate_search: bool = False) -> None:
        """Log a call, and note whether it produced candidates for the matcher.

        Only the three *search* methods can set `served_candidates`, because
        only they answer "which releases might this be?" -- the question a
        `requires_discogs` case is claiming to exercise. `get_release` and the
        artist lookups are follow-ups on a candidate that already exists, so a
        case whose route key drifted would still see them fire (artwork alone
        does) and the guard would pass vacuously.

        The residual hole, stated rather than papered over: artwork also calls
        `search`, so a drifted track route on a case that still surfaces some
        routed shelf row could keep the flag set. Closing that would mean the
        fake knowing which caller it is answering, which is exactly the kind
        of matching logic a corpus double must not contain.
        """
        self.calls.append(call)
        if served and candidate_search:
            self.served_candidates = True
            self.served_keys.add(call)

    def _release_info(self, release_id: int) -> ReleaseInfo:
        release = self._releases[release_id]
        return ReleaseInfo(
            album=release.title,
            artist=release.artist,
            release_id=release.release_id,
            release_url=release.release_url,
            is_compilation=release.release_id in self._compilations,
        )

    # -- the faked I/O boundary -------------------------------------------

    async def search(
        self,
        request: DiscogsSearchRequest,
        limit: int = 5,
        *,
        skip_pg: bool = False,
        **_: Any,
    ) -> DiscogsSearchResponse:
        # `limit` defaults to 5, mirroring `DiscogsService.search`
        # (`discogs/service.py`), and is applied the same way its sibling
        # methods below apply theirs (`ids[:limit]`) -- previously ignored
        # entirely, so a route carrying more candidates than the caller asked
        # for would hand all of them back (LML#1233 review). Latent against
        # today's fixture (every route carries at most one id) but not against
        # a future one.
        key = _route_key(request.artist, request.album)
        ids = self._album_searches.get(key, [])
        self._record(f"search:{key}", bool(ids), candidate_search=True)
        return DiscogsSearchResponse(
            results=[
                DiscogsSearchResult(
                    album=self._releases[rid].title,
                    artist=self._releases[rid].artist,
                    release_id=rid,
                    release_url=self._releases[rid].release_url,
                )
                for rid in ids[:limit]
            ],
            total=len(ids),
        )

    async def get_release(self, release_id: int, **_: Any) -> ReleaseMetadataResponse | None:
        release = self._releases.get(release_id)
        self._record(f"get_release:{release_id}", release is not None)
        return release

    async def search_releases_by_track(
        self, track: str, artist: str | None = None, limit: int = 20, **_: Any
    ) -> TrackReleasesResponse:
        ids = self._track_searches.get(_route_key(track, artist), [])
        self._record(f"track:{_route_key(track, artist)}", bool(ids), candidate_search=True)
        return TrackReleasesResponse(
            track=track,
            artist=artist,
            releases=[self._release_info(rid) for rid in ids[:limit]],
            total=len(ids),
            cached=False,
        )

    async def search_releases_by_album_title(
        self, album: str, limit: int = 10, **_: Any
    ) -> TrackReleasesResponse:
        ids = self._album_title_searches.get(_route_key(album), [])
        self._record(f"album_title:{_route_key(album)}", bool(ids), candidate_search=True)
        return TrackReleasesResponse(
            track=None,
            artist=None,
            releases=[self._release_info(rid) for rid in ids[:limit]],
            total=len(ids),
            cached=False,
        )

    async def validate_track_on_release(self, release_id: int, track: str, artist: str) -> bool:
        """The one method that is not a lookup.

        Delegates to the production `_scan_tracklist_for_match`, so the
        LML#1225 query-coverage gate, the LML#334 fuzzy fallback, and the
        per-track/release-level artist steps all run for real against the
        fixture tracklist. Imported inside the call to keep the import graph
        of this module free of `discogs.service`'s httpx/limiter setup.
        """
        from discogs.service import _scan_tracklist_for_match

        release = self._releases.get(release_id)
        self._record(f"validate:{release_id}:{track.lower()}", release is not None)
        if release is None:
            return False
        return _scan_tracklist_for_match(release, release_id, track, artist)

    async def get_track_credit_on_release(self, release_id: int, track: str) -> str | None:
        release = self._releases.get(release_id)
        self._record(f"track_credit:{release_id}", release is not None)
        if release is None:
            return None
        for item in release.tracklist or []:
            if item.title.lower() == track.strip().lower() and item.artists:
                return ", ".join(item.artists)
        return None

    async def get_release_artist_variations(self, release_id: int) -> set[str]:
        self._record(f"artist_variations:{release_id}", False)
        return set()

    async def get_artist_image(self, artist_id: int) -> str | None:
        self._record(f"artist_image:{artist_id}", False)
        return None

    async def get_artist_details(self, artist_id: int, **_: Any) -> ArtistDetails | None:
        self._record(f"artist_details:{artist_id}", False)
        return None
