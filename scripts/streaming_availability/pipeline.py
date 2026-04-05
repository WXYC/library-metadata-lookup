"""Async pipeline for processing items through multiple concurrent stages.

Items flow through stages connected by asyncio queues. Each stage processes
items concurrently (up to batch_size), and passes results to the next stage.
A stage returning None filters the item from downstream stages.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_SENTINEL = object()


@dataclass
class Stage:
    """A single processing stage in the pipeline."""

    name: str
    process: Callable[[Any], Coroutine[Any, Any, Any | None]]


class Pipeline:
    """Multi-stage async pipeline with concurrent item processing.

    Items are fed into the first stage, processed concurrently in batches,
    and results flow to the next stage. Stages returning None filter items
    from downstream. Exceptions in individual items are logged and the item
    is dropped.
    """

    def __init__(self, batch_size: int = 100):
        self._stages: list[Stage] = []
        self._batch_size = batch_size

    def add_stage(self, stage: Stage) -> None:
        self._stages.append(stage)

    async def run(self, items: list[Any]) -> None:
        if not self._stages or not items:
            return

        # Create queues between stages
        queues: list[asyncio.Queue] = []
        for _ in range(len(self._stages)):
            queues.append(asyncio.Queue())

        # Feed items into the first queue
        input_q = queues[0]
        for item in items:
            await input_q.put(item)
        await input_q.put(_SENTINEL)

        # Create a worker for each stage
        async def _worker(stage: Stage, in_q: asyncio.Queue, out_q: asyncio.Queue | None) -> None:
            batch: list[Any] = []

            while True:
                item = await in_q.get()
                if item is _SENTINEL:
                    break
                batch.append(item)

                if len(batch) >= self._batch_size:
                    await _process_batch(stage, batch, out_q)
                    batch = []

            # Process remaining items
            if batch:
                await _process_batch(stage, batch, out_q)

            # Signal downstream that we're done
            if out_q is not None:
                await out_q.put(_SENTINEL)

        async def _process_batch(
            stage: Stage, batch: list[Any], out_q: asyncio.Queue | None
        ) -> None:
            async def _safe_process(item: Any) -> tuple[Any | None, bool]:
                try:
                    result = await stage.process(item)
                    return result, True
                except Exception:
                    logger.exception("Pipeline stage '%s' error", stage.name)
                    return None, False

            results = await asyncio.gather(*[_safe_process(item) for item in batch])

            if out_q is not None:
                for result, ok in results:
                    if ok and result is not None:
                        await out_q.put(result)

        # Launch all workers concurrently
        workers = []
        for i, stage in enumerate(self._stages):
            in_q = queues[i]
            out_q = queues[i + 1] if i + 1 < len(queues) else None
            workers.append(_worker(stage, in_q, out_q))

        await asyncio.gather(*workers)
