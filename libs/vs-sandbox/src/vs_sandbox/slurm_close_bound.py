"""How long the Slurm executor's close keeps releasing its jobs.

A close cancels every evaluation it owns, and with the evaluation ledger each cancel
relinquishes the job's ledger request: cluster calls that each wait up to a transport
call's limit, retried when their answer is lost. On a controller that does not answer
that has no end but the retries, so the close bounds the release (``CLOSE_RELEASE_BOUND_S``)
and detaches what is left, as a run that keeps its jobs on a stop does.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sim.api import Sleeper

CLOSE_RELEASE_BOUND_S = 30.0
"""How long a close keeps starting cluster calls to release its jobs, in seconds.

A healthy controller cancels every job well within it. Past it, the close starts no
further release and detaches what is left: each job's record stays resumable, its ledger
lease lapses, and the next run that opens the ledger reconciles it. A call already
running then ends within its transport limit, so a close takes at most this plus one
transport call."""


async def ended_within[T](work: asyncio.Future[T], sleeper: Sleeper, bound_s: float) -> bool:
    """Wait for ``work`` at most ``bound_s`` on ``sleeper``; whether it ended.

    ``work`` is left running past the bound, for the caller to give up as it must. A
    caller cancelled while it waits cancels ``work`` too, as awaiting it would.
    """
    bound = asyncio.ensure_future(sleeper.sleep(bound_s))
    try:
        await asyncio.wait({work, bound}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        raise
    finally:
        bound.cancel()
        await asyncio.gather(bound, return_exceptions=True)
    return work.done()


__all__ = ["CLOSE_RELEASE_BOUND_S", "ended_within"]
