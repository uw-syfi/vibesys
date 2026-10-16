"""Evaluator stage and tool build output lands in the run's log store under typed owners.

The run's openers (``vibesys.run.logs``) write each gate stage's raw output to a stream
owned by ``GateStage(execution_id, gate, attempt)``, named the way the gate events name
the stage, while it runs; and each evaluator tool build's output to one ``ToolBuild``.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.events import GateKind
from vibesys.log_owners import LOG_OWNER, GateStage, LogOwner, ToolBuild
from vibesys.run.logs import (
    TOOL_BUILD_COMMAND,
    stage_output_opener,
    tool_build_name,
    tool_build_output_opener,
)
from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    PollPhase,
    SemanticEvaluationStage,
)
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_logs.api import MemoryLogStore, StreamOutcome
from vs_logs.api.testing import MemoryVolume, no_warning
from vs_logs.read import Tail, reader_of
from vs_runtime.api import (
    AccuracyEvaluation,
    BenchmarkFailureKind,
    BuildEvaluation,
    EvaluationRetries,
    PollingEvaluationExecutor,
    RetryingEvaluationExecutor,
    TryHandles,
)
from vs_runtime.api.infrastructure import (
    DockerToolBuilder,
    TrustedEvaluatorRequirements,
    prepare_docker_evaluator_resources,
)
from vs_runtime.api.slurm_evaluation_stack import BENCHMARK_OUTPUT, ScenarioCluster, build_stack
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sandbox.api import CommandResult, SandboxKind
from vs_sandbox.api.evaluator_tools import CargoGitToolSpec, EvaluatorToolError
from vs_sandbox.api.testing import FakeComputeBackend, FakeLifecycleRunner
from vs_sim.api.testing import Gate, ManualClock, SimThreads, arrival, yield_until
from vs_slurm.api import SlurmJobStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_logs.read import LogReader, StreamInfo
    from vs_runtime.api.core import StageOutputOpener

_HANDLE = "vs-measurement"
_PRINTED: dict[Literal["build", "accuracy", "benchmark"], tuple[bytes, bytes]] = {
    "accuracy": (b"checking 1/3\nchecking 2/3\n", b"warning: slow\n"),
    "benchmark": (b"warmup\nrps=41\n", b""),
}
_STAGES = (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})
_RETRIES = EvaluationRetries(infrastructure_attempts=2, ambiguous_attempts=1)


def _request(snapshot: str, purpose: str, stages: tuple[EvidenceKind, ...]) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    return EvaluationRequest(
        key=_HANDLE,
        stages=tuple(
            EvaluationStep(
                name=kind.value,
                payload=SemanticEvaluationStage(
                    snapshot=snapshot, kind=kind, fingerprints=fingerprints, purpose=purpose
                ).model_dump(mode="json"),
            )
            for kind in stages
        ),
    )


def _without_purpose(request: EvaluationRequest) -> EvaluationRequest:
    """``request`` as a host recorded it before stages carried a purpose."""
    stages = []
    for step in request.stages:
        assert isinstance(step.payload, dict)
        payload = {key: value for key, value in step.payload.items() if key != "purpose"}
        stages.append(step.model_copy(update={"payload": payload}))
    return request.model_copy(update={"stages": tuple(stages)})


type _EarlierHost = Callable[
    [EvaluationRequest, InMemoryEvaluationNamespace, FakeWorkspaces], Awaitable[None]
]


@dataclass(frozen=True)
class _Hooks:
    """What a test does around a measurement."""

    earlier_host: _EarlierHost | None = None
    """Runs first with the request, the retry wrapper's namespace and the workspaces,
    as a host that stopped before this one resumed."""
    while_running: Callable[[asyncio.Future[object]], Awaitable[None]] | None = None
    """Runs right after the measurement is submitted, with the task that waits for it
    to end (so a wait in the hook ends if the measurement does)."""


def _store() -> MemoryLogStore[LogOwner]:
    return MemoryLogStore(LOG_OWNER, threads=SimThreads(), warn=no_warning)


def _read(reader: LogReader[LogOwner], info: StreamInfo[LogOwner]) -> bytes:
    return reader.read(info.stream_id, Tail(1 << 16), 1 << 16).data


def _owners(store: MemoryLogStore[LogOwner]) -> list[tuple[LogOwner, StreamOutcome | None]]:
    return [(info.owner, info.outcome) for info in reader_of(store, LOG_OWNER).list_streams()]


async def _measure(
    evaluation: FakeEvaluation,
    *,
    purpose: str,
    stage_output: Callable[[TryHandles], StageOutputOpener | None],
    stages: tuple[EvidenceKind, ...] = _STAGES,
    hooks: _Hooks | None = None,
) -> None:
    """Measure one candidate the way the run host does: polling under the retry wrapper."""
    hooks = hooks or _Hooks()
    evaluation.printed.update(_PRINTED)
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True
    )
    tries = TryHandles()
    namespace = InMemoryEvaluationNamespace()
    request = _request(await workspaces.root.snapshot("candidate"), purpose, stages)
    if hooks.earlier_host is not None:
        await hooks.earlier_host(request, namespace, workspaces)
    executor = RetryingEvaluationExecutor(
        PollingEvaluationExecutor(
            evaluation, workspaces, clock=ManualClock(), stage_output=stage_output(tries)
        ),
        _RETRIES,
        namespace,
        tries=tries,
    )
    await executor.submit(request, handle_id=_HANDLE)
    ended: asyncio.Future[object] = asyncio.ensure_future(
        yield_until(
            lambda: executor.poll(_HANDLE),
            lambda polled: polled.phase is PollPhase.ENDED,
            "the measurement to end",
        )
    )
    if hooks.while_running is not None:
        await hooks.while_running(ended)
    await ended
    await executor.close()


@pytest.mark.asyncio
async def test_each_gate_stage_writes_to_its_gate_stream_before_it_settles() -> None:
    store = _store()
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    gate = evaluation.gate("accuracy", 0)
    live: list[tuple[list[tuple[LogOwner, StreamOutcome | None]], bytes]] = []

    async def look_while_accuracy_runs(ended: asyncio.Future[object]) -> None:
        await arrival(gate.entered.wait(), ended)
        reader = reader_of(store, LOG_OWNER)
        (accuracy,) = reader.list_streams()
        live.append((_owners(store), _read(reader, accuracy)))
        gate.release()

    await _measure(
        evaluation,
        purpose="official",
        stage_output=lambda tries: tries.stage_output(stage_output_opener(store)),
        hooks=_Hooks(while_running=look_while_accuracy_runs),
    )

    accuracy = GateStage(execution_id=_HANDLE, gate=GateKind.ACCURACY, attempt=1)
    benchmark = GateStage(execution_id=_HANDLE, gate=GateKind.BENCHMARK, attempt=1)
    assert live == [([(accuracy, None)], b"".join(_PRINTED["accuracy"]))]
    reader = reader_of(store, LOG_OWNER)
    streams = reader.list_streams()
    assert [(info.owner, info.outcome) for info in streams] == [
        (accuracy, StreamOutcome.ENDED),
        (benchmark, StreamOutcome.ENDED),
    ]
    assert [info.command for info in streams] == ["accuracy", "benchmark"]
    assert _read(reader, streams[1]) == b"".join(_PRINTED["benchmark"])


class _HeldPacing:
    """Pacing that holds the executor's own wait for a running job until the test opens it.

    The executor's execution task, which opens and settles the stage streams, pauses here
    between its looks at a running job. A poll collects an ended job by itself, so while
    the task is held a poll reports the end before the task has collected the job.
    """

    def __init__(self) -> None:
        self.entered = Gate()
        self.release = Gate()

    async def sleep(self, seconds: float) -> None:
        del seconds
        self.entered.open()
        await self.release.wait()


async def _execution_ended(executor: RetryingEvaluationExecutor) -> None:
    """Wait until the execution task has settled the measurement, stage streams included.

    A poll's end is no such proof: the poll collects the job apart from that task.
    """
    while True:
        observed = await executor.inspect(_HANDLE)
        if observed is not None and observed.state in _TERMINAL:
            return
        await executor.wait_for_change(_HANDLE, timeout_s=math.inf)


@pytest.mark.asyncio
async def test_each_slurm_gate_stage_writes_to_its_gate_stream_as_the_host_wires_it(
    tmp_path: Path,
) -> None:
    """A stage run on the cluster lands under the same gate owners, through the same tries.

    The host learns the end from a poll, which here comes before the execution task has
    collected the job; the streams are whole once that task has settled.
    """
    store = _store()
    tries = TryHandles()
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED)
    pacing = _HeldPacing()
    stack = await build_stack(
        tmp_path,
        cluster,
        stage_output=tries.stage_output(stage_output_opener(store)),
        sleeper=pacing,
    )
    executor = RetryingEvaluationExecutor(
        stack.executor, _RETRIES, InMemoryEvaluationNamespace(), tries=tries
    )

    settled: asyncio.Future[None] | None = None
    try:
        await executor.submit(_request(stack.snapshot, "official", _STAGES), handle_id=_HANDLE)
        # The execution task is the producer; it lives inside the executor, so follow it
        # through the executor's settled state: if it ends without pausing, fail with that.
        settled = asyncio.ensure_future(_execution_ended(executor))
        await arrival(pacing.entered.wait(), settled)  # the task saw the job running, paused
        # The job has completed; the poll wakes and joins the task that owns collection.
        polled = await executor.poll(_HANDLE)
        assert polled.phase is PollPhase.ENDED
        pacing.release.open()
        await settled
    finally:
        pacing.release.open()
        if settled is not None:
            settled.cancel()
        await executor.close()

    reader = reader_of(store, LOG_OWNER)
    streams = reader.list_streams()
    assert [(info.owner, info.outcome) for info in streams] == [
        (GateStage(execution_id=_HANDLE, gate=GateKind.ACCURACY, attempt=1), StreamOutcome.ENDED),
        (GateStage(execution_id=_HANDLE, gate=GateKind.BENCHMARK, attempt=1), StreamOutcome.ENDED),
    ]
    assert [_read(reader, info) for info in streams] == [b"ok", BENCHMARK_OUTPUT.encode()]


@pytest.mark.asyncio
async def test_a_slurm_stage_whose_log_disk_is_full_still_measures(tmp_path: Path) -> None:
    """Copying a stage's output into a full store loses log bytes, never the measurement."""
    volume = MemoryVolume()
    warnings: list[str] = []
    store = MemoryLogStore(LOG_OWNER, threads=SimThreads(), warn=warnings.append, volume=volume)
    tries = TryHandles()
    stack = await build_stack(tmp_path, stage_output=tries.stage_output(stage_output_opener(store)))
    executor = RetryingEvaluationExecutor(
        stack.executor, _RETRIES, InMemoryEvaluationNamespace(), tries=tries
    )
    volume.capacity = 200  # the first stage's index record fits, and little else

    await executor.submit(_request(stack.snapshot, "official", _STAGES), handle_id=_HANDLE)
    ended = await yield_until(
        lambda: executor.poll(_HANDLE),
        lambda polled: polled.phase is PollPhase.ENDED,
        "the measurement to end",
    )
    await _execution_ended(executor)
    await executor.close()

    assert ended.terminal is not None
    assert ended.terminal.state is EvaluationState.SUCCEEDED
    reader = reader_of(store, LOG_OWNER)
    first = reader.list_streams()[0]
    assert first.owner == GateStage(execution_id=_HANDLE, gate=GateKind.ACCURACY, attempt=1)
    assert b"ok".startswith(_read(reader, first))
    assert warnings


