"""When a test re-creates the host between steps.

A restart after every step equals the live run: vs-core proves that once, generically, over
generated runs (``test_flows_properties``, ``test_recovery_converges``, ``test_flows_restart``).
A rule's own test therefore steps live at pull-request depth and reloads between steps only at
nightly depth, where the whole suite is a restart sweep.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from vs_sim.api.testing import Depth, depth

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_core.api import RequestRecord

_live = 0
"""How many ``live_steps`` blocks are open."""


def restarts() -> bool:
    """Whether tests reload the state between steps (nightly depth)."""
    return depth() is Depth.NIGHTLY and not _live


@contextmanager
def live_steps() -> Iterator[None]:
    """Step live even at nightly depth, for a property that generates its own reloads.

    A reload after every step costs about three times the live step and grows with the
    ledger, so a property that already reloads at a generated point (and checks the reloaded
    state) takes its nightly search from more examples, not from reloading every step.
    """
    global _live  # noqa: PLW0603
    _live += 1
    try:
        yield
    finally:
        _live -= 1


def unaddressed(record: RequestRecord) -> RequestRecord:
    """*record* apart from the reconciliation commands sent about it (``addressed_by``).

    Recovery sends inspections and cancels about the records it reconciles; a test of what it
    does to a record's own facts compares the rest. The addressing is tested on its own.
    """
    return record.model_copy(update={"addressed_by": ()})
