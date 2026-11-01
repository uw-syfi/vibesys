"""Owned in-memory test implementations for :mod:`vs_runtime.api`."""

from vs_runtime._control_schema import UnsafeControlSchemaError, control_schema_fields
from vs_runtime._counter_search import CounterConfig, counter
from vs_runtime._environment_system import EnvironmentSystem
from vs_runtime._evidence_ledger import FakeEvidenceLedger
from vs_runtime._fake_stage_work_reaper import FakeStageWorkReaper
from vs_runtime._fake_step_log import (
    FakeQuery,
    FakeRunRecords,
    FakeRunSeed,
    FakeStepLog,
    fake_run_seed,
)
from vs_runtime._fake_step_runner import (
    AttemptHangs,
    AttemptHolds,
    AttemptRaises,
    AttemptReturns,
    Behavior,
    FakeStepRunner,
    LeftoverWorkError,
    OverlappingCallError,
    RunnerCall,
    RunnerMethod,
    StepTagMismatchError,
    StillRunningAfterCancelError,
    finished_result,
)
from vs_runtime._holding_step_runner import HoldAt, HoldingStepRunner
from vs_runtime._run_clocks import CrashableRunClock, ProbedRunClock
from vs_runtime._run_environment import HostEnvironment
from vs_runtime._runs import FakeRunHandle, FakeRuns
from vs_runtime._shell_kit import (
    HostOptions,
    InvalidReply,
    Launch,
    RecordingSearch,
    Run,
    Script,
    ScriptedExecutors,
)
from vs_runtime._validation_passes import MemoryValidationPasses
from vs_runtime.api.stage_work_contracts import (
    StageWorkPlace,
    StageWorkReaperContract,
    StageWorkUnderTest,
)
from vs_runtime.api.step_log_contracts import StepLogContract, StepLogUnderTest
from vs_runtime.api.step_runner_contracts import StepRunnerContract, StepRunnerUnderTest
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
    "AttemptHangs",
    "AttemptHolds",
    "AttemptRaises",
    "AttemptReturns",
    "Behavior",
    "CounterConfig",
    "CrashableRunClock",
    "DaemonBackend",
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
    "FakeQuery",
    "FakeRunControlEventSink",
    "FakeRunHandle",
    "FakeRunRecords",
    "FakeRunSeed",
    "FakeRuns",
    "FakeStageWorkReaper",
    "FakeStepLog",
    "FakeStepRunner",
    "FakeStopTimer",
    "FakeTrustedAccuracyCall",
    "FakeTrustedBenchmarkCall",
    "FakeTrustedEvaluationExecutor",
    "FakeTrustedShellCall",
    "FakeWorkspace",
    "FakeWorkspaces",
    "HoldAt",
    "HoldingStepRunner",
    "HostOptions",
    "InvalidReply",
    "Launch",
    "LeftoverWorkError",
    "MemoryValidationPasses",
    "OverlappingCallError",
    "ProbedRunClock",
    "RecordingSearch",
    "Run",
    "RunnerCall",
    "RunnerMethod",
    "Script",
    "ScriptedExecutors",
    "StageWorkPlace",
    "StageWorkReaperContract",
    "StageWorkUnderTest",
    "StepLogContract",
    "StepLogUnderTest",
    "StepRunnerContract",
    "StepRunnerUnderTest",
    "StepTagMismatchError",
    "StillRunningAfterCancelError",
    "UnsafeControlSchemaError",
    "control_schema_fields",
    "counter",
    "daemon_docker_config",
    "daemon_engine",
    "fake_run_seed",
    "finished_result",
    "idle_environment_system",
    "unconfined_host_environment",
]