@pytest.mark.asyncio
async def test_a_local_validation_accuracy_stage_is_the_validation_gate() -> None:
    store = _store()

    await _measure(
        FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True)),
        purpose="local-validation",
        stage_output=lambda tries: tries.stage_output(stage_output_opener(store)),
        stages=(EvidenceKind.ACCURACY,),
    )

    validation = GateStage(execution_id=_HANDLE, gate=GateKind.VALIDATION, attempt=1)
    assert _owners(store) == [(validation, StreamOutcome.ENDED)]


_BUILD_STAGES = (EvidenceKind.BUILD, EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)


@pytest.mark.asyncio
async def test_a_build_stage_writes_its_output_to_the_build_gate_stream_first() -> None:
    store = _store()
    evaluation = FakeEvaluation(
        default_build=BuildEvaluation(executed=True),
        default_accuracy=AccuracyEvaluation(executed=True),
    )
    evaluation.printed["build"] = (b"cargo build --release\n", b"")

    await _measure(
        evaluation,
        purpose="official",
        stage_output=lambda tries: tries.stage_output(stage_output_opener(store)),
        stages=_BUILD_STAGES,
    )

    build = GateStage(execution_id=_HANDLE, gate=GateKind.BUILD, attempt=1)
    reader = reader_of(store, LOG_OWNER)
    streams = reader.list_streams()
    assert [info.owner.gate for info in streams if isinstance(info.owner, GateStage)] == [
        GateKind.BUILD,
        GateKind.ACCURACY,
        GateKind.BENCHMARK,
    ]
    assert (streams[0].owner, streams[0].command) == (build, "build")
    assert _read(reader, streams[0]) == b"cargo build --release\n"


