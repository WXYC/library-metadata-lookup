"""LML#1354: the flagged memory profiler.

Prod RSS ramps ~0.07 GB/day for 13 days and only resets on a deploy, and static
reading has already failed once to localise it. This suite covers the
instrumentation that replaces the reading: the cheap ``gauges`` report that can
run on production, the ``tracemalloc`` snapshot diff that cannot, the sampler
loop that emits them, and the default-off gate that makes merging the whole
thing a zero-behaviour change.

Everything here is driven with fakes — a fake ``/proc`` pair, a fake pool, fake
``tracemalloc.Snapshot`` objects, an injected sleep — so no test depends on
procfs existing (macOS has none), on a real asyncpg pool, or on real time.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from config.settings import Settings
from core import memory_profile


class _FakePool:
    """Minimal asyncpg.Pool stand-in exposing only the two size accessors."""

    def __init__(self, size: int, idle: int) -> None:
        self._size = size
        self._idle = idle

    def get_size(self) -> int:
        return self._size

    def get_idle_size(self) -> int:
        return self._idle


class _FakeTraceback:
    def __init__(self, lines: tuple[str, ...]) -> None:
        self._lines = lines

    def format(self) -> list[str]:
        return list(self._lines)


class _FakeStat:
    """``tracemalloc.StatisticDiff`` / ``Statistic`` stand-in."""

    def __init__(
        self,
        *,
        size: int,
        size_diff: int = 0,
        count: int = 1,
        count_diff: int = 0,
        lines: tuple[str, ...] = ("frame",),
    ) -> None:
        self.size = size
        self.size_diff = size_diff
        self.count = count
        self.count_diff = count_diff
        self.traceback = _FakeTraceback(lines)


class _FakeSnapshot:
    """``tracemalloc.Snapshot`` stand-in recording what it was asked for."""

    def __init__(
        self, *, compare: tuple[_FakeStat, ...] = (), stats: tuple[_FakeStat, ...] = ()
    ) -> None:
        self._compare = list(compare)
        self._stats = list(stats)
        self.filtered_with: object = None
        self.compared_to: object = None

    def filter_traces(self, filters):
        self.filtered_with = filters
        return self

    def compare_to(self, previous, key_type):
        self.compared_to = (previous, key_type)
        return list(self._compare)

    def statistics(self, key_type):
        return list(self._stats)


class TestReadRssBytes:
    """RSS comes from ``/proc/self/statm`` field 2 (resident pages), not psutil —
    the ticket's constraint is that always-on instrumentation stays cheap, and a
    dependency for one number is not that."""

    def test_reads_resident_pages_times_page_size(self, tmp_path):
        from core.memory_profile import read_rss_bytes

        statm = tmp_path / "statm"
        # size resident shared text lib data dt — resident is field 2.
        statm.write_text("2048 512 100 1 0 300 0\n")
        assert read_rss_bytes(statm, page_size=4096) == 512 * 4096

    def test_missing_procfs_reads_none(self, tmp_path):
        """macOS and any non-Linux runner has no procfs. The gauge must degrade
        to ``None`` rather than raise, so the rest of the report still lands."""
        from core.memory_profile import read_rss_bytes

        assert read_rss_bytes(tmp_path / "nope", page_size=4096) is None

    def test_unparseable_reads_none(self, tmp_path):
        from core.memory_profile import read_rss_bytes

        statm = tmp_path / "statm"
        statm.write_text("garbage\n")
        assert read_rss_bytes(statm, page_size=4096) is None


class TestCountOpenFds:
    """``len(os.listdir('/proc/self/fd'))`` — the LML#241 shape. A climbing fd
    count is the one gauge here that names a known prior incident."""

    def test_counts_entries(self, tmp_path):
        from core.memory_profile import count_open_fds

        (tmp_path / "0").write_text("")
        (tmp_path / "1").write_text("")
        assert count_open_fds(tmp_path) == 2

    def test_missing_dir_counts_none(self, tmp_path):
        from core.memory_profile import count_open_fds

        assert count_open_fds(tmp_path / "nope") is None


class TestCollectGauges:
    @pytest.mark.asyncio
    async def test_reads_pool_sizes_from_the_injected_pool(self):
        from core.memory_profile import collect_gauges

        gauges = collect_gauges(pool=_FakePool(size=8, idle=6))
        assert gauges["pool_size"] == 8
        assert gauges["pool_idle_size"] == 6

    @pytest.mark.asyncio
    async def test_no_pool_leaves_the_pool_keys_none(self):
        """API-only mode (no discogs-cache DSN) has no pool; the keys must still
        be present so the log line's shape does not change between deploys."""
        from core.memory_profile import collect_gauges

        gauges = collect_gauges(pool=None)
        assert gauges["pool_size"] is None
        assert gauges["pool_idle_size"] is None

    @pytest.mark.asyncio
    async def test_reports_the_live_cache_registry_and_task_count(self):
        from core.memory_profile import collect_gauges
        from discogs.memory_cache import _cache_registry

        gauges = collect_gauges(pool=None)
        assert len(gauges["discogs_cache_currsizes"]) == len(_cache_registry)
        assert gauges["asyncio_tasks"] >= 1  # at least this test's own task
        assert len(gauges["gc_counts"]) == 3

    def test_task_count_degrades_to_none_off_the_loop(self):
        """``asyncio.all_tasks()`` raises without a running loop. The sampler
        always has one, but a gauge that can raise is a gauge that can kill the
        report that carries the other eight."""
        from core.memory_profile import count_tasks

        assert count_tasks() is None


