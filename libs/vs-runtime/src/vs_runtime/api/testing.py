"""Owned in-memory test implementations for :mod:`vs_runtime.api`."""

from vs_runtime._environment_system import EnvironmentSystem
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
from vs_sim.api import Processes
from vs_sim.api.testing import NO_PROCESSES, FakePipedProcessLauncher, SimNetwork, SimThreads


def unconfined_host_environment() -> HostEnvironment:
    """Return a host environment whose confinement check always passes.

    For tests of what a run does with an agent on the host, on machines that may
    lack the host sandbox. Production code never builds this.
    """
    return HostEnvironment(build_sandbox=lambda *_args, **_kwargs: None)


def idle_environment_system(slurm_processes: Processes = NO_PROCESSES) -> EnvironmentSystem:
    """Return a host on which nothing starts: every process start is refused.

    Its threads and sockets are simulated and nothing drives them, so a test whose
    environment starts a broker or a program fails instead of reaching the real host.
    *slurm_processes* answers the Slurm programs of a test that scripts them.
    """
    threads = SimThreads()
    return EnvironmentSystem(
        threads=threads,
        network=SimNetwork(threads),
        processes=NO_PROCESSES,
        launcher=FakePipedProcessLauncher(),
        slurm_processes=slurm_processes,
    )


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
    "idle_environment_system",
    "request_executors",
    "scripted_host",
    "unconfined_host_environment",
]