@pytest.mark.asyncio
async def test_a_failed_build_skips_the_check_and_the_measure() -> None:
    """Nothing after a failed build can count, so neither the check nor the measure runs."""
    store = _store()
    missing = "the build did not produce required output 'queue-candidate.so'"
    evaluation = FakeEvaluation(
        default_build=BuildEvaluation(
            executed=True,
            feedback=missing,
            operator_feedback=missing,
            failure_kind=BenchmarkFailureKind.WORKLOAD,
        )
    )

    await _measure(
        evaluation,
        purpose="official",
        stage_output=lambda tries: tries.stage_output(stage_output_opener(store)),
        stages=_BUILD_STAGES,
    )

    assert (len(evaluation.build_calls), evaluation.accuracy_calls) == (1, [])
    assert evaluation.benchmark_calls == []
    build = GateStage(execution_id=_HANDLE, gate=GateKind.BUILD, attempt=1)
    assert [owner for owner, _ in _owners(store)] == [build]


@pytest.mark.asyncio
async def test_a_retried_stage_writes_a_failed_stream_then_one_for_its_next_attempt() -> None:
    store = _store()
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    # A stage that raises fails its try for a reason in the machinery, which is retried.
    evaluation.script_accuracy(RuntimeError("evaluator crashed"))

    await _measure(
        evaluation,
        purpose="official",
        stage_output=lambda tries: tries.stage_output(stage_output_opener(store)),
        stages=(EvidenceKind.ACCURACY,),
    )

    reader = reader_of(store, LOG_OWNER)
    first, second = reader.list_streams()
    assert (first.owner, first.outcome) == (
        GateStage(execution_id=_HANDLE, gate=GateKind.ACCURACY, attempt=1),
        StreamOutcome.FAILED,
    )
    assert (second.owner, second.outcome) == (
        GateStage(execution_id=_HANDLE, gate=GateKind.ACCURACY, attempt=2),
        StreamOutcome.ENDED,
    )
    assert _read(reader, first) == _read(reader, second) == b"".join(_PRINTED["accuracy"])


