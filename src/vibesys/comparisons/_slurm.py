"""Sample executors that run a comparison's job on the operator's Slurm cluster."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime.api import render_stage_failure
from vs_runtime.api.comparisons import ComparisonUnavailableError
from vs_runtime.api.infrastructure import SemanticSlurmEvaluationExecutor
from vs_sandbox.api.slurm import (
    BaselineServer,
    ComparisonNotConfiguredError,
    comparison_policy,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_evaluation.api import EvaluationStateNamespace
    from vs_runtime.api.comparisons import ComparisonExecutor, ComparisonSubject
    from vs_runtime.api.infrastructure import MeasuredCheckouts, TrustedEvaluationPlan
    from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmOperatorSettings
    from vs_sim.api import Clock, Processes, Sleeper
    from vs_slurm.api import Cluster


class SlurmSampleExecutors:
    """One semantic Slurm executor per sample, serving the comparison's engine in its image.

    The executor is the one a candidate's measurement uses; the operator's policy has its
    service swapped for the engine's, and a sample that must avoid nodes carries the cluster's
    ``--exclude`` for them.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-930096 [PLR0913]; independent injected ports and plans of one executor family
        self,
        settings: SlurmOperatorSettings,
        evaluation: SlurmEvaluationPlan,
        trusted: TrustedEvaluationPlan,
        checkouts: MeasuredCheckouts,
        namespace: EvaluationStateNamespace,
        handle_root: Path,
        *,
        clock: Clock,
        sleeper: Sleeper,
        processes: Processes | None = None,
        cluster: Cluster | None = None,
    ) -> None:
        """Bind the operator's settings and the plans every sample's job runs."""
        self._settings = settings
        self._evaluation = evaluation
        self._trusted = trusted
        self._checkouts = checkouts
        self._namespace = namespace
        self._handle_root = handle_root
        self._clock = clock
        self._sleeper = sleeper
        self._processes = processes
        self._cluster = cluster

    def executor_for(
        self, subject: ComparisonSubject, *, avoiding: tuple[str, ...]
    ) -> ComparisonExecutor:
        """The executor of one sample of *subject*; refuses what this machine cannot serve."""
        policy = self._settings.policy
        machine = policy.comparison
        if machine is None:
            message = "the operator's Slurm file has no [vibesys.comparison] table"
            raise ComparisonUnavailableError(message)
        spec = subject.spec
        if spec.image is None:
            message = (
                f"comparison {spec.name!r} builds its image from a Dockerfile, which the "
                "Slurm run environment does not do; publish the image and pin its digest"
            )
            raise ComparisonUnavailableError(message)
        server = BaselineServer(
            image=spec.image,
            argv=spec.serve,
            env=spec.env,
            ready_timeout_seconds=spec.ready_timeout_seconds,
            benchmark_args=spec.benchmark_args,
        )
        try:
            overlaid = comparison_policy(policy, machine, server)
        except ComparisonNotConfiguredError as error:
            raise ComparisonUnavailableError(str(error)) from error
        config = self._settings.config
        if avoiding:
            config = config.model_copy(
                update={
                    "sbatch_arguments": (
                        *config.sbatch_arguments,
                        f"--exclude={','.join(avoiding)}",
                    )
                }
            )
        return SemanticSlurmEvaluationExecutor(
            config,
            overlaid,
            self._evaluation,
            self._trusted,
            self._checkouts,
            self._namespace,
            self._handle_root,
            stage_failure_text=render_stage_failure,
            clock=self._clock,
            sleeper=self._sleeper,
            processes=self._processes,
            cluster=self._cluster,
        )
