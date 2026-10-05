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

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vs_core.api import RetainedCandidate, RunResultProposal
from vs_project.api import Project
from vs_runtime.api import RunStatus
from vs_runtime.api.core import (
    CoreResumeError,
    CoreRunHost,
    CoreRuntime,
    JournalPublicationDelivery,
    RunControlBridge,
    RunLoopConfig,
    drive_core,
    reject_legacy_resume,
    start_core_awaiting_lease,
)
from vs_runtime.api.infrastructure import RunStopped

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.run.host import CoreHost
    from vibesys.run.integration import LocalRunIntegration
    from vs_core.api import ArtifactRef
    from vs_runtime.api import ArtifactStore
    from vs_runtime.api.core import ExecutorRefusal

_STOP_RESULT = RunResultProposal(outcome="cancelled", reason="stop requested by the operator")
_DEADLINE_RESULT = RunResultProposal(outcome="cancelled", reason="run deadline reached")


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


def _loop_config(run_id: str, lease_seconds: float) -> RunLoopConfig:
    # One lease holder per process: a restart of the same run is a new host.
    return RunLoopConfig(
        host_id=f"vibesys:{run_id}:{os.getpid()}:{uuid.uuid4().hex[:8]}",
        lease_duration=lease_seconds,
        stop_result=_STOP_RESULT,
        deadline_result=_DEADLINE_RESULT,
    )


def ensure_not_legacy_resume(project_root: Path, run_id: str) -> None:
    """Reject resuming a run that the legacy dynamic loop created, before any resource opens.

    The core cannot continue a legacy run's state, and starting a fresh run under its
    identity would discard that run's history. The error names the run and the file
    that marks it as legacy.
    """
    try:
        reject_legacy_resume(Project.open(project_root), run_id)
    except CoreResumeError as error:
        diagnostic = error.diagnostic
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code=diagnostic.code,
                stage=diagnostic.stage,
                message=(
                    f"cannot resume run {run_id!r}: it was created by the legacy dynamic loop "
                    f"({diagnostic.path}, schema {diagnostic.source_schema}); "
                    "start a new run instead"
                ),
            )
        ) from error


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
    config = _loop_config(services.run_id, host.timing.lease_seconds)
    await start_core_awaiting_lease(loop, config)
    bridge = services.agent_evaluation
    if bridge is not None:
        bridge.attach(shell, services.clock)
        await bridge.serve()
    try:
        outcome = await drive_core(loop, config)
    finally:
        try:
            if bridge is not None:
                await bridge.close()
        finally:
            # A crashed process cannot reach this line, so its restart waits out the lease
            # (see ``start_core_awaiting_lease``); any return or raise here frees it.
            shell.release_lease(now_at=services.clock.now())
    if outcome.refusal is not None:
        raise CoreRunRefusedError(services.run_id, outcome.refusal)
    return _status_of(outcome.result, shell)


def _status_of(result: RunResultProposal | None, shell: CoreRuntime) -> RunStatus:
    """Map the run's recorded result to the status a plugin returns.

    The kernel keeps the first stop result, so a run that reaches its deadline records
    "cancelled" even when its strategy drained into an adopted candidate. A time budget
    is the normal way an optimization run ends, so a deadline that found a verified
    retained candidate is a success; with none it is a failure. An operator stop is
    reported as stopped.
    """
    if result is None:
        return RunStatus.FAILED
    if result == _STOP_RESULT:
        raise RunStopped
    kept = result == _DEADLINE_RESULT and _adopted_retained_candidate(shell)
    return RunStatus.SUCCEEDED if result.outcome == "success" or kept else RunStatus.FAILED


def _adopted_retained_candidate(shell: CoreRuntime) -> bool:
    """Whether core verified the adoption of a retained candidate."""
    adoption = shell.record.envelope.core.settlement.adoption
    return (
        adoption is not None
        and adoption.verified
        and isinstance(adoption.selection, RetainedCandidate)
    )


__all__ = ["CoreRunRefusedError", "drive_core_run", "ensure_not_legacy_resume"]
