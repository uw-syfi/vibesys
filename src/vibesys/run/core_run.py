"""Drive one core run with the runtime's production loop over the host's services.

``drive_core_run`` is the one place a core run's services reach the loop. It builds the
shell over the run's durable store (a fresh run when the store is empty, a recovery of
the committed envelope otherwise), joins it with the publication delivery, the run clock
and the host's control channel, then runs ``vs_runtime``'s loop to the run's terminal
status and maps that outcome to the status a plugin returns. It chooses no policy: the
strategy, limits and deadline were fixed by composition (``CoreServices``).
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

from vs_core.api import RunResultProposal
from vs_runtime.api import RunStatus
from vs_runtime.api.core import (
    CoreRunHost,
    CoreRuntime,
    JournalPublicationDelivery,
    RunControlBridge,
    RunLoopConfig,
    drive_core,
    start_core,
)
from vs_runtime.api.infrastructure import RunStopped

if TYPE_CHECKING:
    from vibesys.run.host import CoreHost
    from vibesys.run.integration import LocalRunIntegration
    from vs_core.api import ArtifactRef
    from vs_runtime.api import ArtifactStore
    from vs_runtime.api.core import ExecutorRefusal

# How long the run's state-store lease is valid without renewal. The loop renews it every
# third of this, so a host that dies is replaced after at most this long.
LEASE_SECONDS = 60.0

_STOP_RESULT = RunResultProposal(outcome="cancelled", reason="stop requested by the operator")


class CoreRunRefusedError(RuntimeError):
    """An executor the run needs is not composed, so the run cannot make progress."""

    def __init__(self, run_id: str, refusal: ExecutorRefusal) -> None:
        """Name the run, the request and the executor role that refused."""
        self.refusal = refusal
        super().__init__(
            f"run {run_id!r}: no executor for {refusal.role.value} request "
            f"{refusal.request_id.root!r}: {refusal.detail}"
        )


class _SteerTexts:
    """Where steer text lives: the run's content-addressed artifact store.

    A steer control carries only the digest of its text, so the text must be readable by
    that digest before the control is submitted.
    """

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    def put(self, ref: ArtifactRef, text: str) -> None:
        """Store the text; its content address must be the digest the control carries."""
        receipt = self._artifacts.write(text.encode())
        if receipt.sha256 != ref.digest:
            message = f"steer artifact {ref.artifact_id.root!r} stored under {receipt.sha256}"
            raise RuntimeError(message)


def _loop_config(run_id: str) -> RunLoopConfig:
    # One lease holder per process: a restart of the same run is a new host.
    return RunLoopConfig(
        host_id=f"vibesys:{run_id}:{os.getpid()}:{uuid.uuid4().hex[:8]}",
        lease_duration=LEASE_SECONDS,
    )


async def drive_core_run(host: CoreHost, integration: LocalRunIntegration) -> RunStatus:
    """Run one core run to its terminal status over ``host.services``.

    Raises ``RunStopped`` when the operator's stop ended the run, so the session reports
    a stopped run, and ``CoreRunRefusedError`` when composition left an executor role
    unserved.
    """
    services = host.services
    shell = CoreRuntime(
        services.store, services.strategy, services.state, bindings=services.bindings
    )
    delivery = JournalPublicationDelivery(
        services.publications, services.bindings.registry, services.store
    )
    controls = RunControlBridge(
        integration.control,
        _SteerTexts(services.artifacts),
        stop_result=_STOP_RESULT,
    )
    loop = CoreRunHost(shell, delivery, services.clock, controls)
    config = _loop_config(services.run_id)
    start_core(loop, config)
    outcome = await drive_core(loop, config)
    if outcome.refusal is not None:
        raise CoreRunRefusedError(services.run_id, outcome.refusal)
    result = outcome.result
    if result is not None and result.outcome == "success":
        return RunStatus.SUCCEEDED
    if result is not None and result == _STOP_RESULT:
        raise RunStopped
    return RunStatus.FAILED


__all__ = ["LEASE_SECONDS", "CoreRunRefusedError", "drive_core_run"]
