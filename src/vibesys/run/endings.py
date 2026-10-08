"""Turn a run's derived ending into the terminal status its plugin returns.

``RunEnding`` (``vibesys.hypothesis``) is derived from the durable records. This is the
one place that maps it to ``RunStatus`` and tells the operator which ending it was, so
no orchestration returns a status it did not derive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.hypothesis import RunEnding
from vs_runtime.api import RunStatus

if TYPE_CHECKING:
    from vs_runtime.api import Run


def conclude(run: Run, ending: RunEnding) -> RunStatus:
    """Publish ``ending`` to the operator and return the terminal status it implies."""
    match ending:
        case RunEnding.ADOPTED:
            pass
        case RunEnding.NO_IMPROVEMENT:
            run.observations.note("no candidate improved on the input; kept the measured input")
        case RunEnding.NO_TRUSTED_RESULT:
            run.observations.warning(
                "no trusted measurement was recorded, so the run has no result to keep"
            )
    return RunStatus.SUCCEEDED if ending.succeeded else RunStatus.FAILED


__all__ = ["conclude"]
