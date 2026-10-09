"""Owned in-memory test implementations for :mod:`vs_runtime.api`."""

from vs_runtime._evidence_ledger import FakeEvidenceLedger
from vs_runtime._fake_agent_sessions import FakeAgentSession, TurnResponder
from vs_runtime._fake_core_execution import (
    ExecutedRequest,
    FakePublicationDelivery,
    FakeRequestExecution,
)
from vs_runtime._run_environment import HostEnvironment
from vs_runtime._runs import FakeRunHandle, FakeRuns
from vs_runtime.fakes import (
    FakeAccuracyCall,
    FakeAgentExecutionEnvironment,
    FakeAgentExecutionLifecycleSink,
    FakeBenchmarkCall,
    FakeCandidateWorkspace,
    FakeCapacityTimer,
    FakeCommandCall,
    FakeCommands,
    FakeControl,
    FakeEvaluation,
    FakeEvaluationGate,
    FakeGitRunner,
    FakeLocalValidationCall,
    FakeModelVolumeProvisioner,
    FakeModelVolumeRequest,
    FakeObservations,
    FakeProfileCall,
    FakeProjectMaterializationEffects,
    FakeRun,
    FakeRunControlEventSink,
    FakeSkills,
    FakeState,
    FakeStateCommit,
    FakeStopTimer,
    FakeTrustedAccuracyCall,
    FakeTrustedBenchmarkCall,
    FakeTrustedEvaluationExecutor,
    FakeTrustedShellCall,
    FakeWorkspace,
    FakeWorkspaceAgentSessions,
    FakeWorkspaces,
    ObservationCall,
)


def unconfined_host_environment() -> HostEnvironment:
    """Return a host environment whose confinement check always passes.

    For tests of what a run does with an agent on the host, on machines that may
    lack the host sandbox. Production code never builds this.
    """
    return HostEnvironment(build_sandbox=lambda *_args, **_kwargs: None)


__all__ = [
    "ExecutedRequest",
    "FakeAccuracyCall",
    "FakeAgentExecutionEnvironment",
    "FakeAgentExecutionLifecycleSink",
    "FakeAgentSession",
    "FakeBenchmarkCall",
    "FakeCandidateWorkspace",
    "FakeCapacityTimer",
    "FakeCommandCall",
    "FakeCommands",
    "FakeControl",
    "FakeEvaluation",
    "FakeEvaluationGate",
    "FakeEvidenceLedger",
    "FakeGitRunner",
    "FakeLocalValidationCall",
    "FakeModelVolumeProvisioner",
    "FakeModelVolumeRequest",
    "FakeObservations",
    "FakeProfileCall",
    "FakeProjectMaterializationEffects",
    "FakePublicationDelivery",
    "FakeRequestExecution",
    "FakeRun",
    "FakeRunControlEventSink",
    "FakeRunHandle",
    "FakeRuns",
    "FakeSkills",
    "FakeState",
    "FakeStateCommit",
    "FakeStopTimer",
    "FakeTrustedAccuracyCall",
    "FakeTrustedBenchmarkCall",
    "FakeTrustedEvaluationExecutor",
    "FakeTrustedShellCall",
    "FakeWorkspace",
    "FakeWorkspaceAgentSessions",
    "FakeWorkspaces",
    "ObservationCall",
    "TurnResponder",
    "unconfined_host_environment",
]
