"""Unit tests for scripts/streaming_availability/pipeline.py."""

import asyncio

import pytest

from scripts.streaming_availability.pipeline import Pipeline, Stage


class TestPipeline:
    @pytest.mark.asyncio
    async def test_single_stage(self):
        """A single stage processes all items."""
        results = []

        async def process(item: int) -> int:
            results.append(item * 2)
            return item * 2

        pipeline = Pipeline()
        pipeline.add_stage(Stage("double", process))
        await pipeline.run([1, 2, 3])

        assert sorted(results) == [2, 4, 6]

    @pytest.mark.asyncio
    async def test_two_stages_pipeline(self):
        """Items flow from stage 1 to stage 2."""
        stage1_results = []
        stage2_results = []

        async def stage1_fn(item: int) -> int:
            await asyncio.sleep(0.01)  # simulate work
            stage1_results.append(item)
            return item

        async def stage2_fn(item: int) -> int:
            stage2_results.append(item * 10)
            return item * 10

        pipeline = Pipeline()
        pipeline.add_stage(Stage("first", stage1_fn))
        pipeline.add_stage(Stage("second", stage2_fn))
        await pipeline.run([1, 2, 3])

        assert sorted(stage1_results) == [1, 2, 3]
        assert sorted(stage2_results) == [10, 20, 30]

    @pytest.mark.asyncio
    async def test_stage_can_filter(self):
        """A stage returning None filters the item from the next stage."""
        stage2_items = []

        async def only_evens(item: int) -> int | None:
            return item if item % 2 == 0 else None

        async def collect(item: int) -> int:
            stage2_items.append(item)
            return item

        pipeline = Pipeline()
        pipeline.add_stage(Stage("filter", only_evens))
        pipeline.add_stage(Stage("collect", collect))
        await pipeline.run([1, 2, 3, 4, 5])

        assert sorted(stage2_items) == [2, 4]

    @pytest.mark.asyncio
    async def test_three_stages(self):
        """Three stages process items in sequence."""
        final = []

        async def add_one(x: int) -> int:
            return x + 1

        async def double(x: int) -> int:
            return x * 2

        async def collect(x: int) -> int:
            final.append(x)
            return x

        pipeline = Pipeline()
        pipeline.add_stage(Stage("add", add_one))
        pipeline.add_stage(Stage("double", double))
        pipeline.add_stage(Stage("collect", collect))
        await pipeline.run([1, 2, 3])

        assert sorted(final) == [4, 6, 8]  # (1+1)*2=4, (2+1)*2=6, (3+1)*2=8

    @pytest.mark.asyncio
    async def test_concurrency(self):
        """Items within a stage run concurrently up to batch_size."""
        max_concurrent = 0
        current = 0
        lock = asyncio.Lock()

        async def track_concurrency(item: int) -> int:
            nonlocal max_concurrent, current
            async with lock:
                current += 1
                if current > max_concurrent:
                    max_concurrent = current
            await asyncio.sleep(0.05)
            async with lock:
                current -= 1
            return item

        pipeline = Pipeline(batch_size=5)
        pipeline.add_stage(Stage("concurrent", track_concurrency))
        await pipeline.run(list(range(10)))

        assert max_concurrent > 1  # at least some concurrency

    @pytest.mark.asyncio
    async def test_empty_input(self):
        """Pipeline handles empty input gracefully."""
        called = False

        async def should_not_run(item: int) -> int:
            nonlocal called
            called = True
            return item

        pipeline = Pipeline()
        pipeline.add_stage(Stage("noop", should_not_run))
        await pipeline.run([])

        assert not called

    @pytest.mark.asyncio
    async def test_stage_error_doesnt_stop_pipeline(self):
        """Errors in one item don't prevent other items from processing."""
        results = []

        async def flaky(item: int) -> int:
            if item == 2:
                raise ValueError("boom")
            results.append(item)
            return item

        pipeline = Pipeline()
        pipeline.add_stage(Stage("flaky", flaky))
        await pipeline.run([1, 2, 3])

        assert sorted(results) == [1, 3]