class TestFormatReport:
    """``format_report`` is pure: fake gauges in, the log payload out. It is the
    seam that lets the report's shape be asserted without a process to measure."""

    def _gauges(self, **overrides):
        base = {
            "rss_bytes": 512 * 1024 * 1024,
            "open_fds": 42,
            "asyncio_tasks": 7,
            "gc_counts": [100, 5, 1],
            "discogs_cache_currsizes": [10, 20, 30],
            "library_artist_cache": 11,
            "library_search_cache": 22,
            "pool_size": 8,
            "pool_idle_size": 6,
        }
        base.update(overrides)
        return base

    def test_derives_rss_mb(self):
        from core.memory_profile import format_report

        assert format_report(self._gauges())["rss_mb"] == pytest.approx(512.0)

    def test_totals_the_discogs_cache_entries(self):
        from core.memory_profile import format_report

        report = format_report(self._gauges())
        assert report["discogs_cache_entries"] == 60
        assert report["discogs_cache_sizes"] == [10, 20, 30]

    def test_passes_the_remaining_gauges_through(self):
        from core.memory_profile import format_report

        report = format_report(self._gauges())
        assert report["open_fds"] == 42
        assert report["asyncio_tasks"] == 7
        assert report["gc_counts"] == [100, 5, 1]
        assert report["library_artist_cache"] == 11
        assert report["library_search_cache"] == 22
        assert report["pool_size"] == 8
        assert report["pool_idle_size"] == 6

    def test_none_rss_stays_none(self):
        from core.memory_profile import format_report

        assert format_report(self._gauges(rss_bytes=None))["rss_mb"] is None


class TestDiffSnapshots:
    def test_growing_is_top_n_by_size_diff_descending(self):
        from core.memory_profile import diff_snapshots

        current = _FakeSnapshot(
            compare=(
                _FakeStat(size=10, size_diff=100),
                _FakeStat(size=20, size_diff=900),
                _FakeStat(size=30, size_diff=500),
            )
        )
        diff = diff_snapshots(current, _FakeSnapshot(), top_n=2)
        assert [e["size_diff_bytes"] for e in diff["growing"]] == [900, 500]

    def test_largest_is_top_n_by_absolute_size_descending(self):
        from core.memory_profile import diff_snapshots

        current = _FakeSnapshot(
            stats=(
                _FakeStat(size=10),
                _FakeStat(size=900),
                _FakeStat(size=500),
            )
        )
        diff = diff_snapshots(current, None, top_n=2)
        assert [e["size_bytes"] for e in diff["largest"]] == [900, 500]

    def test_first_interval_has_no_growing_list(self):
        """Exactly one previous snapshot is held, so the first interval has
        nothing to compare against — absolute sizes only, not a fabricated diff."""
        from core.memory_profile import diff_snapshots

        diff = diff_snapshots(_FakeSnapshot(stats=(_FakeStat(size=1),)), None, top_n=5)
        assert diff["growing"] == []

    def test_compares_by_traceback(self):
        """``compare_to(..., "traceback")`` — grouping by filename alone merges
        distinct call paths through one module, which is the discrimination the
        diff exists for."""
        from core.memory_profile import diff_snapshots

        current = _FakeSnapshot(compare=(_FakeStat(size=1, size_diff=1),))
        previous = _FakeSnapshot()
        diff_snapshots(current, previous, top_n=1)
        assert current.compared_to == (previous, "traceback")

    def test_entries_carry_the_formatted_traceback(self):
        from core.memory_profile import diff_snapshots

        current = _FakeSnapshot(compare=(_FakeStat(size=5, size_diff=5, lines=("a", "b")),))
        entry = diff_snapshots(current, _FakeSnapshot(), top_n=1)["growing"][0]
        assert entry["traceback"] == ["a", "b"]
        assert entry["size_bytes"] == 5


