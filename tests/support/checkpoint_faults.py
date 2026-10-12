"""A workspace executor that declines or fails checkpoints, over any scripted run.

Core publishes a turn's suspension only after the turn's workspace is checkpointed, and an
interruption completes only after the same. Whatever the executor answers to that checkpoint
request, the wait it gates must end: the turn is suspended, or fails with a named reason. This
wrapper answers the n-th checkpoint request of a run the way ``answers`` says (cycling) and
leaves every other request to the wrapped script.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import strategies as st

from vs_core.api import SnapshotAndRetain, SnapshotAndRetainRun
from vs_core.testing.drive import Failed

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer
    from vs_runtime.api.testing import Script

CHECKPOINT_ANSWERS = ("succeeded", "failed")
"""``succeeded`` is the wrapped script's own answer; ``failed`` is the executor's fault.

``retryable`` is deliberately not generated: a retryable checkpoint answer makes the shell spin
without ever calling an executor again, with or without this fix (filed separately).
"""

CHECKPOINTS = st.lists(st.sampled_from(CHECKPOINT_ANSWERS), min_size=1, max_size=4).map(tuple)


class FaultyCheckpoints:
    """The wrapped script, except that checkpoint requests are answered per ``answers``."""

    def __init__(self, inner: Script, answers: tuple[str, ...]) -> None:
        """Wrap ``inner``; the n-th checkpoint request gets ``answers[n % len(answers)]``."""
        self._inner = inner
        self._answers = answers
        self._seen = 0

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """The faulty answer for a checkpoint request, else the wrapped script's."""
        if isinstance(request, SnapshotAndRetain | SnapshotAndRetainRun) and self._answers:
            answer = self._answers[self._seen % len(self._answers)]
            self._seen += 1
            if answer == "failed":
                return Failed()
        return self._inner(request, core)


def with_checkpoints(script: Script, answers: tuple[str, ...]) -> Script:
    """``script`` with its checkpoint requests answered per ``answers``; unchanged if empty."""
    return FaultyCheckpoints(script, answers) if answers else script
