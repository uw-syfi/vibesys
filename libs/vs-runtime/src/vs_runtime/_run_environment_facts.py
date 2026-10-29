"""What each run environment reports to the product for presentation, as plain facts.

Split from ``vs_runtime._run_environment`` (which builds the environments that report
them) so each module stays within the file-length ceiling; ``vs_runtime.api.infrastructure``
exports them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sandbox.api.slurm import profile_capture_descriptor

if TYPE_CHECKING:
    from vs_evaluation.api import ProfileField
    from vs_sandbox.api.skypilot import ResolvedSkyPilotResources
    from vs_sandbox.api.slurm import SlurmEvaluationPlan, SlurmOperatorSettings
    from vs_slurm.api import WorkspaceRootProof


@dataclass(frozen=True)
class HostEnvironmentFacts:
    """Presentation facts for an agent confined on the host."""


@dataclass(frozen=True)
class DockerEnvironmentFacts:
    """Presentation facts for Docker execution.

    Git writability is not among them: it depends on the workspace a session mounts, so
    the session's ``RunEnvironmentView`` reports it from that session's mount plan.
    """


@dataclass(frozen=True)
class ClusterEvaluationFacts:
    """What an environment whose trusted evaluation runs on a Slurm cluster reports.

    ``settings`` is the operator file as the run parsed it once, and ``plan`` the
    evaluation plan the run wrote. Consumers read the cluster's capacity and the
    executor's inputs here instead of re-reading either file. ``root_proof`` is the
    run's workspace-root probe on a local Slurm root, which every Slurm submission
    waits for; ``None`` where the root needs no probe.
    """

    settings: SlurmOperatorSettings
    plan: SlurmEvaluationPlan
    root_proof: WorkspaceRootProof | None = None

    @property
    def evaluation_capacity(self) -> int:
        """How many evaluations the cluster runs at once."""
        return self.settings.config.evaluation_capacity

    @property
    def dedicated_measurement_resources(self) -> bool:
        """Whether each trusted evaluation job holds its nodes alone (``SlurmConfig``)."""
        return self.settings.config.dedicates_measurement_resources

    @property
    def captures_profile(self) -> bool:
        """Whether the cluster runs a profile capture for this run."""
        return self.plan.profile_command is not None

    @property
    def supported_profile_fields(self) -> tuple[ProfileField, ...]:
        """The measurement fields the configured capture can supply, none without a capture.

        The single source for what a run offers to its agents and accepts from them: the
        executor checks a request against this same descriptor.
        """
        return profile_capture_descriptor(self.plan.profile_command).supported_fields


@dataclass(frozen=True)
class AgentGpuFacts:
    """The limits of the agent's own GPU jobs, as the agent is told them."""

    max_gpus: int
    max_time_minutes: int
    #: Host paths the jobs may read besides the workspace and system directories.
    read_only_paths: tuple[str, ...] = ()
    #: ``(name, absolute path)`` of each profiler (``ncu``, ``nsys``) that exists on the host but
    #: not on a job's ``PATH``: a bare ``ncu`` exits 127 in a job, so the agent names the path.
    tool_paths: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SlurmEnvironmentFacts:
    """Presentation facts for a local editor with remote trusted execution.

    ``service_command`` is the operator-configured argv (remote interpreter
    already substituted) that trusted jobs use to start the candidate service
    from the candidate root, or empty when the operator configured none.
    ``gpu_client`` is the name the agent runs, on its ``PATH``, to start its own GPU
    commands as Slurm jobs; it is set when ``agent_gpu`` is. Agents request trusted
    evaluation only through the evaluation tool, so no gate client exists.
    """

    service_command: tuple[str, ...] = ()
    gpu_client: str = ""
    agent_gpu: AgentGpuFacts | None = None


@dataclass(frozen=True)
class SkyPilotEnvironmentFacts:
    """Resolved SkyPilot facts needed by product presentation and opening."""

    resources: ResolvedSkyPilotResources
    runtime_container_path: str = "/opt/vibesys-runtime/environment.md"


@dataclass(frozen=True)
class ModalEnvironmentFacts:
    """Resolved Modal facts needed by product presentation and opening."""

    gpu: str
    app_name: str
    reference_path: str
    runtime_container_path: str = "/opt/vibesys-runtime/environment.md"


RunEnvironmentPresentationFacts = (
    HostEnvironmentFacts
    | DockerEnvironmentFacts
    | SlurmEnvironmentFacts
    | SkyPilotEnvironmentFacts
    | ModalEnvironmentFacts
)
