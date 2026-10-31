"""What the run environment decides about the step runners: who ends an agent's work, who runs a job.

An agent step is cleared by the environment its agent runs in (a Docker sweep, a process table),
and a measurement step by the executor the environment evaluates on: a Slurm job is found again by
the evaluation ledger, a local evaluation by the stage processes and containers it left. SkyPilot
and Modal run the evaluation helper inside the agent's turn, so no measurement step reaches them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.run.step_runners import RunnerSeams
from vs_runtime.api.evaluation_step_runners import (
    EachStageWorkReaper,
    HostStageWork,
    LocalJobStepRunner,
    SlurmJobStepRunner,
    SlurmScheduler,
)
from vs_sandbox.api import DockerStepContainers, SubprocessDockerCli

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.run.core_services import ClosableEvaluation
    from vs_project.api import StateNamespace
    from vs_runtime.api.core import JobRegistry
    from vs_runtime.api.infrastructure import (
        EnvironmentSystem,
        RunEnvironment,
        SemanticSlurmEvaluationExecutor,
    )
    from vs_runtime.api.step_runner import StepRunner
    from vs_sim.api import BlockingRunner, Clock

NO_STEP_EVALUATION = frozenset({"skypilot", "modal"})
"""The environments whose agent runs the evaluation helper inside its turn."""


def step_seams(  # noqa: PLR0913  # lint-waiver: LW-263519 [PLR0913]; each argument is an independent port or fact of the one environment this builds seams for.
    # > A settings object would be built at the one call site and unpacked in the same
    # > function, so it adds a type without hiding anything.
    *,
    run_id: str,
    environment: RunEnvironment,
    kind: str,
    system: EnvironmentSystem,
    blocking: BlockingRunner,
    namespace: Callable[[str], StateNamespace],
    log: Callable[[str], None],
    evaluation: ClosableEvaluation,
    slurm: SemanticSlurmEvaluationExecutor | None,
    poll_interval_s: float,
) -> RunnerSeams:
    """The seams of a run on ``environment`` (of ``kind``) that evaluates on ``evaluation`` or ``slurm``."""
    agent_work = environment.agent_work_reaper(
        run_id, system=system, blocking=blocking, local_namespace=namespace, log=log
    )
    if kind in NO_STEP_EVALUATION:
        return RunnerSeams(agent_work=agent_work, jobs=None)
    if slurm is not None:
        return RunnerSeams(
            agent_work=agent_work, jobs=_slurm_jobs(slurm, evaluation, blocking, poll_interval_s)
        )
    containers = DockerStepContainers(
        SubprocessDockerCli(processes=system.processes, launcher=system.launcher), run_id
    )
    stage_work = HostStageWork(
        system.process_table, containers if kind == "docker" else None, run_id, blocking
    )

    def local(clock: Clock, registry: JobRegistry) -> StepRunner:
        return LocalJobStepRunner.on_host(
            evaluation, stage_work, registry, clock=clock, poll_interval_s=poll_interval_s
        )

    return RunnerSeams(agent_work=agent_work, jobs=local)


def _slurm_jobs(
    slurm: SemanticSlurmEvaluationExecutor,
    evaluation: ClosableEvaluation,
    blocking: BlockingRunner,
    poll_interval_s: float,
) -> Callable[[Clock, JobRegistry], StepRunner]:
    """The job runner of a cluster run: adopt a job through the ledger, or end it and run again.

    A run whose operator configured ``[vibesys.ledger]`` finds a job its dead process left by the
    request's key and attaches to it. Without a ledger nothing names the job but its handle, so
    ``clear`` cancels that handle (the executor persists it) and the step runs on a new job. No
    local process or container is involved, so there is no stage work to reap.
    """
    ledger = slurm.ledger

    def jobs(clock: Clock, registry: JobRegistry) -> StepRunner:
        if ledger is None:
            return LocalJobStepRunner(
                evaluation,
                EachStageWorkReaper(),
                registry,
                clock=clock,
                poll_interval_s=poll_interval_s,
            )
        return SlurmJobStepRunner(
            slurm,
            SlurmScheduler(ledger, slurm.cluster, blocking),
            registry,
            clock=clock,
            poll_interval_s=poll_interval_s,
        )

    return jobs