class TestDiffSnapshotsAgainstRealTracemalloc:
    """The fakes above can only be as right as their author's model of the API.
    ``compare_to()`` yields ``StatisticDiff`` (which has ``size_diff`` /
    ``count_diff``) but ``statistics()`` yields ``Statistic`` (which has
    neither), so one summariser written against the diff shape alone raises
    ``AttributeError`` on the ``largest`` ranking — in production, inside the
    profiler, on a service already suspected of leaking. Only a real snapshot
    pair catches that."""

    def test_both_rankings_survive_real_snapshots(self):
        import tracemalloc

        from core.memory_profile import SNAPSHOT_FILTERS, diff_snapshots

        was_tracing = tracemalloc.is_tracing()
        if not was_tracing:
            tracemalloc.start(3)
        try:
            first = tracemalloc.take_snapshot().filter_traces(SNAPSHOT_FILTERS)
            ballast = [bytes(2048) for _ in range(200)]
            second = tracemalloc.take_snapshot().filter_traces(SNAPSHOT_FILTERS)
        finally:
            if not was_tracing:
                tracemalloc.stop()

        diff = diff_snapshots(second, first, top_n=3)
        assert len(ballast) == 200  # keep it alive across the second snapshot
        assert 0 < len(diff["largest"]) <= 3
        assert all(e["size_bytes"] > 0 and e["traceback"] for e in diff["largest"])
        assert 0 < len(diff["growing"]) <= 3
        # The ballast allocated between the two snapshots must be the top grower.
        assert diff["growing"][0]["size_diff_bytes"] >= 200 * 2048


class TestTakeFilteredSnapshot:
    def test_filters_out_the_profiler_s_own_machinery(self):
        """``tracemalloc``, ``linecache`` and the import bootstrap allocate on
        every snapshot; left in, they crowd the top-N the leak has to appear in."""
        from core.memory_profile import SNAPSHOT_FILTERS

        excluded = [f.filename_pattern for f in SNAPSHOT_FILTERS]
        assert all(f.inclusive is False for f in SNAPSHOT_FILTERS)
        assert "<frozen importlib._bootstrap>" in excluded
        assert any("tracemalloc" in p for p in excluded)
        assert any("linecache" in p for p in excluded)


