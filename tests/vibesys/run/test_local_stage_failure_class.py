"""A polled measurement stage whose command was lost is never blamed on the candidate.

Runs without a Slurm cluster (modal, skypilot) measure candidates through the in-process
``PollingEvaluationExecutor`` over the product's trusted ``Evaluation``. When the sandbox
loses a stage's command (no exit status, or its own cancellation or timeout), nothing about
the candidate is known. The Slurm executor classifies such a stage as infrastructure
(``signal_of`` gives ``NO_EXIT_STATUS``), and so does the polled benchmark. The polled
accuracy stage must agree: a ``WORKLOAD`` verdict is final, so a machinery fault would reject
the candidate and core would never measure it again.

The composition is production from the executor down to the sandbox: the product's
evaluation adapter, the runtime workspaces with real Git candidates, and the trusted
executor. Only the sandbox (``FakeCommandRunner``) and the run environment session are Fakes.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.run_execution import run_execution_record

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.evaluation import create_evaluation
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import NULL_AGENT_EVENT_SINK, NULL_SKILL_SELECTION
from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    ExecutorObservation,
    SemanticEvaluationStage,
    TrustedEvidence,
)
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    RunEnvironmentRecord,
)
from vs_runtime.api import BenchmarkFailureKind, PollingEvaluationExecutor
from vs_runtime.api.infrastructure import (
    AgentPaths,
    BlockingOperations,
    ProjectRunEffects,
    ProjectRunRequest,
    RunEnvironmentRequest,
    RunEnvironmentView,
    ScalarBenchmarkContract,
    TrustedEvaluationPlan,
    WorkspaceResourceFactory,
    create_run_control_channel,
    create_workspace_runtime,
    open_project_run_resources,
    open_run_environment_resources,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink, FakeRunControlEventSink
from vs_sandbox.api import CommandResult, CommandRunner, ProjectPathPolicy
from vs_sandbox.api.testing import FakeCommandRunner, FakeComputeBackend

if TYPE_CHECKING:
    import threading
    from pathlib import Path
    from typing import TextIO

    from vs_agent.api import AgentClientProtocol
    from vs_project.api import OrchestrationRunManifest
    from vs_runtime.api import AgentRole, OrchestrationResumeDecision
    from vs_runtime.api.infrastructure import AgentExecutionConfiguration

_RUN_ID = "lost-stage"
_HANDLE = "handle-1"
_ACCURACY = "check-accuracy"
_BENCHMARK = "python benchmark.py"
_ENDED = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})

# What a sandbox reports for a command it lost: no exit status at all, or its own
# cancellation or timeout (a negative status), as the trusted executor documents.
_LOST = st.one_of(
    st.just(CommandResult(output="connection to the sandbox was lost", exit_code=None)),
    st.integers(min_value=-15, max_value=-1).map(
        lambda code: CommandResult(output="sandbox timed out", exit_code=code)
    ),
    st.just(CommandResult(output="cancelled", exit_code=-15, cancelled=True)),
)


class _LosesStageCommands(FakeCommandRunner):
    """A sandbox that loses the trusted accuracy and benchmark commands and runs the rest."""

    def __init__(self, lost: CommandResult) -> None:
        super().__init__()
        self.lost = lost

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        result = super().execute(command, timeout=timeout, cancel=cancel)
        if _ACCURACY in command or _BENCHMARK in command:
            return self.lost
        return result


@dataclass
class _Session:
    """A run-environment session that opens isolated candidates, as modal or skypilot does."""

    sandbox: CommandRunner
    view: RunEnvironmentView = field(
        default_factory=lambda: RunEnvironmentView(
            paths=AgentPaths(accuracy_command=_ACCURACY, benchmark_command=_BENCHMARK),
            cli_sandboxed=True,
            env_kind="modal",
        )
    )

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def close(self) -> None:
        return None


def _emit(text: str, writer: TextIO) -> None:
    writer.write(text + "\n")
    writer.flush()


def _unexpected_execution(_role: AgentRole) -> AgentExecutionConfiguration:
    message = "an evaluation-only run opened an agent execution"
    raise AssertionError(message)


def _unexpected_client(**_kwargs: object) -> AgentClientProtocol:
    message = "an evaluation-only run opened an agent client"
    raise AssertionError(message)


def _unexpected_resume(_manifest: OrchestrationRunManifest) -> OrchestrationResumeDecision:
    message = "fresh run resumed"
    raise AssertionError(message)


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "benchmark.py").write_text("print('unused: the sandbox is a Fake')\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        f"[accuracy]\ncommand = {json.dumps([_ACCURACY])}\n"
        f"[benchmark]\ncommand = {json.dumps(_BENCHMARK.split())}\n"
        '[benchmark.result]\njson_argument = "--output-json"\nmetric = "throughput"\n'
    )


def _run_request(root: Path) -> RunRequest:
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id="evaluation", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "evaluation"}}),
        input_bundle=load_input_bundle(root),
        objective="Improve the queue.",
        exp_name="evaluation",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _measurement(snapshot: str, kind: EvidenceKind) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key="lost-stage",
        stages=(
            EvaluationStep(
                name=kind.value,
                payload=SemanticEvaluationStage(
                    snapshot=snapshot, kind=kind, fingerprints=fingerprints
                ).model_dump(mode="json"),
            ),
        ),
    )


async def _terminal(executor: PollingEvaluationExecutor) -> ExecutorObservation:
    while True:
        observed = await executor.inspect(_HANDLE)
        if observed is not None and observed.state in _ENDED:
            return observed
        await executor.wait_for_change(_HANDLE, timeout_s=float("inf"))


def _measure_with_lost_command(
    tmp_path: Path, kind: EvidenceKind, lost: CommandResult
) -> ExecutorObservation:
    root = tmp_path / "project"
    _write_project(root)
    sandbox = _LosesStageCommands(lost)
    integration = LocalRunIntegration()
    request = ProjectRunRequest(
        project_root=root,
        run_id=_RUN_ID,
        display_name="lost stage",
        task_name=None,
        existing=False,
        framework_version="1.2.3",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="evaluation", config_version=1, options={}),
        trusted_input_paths=("benchmark.py", "vibesys.input.toml"),
    )
    effects = ProjectRunEffects(
        git_events=NullGitTrackerEvents(), log_emit=_emit, on_log_ready=lambda _path: None
    )
    with (
        closing(integration),
        open_project_run_resources(
            request, effects=effects, resolve_resume=_unexpected_resume
        ) as project,
    ):
        environment = open_run_environment_resources(
            RunEnvironmentRequest(
                log_dir=project.logger.log_dir,
                workspace=root,
                ref_dir=None,
                backend=FakeComputeBackend(),
                agent_backend="claude",
                cli_provider="claude",
                run_id=_RUN_ID,
                framework_root=tmp_path,
                project_path_policy=ProjectPathPolicy(),
                git_history_root=project.git.history_root,
            ),
            lambda _request: _Session(sandbox),
        )
        with closing(environment):
            factory = WorkspaceResourceFactory(
                project,
                environment,
                evaluation_plan=TrustedEvaluationPlan(
                    benchmark_contract=ScalarBenchmarkContract(
                        output_argument="--output-json", metric="throughput"
                    )
                ),
                memory_paths=(),
                skill_source_dirs=(),
                skill_selection=NULL_SKILL_SELECTION,
                host_resources=(),
                events=lambda _event: None,
            )
            runtime = create_workspace_runtime(
                (),
                workspace_resources=factory,
                resolve_configuration=_unexpected_execution,
                session_store=lambda: None,
                control=create_run_control_channel(FakeRunControlEventSink()),
                lifecycle_events=FakeAgentExecutionLifecycleSink(),
                agent_events=NULL_AGENT_EVENT_SINK,
                route_message=lambda message, _steering: message,
                blocking=BlockingOperations(),
                client_factory=_unexpected_client,
            )
            evaluation = create_evaluation(
                _RUN_ID, _run_request(root), runtime, integration.events, lambda _line: None
            )

            async def exercise() -> ExecutorObservation:
                executor = PollingEvaluationExecutor(evaluation, runtime.workspaces)
                try:
                    snapshot = await runtime.workspaces.root.snapshot("candidate")
                    await executor.submit(_measurement(snapshot, kind), handle_id=_HANDLE)
                    return await _terminal(executor)
                finally:
                    await executor.close()
                    await runtime.workspaces.close()

            return asyncio.run(exercise())


@pytest.mark.parametrize("kind", [EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK])
@settings(max_examples=3, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(lost=_LOST)
def test_a_polled_stage_whose_command_was_lost_is_not_a_final_candidate_failure(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    kind: EvidenceKind,
    lost: CommandResult,
) -> None:
    tmp_path = tmp_path_factory.mktemp("lost")
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))

    observed = _measure_with_lost_command(tmp_path, kind, lost)

    assert observed.stage_results, observed.failure
    evidence = TrustedEvidence.model_validate(observed.stage_results[0].result)
    assert evidence.outcome is EvidenceOutcome.FAILED
    # The sandbox reported no exit status, which says nothing about the candidate. Only a
    # WORKLOAD stage is final (core never measures it again), so it must not be one.
    assert evidence.failure_kind is not BenchmarkFailureKind.WORKLOAD
