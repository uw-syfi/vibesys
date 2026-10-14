"""Owned in-memory test implementations for :mod:`vs_runtime.api`."""

from vs_runtime._evidence_ledger import FakeEvidenceLedger
from vs_runtime._fake_core_execution import (
    ExecutedRequest,
    FakePublicationDelivery,
    FakeRequestExecution,
)
from vs_runtime._run_clocks import CrashableRunClock, ProbedRunClock
from vs_runtime._run_environment import HostEnvironment
from vs_runtime._runs import FakeRunHandle, FakeRuns
from vs_runtime._shell_kit import (
    LEASE,
    Admitting,
    HostOptions,
    InvalidReply,
    Launch,
    RecordingSearch,
    Run,
    Script,
    ScriptedExecutors,
    ScriptedHost,
    TranslatorFactory,
    drive_shell,
    request_executors,
    scripted_host,
)
from vs_runtime._validation_passes import MemoryValidationPasses
from vs_runtime.fake_docker_daemon import DaemonBackend, daemon_docker_config, daemon_engine
from vs_runtime.fakes import (
    FakeAccuracyCall,
    FakeAgentExecutionEnvironment,
    FakeAgentExecutionLifecycleSink,
    FakeBenchmarkCall,
    FakeCandidateWorkspace,
    FakeCapacityTimer,
    FakeCommandCall,
    FakeCommands,
    FakeEvaluation,
    FakeEvaluationGate,
    FakeGitRunner,
    FakeLocalValidationCall,
    FakeModelVolumeProvisioner,
    FakeModelVolumeRequest,
    FakeProjectMaterializationEffects,
    FakeRunControlEventSink,
    FakeStopTimer,
    FakeTrustedAccuracyCall,
    FakeTrustedBenchmarkCall,
    FakeTrustedEvaluationExecutor,
    FakeTrustedShellCall,
    FakeWorkspace,
    FakeWorkspaces,
)


def unconfined_host_environment() -> HostEnvironment:
    """Return a host environment whose confinement check always passes.

    For tests of what a run does with an agent on the host, on machines that may
    lack the host sandbox. Production code never builds this.
    """
    return HostEnvironment(build_sandbox=lambda *_args, **_kwargs: None)


__all__ = [
    "LEASE",
    "Admitting",
    "CrashableRunClock",
    "DaemonBackend",
    "ExecutedRequest",
    "FakeAccuracyCall",
    "FakeAgentExecutionEnvironment",
    "FakeAgentExecutionLifecycleSink",
    "FakeBenchmarkCall",
    "FakeCandidateWorkspace",
    "FakeCapacityTimer",
    "FakeCommandCall",
    "FakeCommands",
    "FakeEvaluation",
    "FakeEvaluationGate",
    "FakeEvidenceLedger",
    "FakeGitRunner",
    "FakeLocalValidationCall",
    "FakeModelVolumeProvisioner",
    "FakeModelVolumeRequest",
    "FakeProjectMaterializationEffects",
    "FakePublicationDelivery",
    "FakeRequestExecution",
    "FakeRunControlEventSink",
    "FakeRunHandle",
    "FakeRuns",
    "FakeStopTimer",
    "FakeTrustedAccuracyCall",
    "FakeTrustedBenchmarkCall",
    "FakeTrustedEvaluationExecutor",
    "FakeTrustedShellCall",
    "FakeWorkspace",
    "FakeWorkspaces",
    "HostOptions",
    "InvalidReply",
    "Launch",
    "MemoryValidationPasses",
    "ProbedRunClock",
    "RecordingSearch",
    "Run",
    "Script",
    "ScriptedExecutors",
    "ScriptedHost",
    "TranslatorFactory",
    "daemon_docker_config",
    "daemon_engine",
    "drive_shell",
    "request_executors",
    "scripted_host",
    "unconfined_host_environment",
]
