"""The public surface of VibeSys core: the only module other packages import.

Everything else under `vibesys.*` is private to core + `entrypoints`. `server.*`
talks to core only through this module: no deep imports, no escape hatch.
`entrypoints`, as the composition root that assembles a `RunRequest`, still
reaches some core internals directly.

Most symbols here are contracts and Protocols; the rest are re-exports of
core-owned types that `server.*` legitimately needs (events, control signals,
the resource-handoff seam) so it never has to import their private home modules.
"""

from __future__ import annotations

from vibesys import boot_trace
from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.api._readmodel import project_committed_run_view
from vibesys.api.composition import experiment_origin_matches, supported_profilers
from vibesys.api.contracts import (
    AgentEnvironment,
    CandidateDisposition,
    Config,
    ConfigurationDiagnostic,
    ConfigurationError,
    CoreEvent,
    EventSink,
    EventStatus,
    HypothesisRoundView,
    HypothesisView,
    LoopKind,
    MetricSpace,
    Objective,
    PerfDeltaReason,
    ResumeRef,
    RoundView,
    RunRequest,
    RunResult,
    RunStatus,
    RunView,
)
from vibesys.api.entry import default_request, load_config, validate
from vibesys.api.session import (
    RunAgentHost,
    RunControl,
    RunQuery,
    RunSession,
    RunWorkspace,
    create_session,
)
from vibesys.api.store import RunStore, open_run_store
from vibesys.constants import KNOWN_COMPUTE_BACKENDS, PROJECT_ROOT, ComputeBackend, DomainName
from vibesys.evaluators.input_manifest import InputBundle, load_input_bundle, load_project_task
from vibesys.evaluators.input_synthesis import (
    InputSynthesisError,
    SynthesizedInputSpec,
    synthesize_input_bundle,
)
from vibesys.evaluators.objective import load_objective, with_operator_constraints
from vibesys.events import AgentExecutionStartedData, CoreEventType
from vibesys.loops.agent.issue_board import framework_memory_paths
from vibesys.loops.evolve.search_policy import OpenEvolveSearchConfig
from vibesys.profilers import CLI_PROFILER_CHOICES, ProfilerKind, coerce_profiler_kind
from vibesys.render.headless import HeadlessRenderer
from vibesys.render.sink import output_sink
from vibesys.repository import (
    REPOSITORY_SLUG,
    RepositoryVisibility,
    generate_experiment_name,
    repository_name_from_experiment,
    validate_experiment_name,
)
from vibesys.resource_paths import default_skill_roots
from vibesys.run import RunLogger
from vibesys.run.integration import RunResourceHandoff
from vibesys.run.run_control import RunStopped
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
    run_environment_record,
)
from vibesys.sandbox.task_image import build_task_image
from vibesys.skills import resolve_skill_source_dirs

__all__ = [
    "CLI_PROFILER_CHOICES",
    "KNOWN_COMPUTE_BACKENDS",
    "PROJECT_ROOT",
    "REPOSITORY_SLUG",
    "AgentEnvironment",
    "AgentExecutionStartedData",
    "CandidateDisposition",
    "ComputeBackend",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "CoreEventType",
    "DomainName",
    "EventSink",
    "EventStatus",
    "HeadlessRenderer",
    "HypothesisRoundView",
    "HypothesisView",
    "InputBundle",
    "InputSynthesisError",
    "LoopKind",
    "MetricSpace",
    "Objective",
    "OpenEvolveSearchConfig",
    "PerfDeltaReason",
    "ProfilerKind",
    "RepositoryVisibility",
    "ResumeRef",
    "RoundView",
    "RunAgentHost",
    "RunControl",
    "RunEnvironmentSpec",
    "RunLogger",
    "RunQuery",
    "RunRequest",
    "RunResourceHandoff",
    "RunResult",
    "RunSession",
    "RunStatus",
    "RunStopped",
    "RunStore",
    "RunView",
    "RunWorkspace",
    "SynthesizedInputSpec",
    "agent_spec_from_config",
    "boot_trace",
    "build_task_image",
    "coerce_profiler_kind",
    "create_session",
    "default_request",
    "default_skill_roots",
    "experiment_origin_matches",
    "framework_memory_paths",
    "generate_experiment_name",
    "load_config",
    "load_input_bundle",
    "load_objective",
    "load_project_task",
    "make_run_environment_spec",
    "open_run_store",
    "output_sink",
    "project_committed_run_view",
    "repository_name_from_experiment",
    "resolve_skill_source_dirs",
    "run_environment_record",
    "supported_profilers",
    "synthesize_input_bundle",
    "validate",
    "validate_experiment_name",
    "with_operator_constraints",
]