@pytest.mark.asyncio
async def test_a_measurement_recorded_before_stages_named_a_purpose_resumes_on_its_gate() -> None:
    store = _store()
    stages = (EvidenceKind.ACCURACY,)

    async def host_before_the_upgrade(
        request: EvaluationRequest,
        namespace: InMemoryEvaluationNamespace,
        workspaces: FakeWorkspaces,
    ) -> None:
        # It recorded the measurement in the earlier format and stopped before it ran.
        earlier = RetryingEvaluationExecutor(
            PollingEvaluationExecutor(FakeEvaluation(), workspaces, clock=ManualClock()),
            _RETRIES,
            namespace,
        )
        await earlier.submit(_without_purpose(request), handle_id=_HANDLE)
        await earlier.close()

    await _measure(
        FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True)),
        purpose="local-validation",
        stage_output=lambda tries: tries.stage_output(stage_output_opener(store)),
        stages=stages,
        hooks=_Hooks(earlier_host=host_before_the_upgrade),
    )

    validation = GateStage(execution_id=_HANDLE, gate=GateKind.VALIDATION, attempt=1)
    assert _owners(store) == [(validation, StreamOutcome.ENDED)]


@pytest.mark.asyncio
async def test_a_stage_that_is_no_gate_opens_no_stream() -> None:
    store = _store()

    # The polling executor does not run profile stages; the stage fails, unrecorded.
    await _measure(
        FakeEvaluation(),
        purpose="profile",
        stage_output=lambda tries: tries.stage_output(stage_output_opener(store)),
        stages=(EvidenceKind.PROFILE,),
    )

    assert _owners(store) == []


