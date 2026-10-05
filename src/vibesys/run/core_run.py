"""The seam between the product host and the runtime's production core run loop.

``drive_core_run`` is the one place a core run's services reach the loop. The loop
receives what ``CoreServices`` holds: the fresh core state, the executor bindings,
the durable store and publication namespace, the strategy, plus the host's control
channel and a monotonic clock. It returns the run's terminal status.

The runtime's production loop (``feat/runtime-core-run-loop``, SW-1) has not landed,
so this seam fails loudly instead of choosing a substitute: a core run must never be
driven by test scaffolding or fall back to the legacy loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.run.host import CoreHost
    from vibesys.run.integration import LocalRunIntegration
    from vs_runtime.api import RunStatus


class CoreRunLoopUnavailableError(NotImplementedError):
    """The runtime ships no production core run loop yet."""

    def __init__(self, run_id: str) -> None:
        """Name the run that could not start."""
        super().__init__(
            f"run {run_id!r}: vs_runtime has no production core run loop yet "
            "(SW-1, branch feat/runtime-core-run-loop)"
        )


async def drive_core_run(host: CoreHost, integration: LocalRunIntegration) -> RunStatus:
    """Run one core run to its terminal status over ``host.services``."""
    del integration
    raise CoreRunLoopUnavailableError(host.services.run_id)


__all__ = ["CoreRunLoopUnavailableError", "drive_core_run"]
