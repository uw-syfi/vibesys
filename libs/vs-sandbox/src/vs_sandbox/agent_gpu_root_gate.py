"""Hold the agent's GPU jobs until the run's local Slurm root is proven shared.

An agent GPU job runs a command in the agent's workspace on a compute node, so it
depends on the root the run's workspace-root probe proves. The run starts without
waiting for that probe; :class:`RootGatedGpuLauncher` makes each GPU command wait
for it on the submit host instead, before anything reaches ``srun``, and refuses
the command with the probe's error once the probe failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vs_sandbox.agent_gpu import GpuJobOutcome
from vs_sandbox.agent_gpu_cap import CANCELLED_BEFORE_SUBMISSION
from vs_slurm.api import SlurmWorkspaceRootProbeError

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.agent_gpu import GpuCommand, GpuJobRequest, GpuLauncher
    from vs_sim.api import Event
    from vs_slurm.api import WorkspaceRootProof


@runtime_checkable
class _GpuJobReconciler(Protocol):
    def reconcile(self, job_name: str) -> bool: ...


class RootGatedGpuLauncher:
    """A :class:`~vs_sandbox.agent_gpu.GpuLauncher` that submits nothing to an unproven root.

    ``run`` waits for *proof* before handing the command to *inner*: a request
    cancelled while it waits is never submitted and returns
    :data:`~vs_sandbox.agent_gpu_cap.CANCELLED_BEFORE_SUBMISSION`, and a failed
    proof returns a launch failure carrying the probe's error, with nothing
    submitted. ``reconcile`` settles an earlier job and is never held: it only
    cancels.
    """

    def __init__(self, inner: GpuLauncher, proof: WorkspaceRootProof) -> None:
        """Gate *inner*, the run's launcher, on *proof*, the run's workspace-root probe."""
        self._inner = inner
        self._proof = proof

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> GpuJobOutcome:
        """Wait for the proven root, then run *command* through the wrapped launcher."""
        try:
            if not self._proof.require(cancel):
                return GpuJobOutcome(CANCELLED_BEFORE_SUBMISSION)
        except SlurmWorkspaceRootProbeError as error:
            return GpuJobOutcome(1, failure=error)
        return self._inner.run(request, command, write=write, cancel=cancel)

    def reconcile(self, job_name: str) -> bool:
        """Reconcile an earlier job through the wrapped launcher, if it can."""
        if not isinstance(self._inner, _GpuJobReconciler):
            return False
        return self._inner.reconcile(job_name)


__all__ = ["RootGatedGpuLauncher"]
