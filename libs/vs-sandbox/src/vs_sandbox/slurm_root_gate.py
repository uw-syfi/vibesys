"""The gate a Slurm evaluation passes before its first submission: the run's root proof.

A local Slurm root must be proven shared with the compute nodes before any job uses
it (``vs_slurm.WorkspaceRootProof``). An evaluation that has not been dispatched waits
here for that proof; one that may already have a job never waits, since refusing it
would disown the job. A proof that failed for good refuses the evaluation as
:class:`RunEnvironmentUnavailableError`: the run environment can run no Slurm work,
which no retry of the measurement can change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_slurm.api import SlurmSubmissionRejectedError, SlurmWorkspaceRootProbeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_slurm.api import WorkspaceRootProof


class RunEnvironmentUnavailableError(SlurmSubmissionRejectedError):
    """Nothing was sent: the run's workspace root failed its probe for good."""


async def await_root_proof(
    proof: WorkspaceRootProof | None,
    *,
    dispatched: bool,
    sleep: Callable[[float], Awaitable[None]],
    poll_interval: float,
) -> None:
    """Hold a first submission until the root is proven; never one that may have a job.

    Raises :class:`RunEnvironmentUnavailableError` once the proof failed for good, and a
    plain ``SlurmSubmissionRejectedError`` when the run stopped the probe first.
    """
    if proof is None or dispatched:
        return  # a dispatched evaluation reconciles its job; refusing would disown it
    try:
        while not proof.check():
            await sleep(poll_interval)
    except SlurmWorkspaceRootProbeError as exc:
        # Nothing was sent: a definite refusal, recorded and published as one.
        if exc.retryable:
            raise SlurmSubmissionRejectedError(str(exc)) from exc
        raise RunEnvironmentUnavailableError(str(exc)) from exc