class TestRunSampler:
    @pytest.mark.asyncio
    async def test_runs_n_iterations_with_an_injected_sleep_then_cancels_cleanly(self, caplog):
        from core.memory_profile import run_sampler

        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) >= 3:
                await asyncio.Event().wait()  # park so the test controls the end

        with caplog.at_level(logging.INFO, logger="core.memory_profile"):
            task = asyncio.create_task(
                run_sampler(
                    mode="gauges",
                    interval_s=600,
                    top_n=5,
                    sleep=fake_sleep,
                    pool_getter=_none_pool,
                )
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert slept == [600, 600, 600]
        reports = [r for r in caplog.records if r.getMessage().startswith("memory_profile ")]
        assert len(reports) == 2  # one per completed interval before the park

    @pytest.mark.asyncio
    async def test_a_failing_sample_does_not_kill_the_loop(self):
        """A dead sampler task is silent, and silence reads identically to "the
        ramp stopped" — the one failure mode a leak hunt cannot afford."""
        from core.memory_profile import run_sampler

        calls: list[int] = []

        async def exploding_pool_getter():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return None

        async def fake_sleep(_seconds: float) -> None:
            if len(calls) >= 2:
                await asyncio.Event().wait()

        task = asyncio.create_task(
            run_sampler(
                mode="gauges",
                interval_s=1,
                top_n=5,
                sleep=fake_sleep,
                pool_getter=exploding_pool_getter,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 2  # survived the first blow-up and sampled again

    @pytest.mark.asyncio
    async def test_stop_sampler_swallows_the_cancellation(self):
        from core.memory_profile import run_sampler, stop_sampler

        async def fake_sleep(_seconds: float) -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(
            run_sampler(
                mode="gauges", interval_s=1, top_n=5, sleep=fake_sleep, pool_getter=_none_pool
            )
        )
        await asyncio.sleep(0)
        await stop_sampler(task)
        assert task.cancelled()


async def _none_pool():
    return None


class TestStartSampler:
    def test_mode_off_starts_nothing(self):
        """The merge gate: the flag defaults to ``off`` and ``off`` must create
        no task, start no tracemalloc, and log no line."""
        from core.memory_profile import start_sampler

        settings = Settings(lml_memory_profile_mode="off")
        assert start_sampler(settings) is None

    @pytest.mark.asyncio
    async def test_gauges_mode_starts_a_task_without_tracemalloc(self, monkeypatch):
        import tracemalloc

        from core.memory_profile import start_sampler, stop_sampler

        started: list[int] = []
        monkeypatch.setattr(tracemalloc, "start", lambda nframe=1: started.append(nframe))

        task = start_sampler(Settings(lml_memory_profile_mode="gauges"))
        assert task is not None
        await stop_sampler(task)
        assert started == []

    @pytest.mark.asyncio
    async def test_tracemalloc_mode_starts_tracing_with_the_configured_frames(self, monkeypatch):
        import tracemalloc

        from core.memory_profile import start_sampler, stop_sampler

        started: list[int] = []
        monkeypatch.setattr(tracemalloc, "start", lambda nframe=1: started.append(nframe))

        task = start_sampler(
            Settings(lml_memory_profile_mode="tracemalloc", lml_memory_profile_frames=7)
        )
        assert task is not None
        await stop_sampler(task)
        assert started == [7]


class TestTheCheapGaugesSurviveTheExpensiveOnes:
    """A report must not be lost because one of its costly parts failed.

    The module's own reasoning is that a dead sampler is silent and silence
    reads exactly like "the ramp stopped". The same argument applies one level
    down: if a pool acquisition or a ``tracemalloc`` snapshot can discard the
    whole line, then the RSS series goes dark during precisely the incident
    worth measuring -- a database outage, or a profiler running out of room.
    """

    @staticmethod
    async def _raising_pool_getter():
        raise RuntimeError("discogs-cache unreachable")

    @staticmethod
    async def _no_pool_getter():
        return None

    def test_a_failing_pool_getter_still_emits_the_other_gauges(self, caplog):
        with caplog.at_level(logging.INFO, logger="core.memory_profile"):
            asyncio.run(
                memory_profile.report_once(
                    mode="gauges",
                    top_n=3,
                    previous=None,
                    pool_getter=self._raising_pool_getter,
                )
            )
        lines = [r for r in caplog.records if r.getMessage().startswith("memory_profile ")]
        assert len(lines) == 1
        assert "gc_counts" in lines[0].getMessage()

    def test_a_pool_whose_accessors_raise_degrades_to_none(self):
        class _BrokenPool:
            def get_size(self):
                raise RuntimeError("pool is closing")

            def get_idle_size(self):
                raise RuntimeError("pool is closing")

        gauges = memory_profile.collect_gauges(pool=_BrokenPool())
        assert gauges["pool_size"] is None
        assert gauges["pool_idle_size"] is None
        assert gauges["gc_counts"]

    def test_a_failing_snapshot_still_emits_the_gauges(self, caplog, monkeypatch):
        def _boom():
            raise RuntimeError("out of room for traces")

        monkeypatch.setattr(memory_profile.tracemalloc, "take_snapshot", _boom)
        with caplog.at_level(logging.INFO, logger="core.memory_profile"):
            asyncio.run(
                memory_profile.report_once(
                    mode="tracemalloc",
                    top_n=3,
                    previous=None,
                    pool_getter=self._no_pool_getter,
                )
            )
        lines = [r for r in caplog.records if r.getMessage().startswith("memory_profile ")]
        assert len(lines) == 1
        assert "rss_mb" in lines[0].getMessage()
        assert "tracemalloc_error" in lines[0].getMessage()

    def test_a_failing_snapshot_keeps_the_previous_baseline(self, monkeypatch):
        """Otherwise one bad interval also destroys the *next* diff."""
        sentinel = object()

        def _boom():
            raise RuntimeError("out of room for traces")

        monkeypatch.setattr(memory_profile.tracemalloc, "take_snapshot", _boom)
        kept = asyncio.run(
            memory_profile.report_once(
                mode="tracemalloc",
                top_n=3,
                previous=sentinel,
                pool_getter=self._no_pool_getter,
            )
        )
        assert kept is sentinel


class TestTheProfilerNeverBuildsThePool:
    """``async_singleton`` re-invokes its factory whenever the cached value is
    ``None`` -- it only caches a truthy instance. The discogs pool factory
    returns ``None`` both in API-only mode and when the database is
    unreachable, so a getter called on a timer is a pool *builder* on a timer.
    """

    def test_default_getter_does_not_invoke_the_singleton(self, monkeypatch):
        import core.dependencies as deps

        calls: list[int] = []

        async def _tripwire():
            calls.append(1)
            return None

        monkeypatch.setattr(deps, "get_discogs_pool", _tripwire)
        result = asyncio.run(memory_profile._default_pool_getter())
        assert calls == [], "the profiler called the pool-building singleton"
        assert result is None

    def test_peek_reports_none_when_nothing_has_built_a_pool(self):
        from core.dependencies import peek_discogs_pool

        assert peek_discogs_pool() is None

    def test_peek_returns_the_pool_once_something_else_has_built_one(self, monkeypatch):
        """The other half of the contract: a peek that never sees a pool would
        silence the two pool gauges forever, which reads as "no connection
        growth" rather than "not measured"."""
        import core.dependencies as deps

        sentinel = object()

        async def _fake_create_pool(*args, **kwargs):
            return sentinel

        monkeypatch.setattr(deps.asyncpg, "create_pool", _fake_create_pool)
        monkeypatch.setattr(deps, "_last_built_discogs_pool", None)
        monkeypatch.setattr(
            deps, "get_settings", lambda: SimpleNamespace(database_url_discogs="postgresql://x/y")
        )

        built = asyncio.run(deps._build_discogs_pool())

        assert built is sentinel
        assert deps.peek_discogs_pool() is sentinel
        assert asyncio.run(memory_profile._default_pool_getter()) is sentinel


_MB = 1024 * 1024


class TestRssBound:
    """LML#1400: a ramp nobody is watching has to announce itself.

    LML#1354's ramp ran for months and was found on the bill. The gauges line
    is INFO, so it only helps someone already reading logs; the bound turns the
    same RSS reading into one report per crossing.
    """

    @pytest.mark.parametrize(
        "bound_mb, series, expected",
        [
            pytest.param(600, [200.0, 400.0, 599.9, 600.0], [], id="at-or-below-never-reports"),
            pytest.param(600, [500.0, 650.0], [650.0], id="a-crossing-reports-once"),
            pytest.param(600, [650.0, 700.0, 900.0], [650.0], id="staying-above-does-not-repeat"),
            pytest.param(600, [650.0, 500.0, 620.0], [650.0, 620.0], id="re-arms-after-falling"),
            pytest.param(600, [650.0, None, 700.0], [650.0], id="an-unreadable-sample-keeps-state"),
            pytest.param(0, [5000.0], [], id="zero-disables"),
        ],
    )
    def test_reports_on_the_crossing_not_on_every_sample(self, bound_mb, series, expected):
        from core.memory_profile import RssBound

        reported: list[tuple[float, int]] = []
        bound = RssBound(bound_mb, report=lambda rss_mb, limit: reported.append((rss_mb, limit)))

        for rss_mb in series:
            bound.observe(rss_mb)

        assert reported == [(rss_mb, bound_mb) for rss_mb in expected]

    def test_a_failing_report_neither_raises_nor_repeats(self):
        """An hourly sampler above the bound must not retry a broken reporter
        every interval, and must not lose its own log line to one."""
        from core.memory_profile import RssBound

        calls: list[float] = []

        def exploding_report(rss_mb: float, _limit: int) -> None:
            calls.append(rss_mb)
            raise RuntimeError("sentry is down")

        bound = RssBound(600, report=exploding_report)
        bound.observe(650.0)
        bound.observe(700.0)

        assert calls == [650.0]

    def test_the_default_report_warns_and_sends_one_sentry_message(self, monkeypatch, caplog):
        """WARNING records are breadcrumbs only under this service's Sentry
        logging integration (event_level=ERROR), so the report sends an explicit
        message an alert rule can key on. The text is constant so every crossing
        groups into one issue; the numbers travel as context."""
        sent: list[tuple[str, str]] = []
        monkeypatch.setattr(
            memory_profile.sentry_sdk,
            "capture_message",
            lambda message, level=None, **_kwargs: sent.append((message, level)),
        )

        with caplog.at_level(logging.WARNING, logger="core.memory_profile"):
            memory_profile.report_rss_over_bound(650.0, 600)

        assert sent == [(memory_profile.RSS_OVER_BOUND_MESSAGE, "warning")]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "650.0" in warnings[0].getMessage() and "600" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_report_once_feeds_the_bound_the_rss_it_logged(self, monkeypatch):
        from core.memory_profile import RssBound, report_once

        monkeypatch.setattr(memory_profile, "read_rss_bytes", lambda: 700 * _MB)
        reported: list[tuple[float, int]] = []
        bound = RssBound(600, report=lambda rss_mb, limit: reported.append((rss_mb, limit)))

        for _ in range(2):
            await report_once(
                mode="gauges", top_n=5, previous=None, pool_getter=_none_pool, rss_bound=bound
            )

        assert reported == [(700.0, 600)]

    @pytest.mark.asyncio
    async def test_the_sampler_holds_one_bound_across_intervals(self, monkeypatch):
        """The crossing state must outlive a single report, or every interval
        above the bound would look like a fresh crossing."""
        from core.memory_profile import run_sampler

        monkeypatch.setattr(memory_profile, "read_rss_bytes", lambda: 700 * _MB)
        reported: list[float] = []
        monkeypatch.setattr(
            memory_profile, "report_rss_over_bound", lambda rss_mb, _limit: reported.append(rss_mb)
        )
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) >= 4:
                await asyncio.Event().wait()

        task = asyncio.create_task(
            run_sampler(
                mode="gauges",
                interval_s=1,
                top_n=5,
                rss_warn_mb=600,
                sleep=fake_sleep,
                pool_getter=_none_pool,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(slept) == 4  # three completed reports, all above the bound
        assert reported == [700.0]

    @pytest.mark.asyncio
    async def test_start_sampler_passes_the_configured_bound(self, monkeypatch):
        from core.memory_profile import start_sampler, stop_sampler

        seen: dict[str, object] = {}

        async def fake_run_sampler(**kwargs):
            seen.update(kwargs)
            await asyncio.Event().wait()

        monkeypatch.setattr(memory_profile, "run_sampler", fake_run_sampler)

        task = start_sampler(Settings(lml_memory_profile_mode="gauges", lml_memory_rss_warn_mb=512))
        assert task is not None
        await asyncio.sleep(0)
        await stop_sampler(task)

        assert seen["rss_warn_mb"] == 512

    def test_the_bound_defaults_on_and_rejects_negatives(self):
        from pydantic import ValidationError

        assert Settings().lml_memory_rss_warn_mb == 600
        with pytest.raises(ValidationError):
            Settings(lml_memory_rss_warn_mb=-1)