@pytest.mark.asyncio
async def test_an_executor_given_no_opener_opens_no_stream() -> None:
    store = _store()
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))

    await _measure(evaluation, purpose="official", stage_output=lambda _: None)

    assert len(evaluation.accuracy_calls) == len(evaluation.benchmark_calls) == 1
    assert _owners(store) == []


def _tool() -> CargoGitToolSpec:
    return CargoGitToolSpec(
        kind="cargo-git",
        git="https://example.com/tools/check.git",
        rev="a" * 40,
        package="check-tool",
        bins=("check",),
        system_packages=("pkg-config",),
    )


def test_a_tool_build_writes_its_output_to_one_tool_build_stream(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    backend = FakeComputeBackend(threads=SimThreads())
    printed = "   Compiling check-tool v1.0.0\n"
    builder = FakeLifecycleRunner(
        default_result=CommandResult(output=printed, exit_code=0, stdout=printed)
    )
    backend.script_sandbox(
        SandboxKind.DOCKER, str(log_dir / "evaluator-tool-builder-workspace"), builder
    )
    store = _store()

    # The fake builder publishes no binary, so the build fails after its commands ran.
    with pytest.raises(EvaluatorToolError, match="did not publish every declared tool"):
        prepare_docker_evaluator_resources(
            TrustedEvaluatorRequirements(
                package_root=tmp_path / "evaluator-package",
                tools={"check-tool": _tool()},
                tools_root=tmp_path / "operator-tools",
            ),
            workspace,
            builder=DockerToolBuilder(
                backend=backend,
                log_dir=log_dir,
                image="sha256:target-image",
                output=tool_build_output_opener(store),
            ),
        )

    reader = reader_of(store, LOG_OWNER)
    (build,) = reader.list_streams()
    assert (build.owner, build.outcome) == (ToolBuild(name="check-tool"), StreamOutcome.FAILED)
    assert build.command == TOOL_BUILD_COMMAND
    assert printed.encode() in _read(reader, build)


_TOOL_NAMES = st.lists(st.text(min_size=1, max_size=40), max_size=12)


@given(tools=_TOOL_NAMES)
def test_a_tool_build_name_is_bounded_and_ignores_order(tools: list[str]) -> None:
    name = tool_build_name(tuple(tools))

    assert ToolBuild(name=name).name == name
    assert name == tool_build_name(tuple(reversed(tools)))
    assert len(name) <= 4 * 40 + 3 * len(", ") + len(" +99 more")
    for tool in sorted(set(tools))[:4]:
        assert tool in name
