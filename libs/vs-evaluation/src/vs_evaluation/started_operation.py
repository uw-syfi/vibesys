"""Wait for a held operation to start without hanging when it never does.

The generic waits (``wait_until_started``, ``arrival``, ``wait_until_started_sync``,
``start_thread``) live in ``vs_sim.api.testing``; this module adds the one that knows about
evaluation executors.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from vs_evaluation.lifecycle import is_finished
from vs_sim.api.testing import wait_until_started

if TYPE_CHECKING:
    from vs_evaluation.ports import EvaluationExecutor
    from vs_sim.api import Event


async def _ended(executor: EvaluationExecutor, handle_id: str) -> None:
    while True:
        observed = await executor.inspect(handle_id)
        if observed is not None and is_finished(observed.state):
            return
        await executor.wait_for_change(handle_id, timeout_s=float("inf"))
        # A fake may return at once; yield so the test's own tasks still run.
        await asyncio.sleep(0)


async def wait_until_executor_started(
    started: Event | asyncio.Event, executor: EvaluationExecutor, handle_id: str
) -> None:
    """Return once *started* is set; fail if the evaluation *handle_id* ends first.

    For a held step that a worker inside the executor starts, where the test has
    no handle on the operation itself: the evaluation reaching a terminal state
    stands in for the operation ending, so a start that never comes fails the
    test with the evaluation's end and not with a parked worker. A held step
    keeps the evaluation live, so one that is already ended when the start is
    observed was not held.

    The watch reads the executor's published state with ``inspect`` and never with
    ``inspect_only``: while the evaluation's own task runs, ``inspect`` answers from
    what that task published, but ``inspect_only`` polls the scheduler. A poll is a
    command, and against a scheduler that charges time per command (the trace replay)
    each poll moves the world's clock, so how many polls land before the held step
    decided whether the job had already ended by then.

    Raises:
        AssertionError: the evaluation ended without reaching its held step.
    """
    watcher = asyncio.ensure_future(_ended(executor, handle_id))
    try:
        await wait_until_started(started, watcher)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
