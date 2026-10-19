"""The run environment's lasting failure, which ends the run as failed.

Some failures of the run environment leave the run nothing to do: a local Slurm root
its probe could not prove shared refuses every measurement and every agent GPU job.
The environment calls :meth:`RunEnvironmentFailure.fail` once; the run then stops at
once (a cancel) and ends as a failure that names the cause
(``RunFailureKind.RUN_ENVIRONMENT_UNAVAILABLE``) rather than as an operator stop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import StopMode

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import RunControlChannel

__all__ = ["RunEnvironmentFailure"]


class RunEnvironmentFailure:
    """Holds why the run environment can run none of the run's work, once it failed."""

    def __init__(self, control: RunControlChannel) -> None:
        """Stop the run through ``control`` when the environment fails."""
        self._control = control
        self._reason: str | None = None

    @property
    def reason(self) -> str | None:
        """The environment's own account of its failure, or None while it works."""
        return self._reason

    def fail(self, reason: str) -> None:
        """Record the first failure and stop the run; a later failure changes nothing."""
        if self._reason is not None:
            return
        # The reason is set before the stop, so the run that the stop ends reads it.
        self._reason = reason
        self._control.request_stop(StopMode.CANCEL)
