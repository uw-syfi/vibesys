"""Published test drivers for code that runs a strategy against core.

A fake world answers core's requests (`Answer`), a `Harness` fixes the run's facts, and a
`Journal` records what a run did. They are the pieces a host-side test kit builds on, so a
strategy's tests and the runtime's shell tests answer requests the same way.
"""

from vs_core.testing.drive import (
    Answer,
    Failed,
    Harness,
    Retryable,
    Running,
    Succeeded,
    Unknown,
    new_run,
)
from vs_core.testing.liveness import Journal

__all__ = [
    "Answer",
    "Failed",
    "Harness",
    "Journal",
    "Retryable",
    "Running",
    "Succeeded",
    "Unknown",
    "new_run",
]
