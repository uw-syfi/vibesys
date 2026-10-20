"""An idea is compared with its base from the agent's first evaluation to the framework's records (#3047).

One production-shaped run over fakes: the real shell, evaluation bridge, Slurm executor and Git,
a Fake cluster whose benchmark draws a seeded, noisy throughput from what the measured tree
holds (``speed.txt``), and a scripted implementer that edits its workspace, evaluates it through
the tool and reads the framework's comparison in the tool's reply. No scenario holds a number
the framework did not measure, and nothing here decides a verdict: the framework does.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.support.agent_tool_world import resumes, suspensions
from tests.support.real_git import NO_GIT, NO_GITHUB
from tests.support.session_world import ProviderFaults, SessionHost
from tests.support.skeleton_strategy import DIGEST, SkeletonConfig, SkeletonStrategy
from tests.support.skeleton_world import (
    IMPLEMENTATION,
    IMPLEMENTER,
    Implementation,
    Process,
    World,
    open_skeleton_world,
)
from tests.support.workspace_world import RUN_ID
from tests.system.agent_evaluation.test_bridge import (
    POLICY,
    Scenario,
    ToolResolver,
    ToolUsingWriter,
    run,
)

from vibesys.events import CoreEvent, CoreEventType, FrameworkWarningData, MeasurementComparedData
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import AgentClient
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    ComparisonVerdict,
    EvidenceKind,
    RunStatus,
    SubmitMeasurement,
    TaskObjective,
    TaskStage,
    TaskTerms,
)
from vs_core.api.testing import sample_limits
from vs_evaluation.api.tools import (
    SUBMIT_TOOL,
    VALIDATE_WAIT_TOOL,
    EvaluationServiceClientError,
)
from vs_project.api.testing import GitKind
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.core import (
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    AgentEvaluationRecords,
    OwedSubmissions,
    ReceiptEvidenceLedger,
    ReceiptStore,
    ScopeWorkspaces,
    StoreWorkspaceReceipts,
)
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import ManualClock, SimThreads, current_virtual_clock, loop_threads
from vs_slurm.api import SlurmBatchRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vs_agent.api import AgentTurnRequest
    from vs_project.api import StateNamespace
    from vs_runtime.api.core import CommitObserver
    from vs_runtime.api.infrastructure import RuntimeWorkspaces

BASE_SPEED = 100.0
"""What the input, which has no ``speed.txt``, measures before noise."""
NOISE = 0.01
"""The relative standard deviation of one run's measurement."""
TERMS = TaskTerms(
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
    stages=(
        TaskStage(stage_id="accuracy", execution_seconds=10.0),
        TaskStage(stage_id="benchmark", execution_seconds=10.0),
    ),
    measured=("accuracy", "benchmark"),
    benchmark_stage="benchmark",
    queue_allowance=880.0,
    accuracy_stage="accuracy",
    accuracy_gated=(EvidenceKind.BENCHMARK,),
    operation_seconds=10.0,
    objectives=(TaskObjective(name="throughput", direction="max"),),
    relative_noise=0.02,
)
_OUTPUT = (
    "__VIBESYS_FRAMEWORK_BENCHMARK_JSON__\n"
    '{"throughput": %r}\n'
    "__VIBESYS_FRAMEWORK_BENCHMARK_JSON_END__"
)


def _noise(key: str) -> float:
    """A seeded draw of one run's relative noise: roughly normal, mean 0, deviation ``NOISE``.

    The sum of twelve uniform draws, each a slice of the key's digest, less six has mean 0 and
    unit variance, and needs no generator whose state the order of calls could change.
    """
    digest = hashlib.sha256(key.encode()).digest()
    uniforms = (int.from_bytes(digest[i : i + 2], "big") / 65536 for i in range(0, 24, 2))
    return (sum(uniforms) - 6.0) * NOISE


class NoisyCluster(ScenarioCluster):
    """A cluster whose benchmark measures what the staged tree holds, with seeded noise.

    The throughput a tree has is the number in its ``speed.txt`` (the input has none). Each
    run of a tree draws its own noise from a generator seeded by the tree's number and how many
    times that number has been measured before, so the sequence of runs of one tree does not
    depend on how they interleave with other trees' runs.
    """

    def __init__(self, seed: int) -> None:
        """Seed every draw the cluster makes."""
        super().__init__()
        self._seed = seed
        self.runs: dict[float, list[float]] = {}
        """Every noisy value measured, by the tree's own (noiseless) throughput."""

    def submit(self, request: Any, *, operation_id: str) -> Any:  # noqa: ANN401  # lint-waiver: LW-310010 [ANN401]; the scheduler's request union is the base class's own signature.
        """Measure the tree the job staged, once per operation."""
        if isinstance(request, SlurmBatchRequest) and operation_id not in self.submissions:
            speed_file = Path(request.workspace) / "speed.txt"
            truth = float(speed_file.read_text()) if speed_file.exists() else BASE_SPEED
            seen = self.runs.setdefault(truth, [])
            noise = _noise(f"{self._seed}:{truth}:{len(seen)}")
            value = round(truth * (1.0 + noise), 6)
            seen.append(value)
            self.benchmark_output = _OUTPUT % value
        return super().submit(request, operation_id=operation_id)


@dataclass(frozen=True)
class Turn:
    """What the scripted implementer does in one provider turn (its first, or a resume of it)."""

    peek: bool = False
    """Evaluate the unchanged workspace again first: the tool answers with the earlier
    evaluation of that content, comparison included."""
    speed: float | None = None
    """Commit a tree that measures at this throughput."""
    note: str | None = None
    """Commit a line added to ``NOTES.md``, a path the evaluation never reads."""
    evaluates: bool = True
    """Evaluate the committed revision through the tool; False commits and ends the turn, as an
    implementer does that leaves the measuring to the framework."""

    @property
    def edits(self) -> bool:
        """Whether the turn commits and so evaluates a new revision."""
        return self.speed is not None or self.note is not None


@dataclass
class ScriptedWriter(ToolUsingWriter):
    """The Fake provider's implementer: it plays ``script``, one entry per provider turn."""

    script: tuple[Turn, ...] = ()
    seen: list[dict[str, Any] | None] = field(default_factory=list)
    """The comparison each peek's reply carried, or None where it carried none."""

    def __call__(self, request: AgentTurnRequest) -> None:
        """Play this turn: peek, edit and commit, evaluate, and wait for the result."""
        index = len(self.invocations)
        self.invocations.append(request.invocation_id)
        turn = self.script[index]
        tree = self.worktree()
        tools = self.tools()
        submit = tools[SUBMIT_TOOL]
        if turn.peek:
            reply = json.loads(submit.handler(submit.input_schema()))
            self.seen.append(reply.get("comparison"))
        if turn.speed is not None:
            (tree / "speed.txt").write_text(f"{turn.speed}\n", encoding="utf-8")
        if turn.note is not None:
            with (tree / "NOTES.md").open("a", encoding="utf-8") as notes:
                notes.write(turn.note + "\n")
        if turn.edits:
            self.answer["commit"] = self.vcs.commit_all(tree, f"turn {index}")
            if not turn.evaluates:
                self.answer["waiting"] = False
                return
            reply = json.loads(submit.handler(submit.input_schema()))
            self.handles.append(reply["handle_id"])
            wait = tools[VALIDATE_WAIT_TOOL]
            try:
                wait.handler(wait.input_schema(handles=(reply["handle_id"],)))
            except EvaluationServiceClientError as error:
                self.refusals.append(str(error))
        self.answer["waiting"] = turn.edits


@dataclass(frozen=True)
class Pace:
    """How fast the run clock passes in a scenario: jobs take time, and the base goes stale."""

    runtime: float
    """Run-clock seconds every job takes."""
    base_staleness: float
    """The task's ``base_staleness``."""


@dataclass
class Played:
    """One finished run: its state, the agent that played in it and the cluster that measured."""

    world: World
    writer: ScriptedWriter
    cluster: NoisyCluster
    process: Process
    comparisons: tuple[Any, ...]
    submissions: tuple[SubmitMeasurement, ...]


SEED = 7
"""The noisy cluster's seed in every scenario."""


@dataclass(frozen=True)
class Setting:
    """What a scenario varies about the run it plays in."""

    task: TaskTerms = TERMS
    pace: Pace | None = None
    """With a pace the run clock passes while jobs run, and the base goes stale at its pace."""
    policy: AgentEvaluationPolicy = POLICY
    """What the agent's tool measures with."""


@asynccontextmanager
async def play(
    tmp_path: Path,
    script: tuple[Turn, ...],
    *,
    commits: CommitObserver | None = None,
    setting: Setting | None = None,
) -> AsyncIterator[Played]:
    """Run the skeleton search with an implementer that plays ``script``; yield what happened."""
    setting = setting or Setting()
    task, pace, policy = setting.task, setting.pace, setting.policy
    writers: list[ScriptedWriter] = []
    resolvers: list[ToolResolver] = []
    bridges: list[AgentEvaluationBridge] = []
    cluster = NoisyCluster(SEED)

    def agents(root: Path) -> SessionHost:
        writer = ScriptedWriter(root, script=script)
        writers.append(writer)
        resolver = ToolResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=writer,
        )
        resolvers.append(resolver)
        client = AgentClient(
            FakeProvider(answer=writer.answer, on_turn=writer, threads=loop_threads()),
            clock=ManualClock(),
            threads=loop_threads(),
        )
        return SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())

    def yields(workspaces: RuntimeWorkspaces, receipts: StateNamespace) -> AgentEvaluationBridge:
        bridge = AgentEvaluationBridge(
            tmp_path / "evaluation.sock",
            policy,
            ScopeWorkspaces(
                workspaces, StoreWorkspaceReceipts(ReceiptStore(receipts, threads=loop_threads()))
            ),
            AgentEvaluationRecords(
                OwedSubmissions(ReceiptStore(receipts, threads=loop_threads())),
                ReceiptEvidenceLedger(ReceiptStore(receipts, threads=loop_threads())),
            ),
            network=writers[0].network,
            threads=loop_threads(),
        )
        resolvers[0].bridge = bridge
        bridges.append(bridge)
        return bridge

    with open_skeleton_world(
        tmp_path,
        SkeletonStrategy(config=SkeletonConfig(compared=True)),
        cluster=cluster,
        agents=agents,
        git=GitKind.REAL,
        unread_paths=("NOTES.md",),
    ) as world:
        world.yields = yields
        world.limits = sample_limits(max_turns=len(script) + 2)
        world.task = (
            task
            if pace is None
            else task.model_copy(update={"base_staleness": pace.base_staleness})
        )
        world.commits = commits
        if pace is not None:
            world.timed = (current_virtual_clock(), pace.runtime)
        process, _ = await run(Scenario(world, writers[0], bridges))
        state = process.shell.record.envelope.core
        yield Played(
            world,
            writers[0],
            cluster,
            process,
            state.evaluation.comparisons,
            tuple(
                row.request
                for row in state.records.records
                if isinstance(row.request, SubmitMeasurement)
            ),
        )


def _final(played: Played) -> Any:  # noqa: ANN401  # lint-waiver: LW-310011 [ANN401]; the comparison value type is vs_core's and is used only for its fields.
    assert played.comparisons, "no comparison was ever made"
    return played.comparisons[-1]


def _bases(played: Played) -> list[SubmitMeasurement]:
    """The runs of the input beyond the strategy's own baseline measurement."""
    return [
        request
        for request in played.submissions
        if request.plan.purpose == "baseline" and request.plan.measured_input is not None
    ]


@pytest.mark.parametrize(
    ("speed", "verdict"),
    [
        (130.0, ComparisonVerdict.BETTER),
        (70.0, ComparisonVerdict.WORSE),
        (100.3, ComparisonVerdict.WITHIN_NOISE),
    ],
)
async def test_an_idea_is_better_worse_or_within_noise_by_what_the_framework_measured(
    tmp_path: Path, speed: float, verdict: ComparisonVerdict
) -> None:
    """A truly faster idea is better, a slower one worse, one inside the noise neither."""
    async with play(tmp_path, (Turn(speed=speed), Turn(peek=True, speed=None))) as played:
        state = played.process.shell.record.envelope.core
        assert state.run.status is RunStatus.TERMINAL
        assert _final(played).verdict is verdict
        seen = played.writer.seen[-1]
        assert seen is not None, "the tool reported no comparison to the agent"
        assert seen["verdict"] == verdict.value
        assert seen["base_commit"] == _final(played).base.revision_id.root
        assert seen["candidate_samples"] >= 1
        assert seen["base_samples"] >= 1
        assert (suspensions(state), resumes(state)) == (1, 1)
        (wait,) = state.evaluation.continuations
        assert wait.authorization_receipt is not None
        told = wait.authorization_receipt.comparisons
        assert [c.verdict for c in told] == [verdict], "the resume publishes the comparison"


@pytest.mark.parametrize(
    ("speed", "verdict"),
    [(150.0, ComparisonVerdict.BETTER), (60.0, ComparisonVerdict.WORSE)],
)
async def test_an_idea_its_implementer_never_evaluated_is_compared_with_its_base(
    tmp_path: Path, speed: float, verdict: ComparisonVerdict
) -> None:
    """The framework's own final check compares an idea with its base, whoever measured it.

    The implementer commits and ends its turn without calling the evaluation tool (live, most
    ideas of a run were like that). The strategy's official measurement is then the only run of
    the candidate, and it must still be compared with the base the attempt opened on: the input.
    """
    async with play(tmp_path, (Turn(speed=speed, evaluates=False),)) as played:
        assert played.writer.handles == [], "the implementer never used the tool"
        final = _final(played)
        assert final.verdict is verdict
        assert final.base_value == pytest.approx(BASE_SPEED, rel=0.1)
        assert final.candidate_samples == 1
        assert len(played.cluster.runs[BASE_SPEED]) == 1, "the input's own measurement is the base"


async def test_the_strategys_own_baseline_run_is_the_base_so_the_base_is_not_measured_again(
    tmp_path: Path,
) -> None:
    """The input was measured before any agent worked; that run is the base's first sample."""
    async with play(tmp_path, (Turn(speed=150.0), Turn(peek=True))) as played:
        assert _bases(played) == []
        assert _final(played).base_samples == 1
        assert len(played.cluster.runs[BASE_SPEED]) == 1


async def test_an_implementer_who_sees_worse_fixes_it_and_measures_better_in_one_turn(
    tmp_path: Path,
) -> None:
    """Slower, then a fix, then faster: two evaluations of one paid turn, one base measurement."""
    script = (Turn(speed=70.0), Turn(peek=True, speed=140.0), Turn(peek=True))
    async with play(tmp_path, script) as played:
        state = played.process.shell.record.envelope.core
        first, second = played.writer.seen
        assert first is not None
        assert second is not None
        assert (first["verdict"], second["verdict"]) == ("worse", "better")
        assert (suspensions(state), resumes(state)) == (2, 2)
        assert _bases(played) == []
        assert len(played.cluster.runs[BASE_SPEED]) == 1, "the base is measured once"
        assert [c.verdict for c in played.comparisons] == [
            ComparisonVerdict.WORSE,
            ComparisonVerdict.BETTER,
        ]


async def test_repeated_runs_of_one_input_aggregate_and_a_lucky_run_is_not_picked(
    tmp_path: Path,
) -> None:
    """The agent re-runs one input (a notes-only commit each time): all its runs are one measurement.

    Every run of the input is a sample, the agent's four and, only when they leave a close call
    against the base, the strategy's own final check as a fifth. The comparison is the mean of
    all of them over the base's, however the runs came out, and the official record the ledger
    carries to ranking states that same mean, not one run's value.
    """
    script = (
        Turn(speed=104.0),
        Turn(note="again"),
        Turn(note="and again"),
        Turn(note="once more"),
        Turn(peek=True),
    )
    async with play(tmp_path, script) as played:
        values = played.cluster.runs[104.0]
        official = [r for r in played.submissions if r.plan.purpose == "official"]
        assert [r.reuses is None for r in official] == [len(values) == 5]
        assert len(values) in (4, 5), "the agent's runs, and one more only when needed"
        last = _final(played)
        assert last.candidate_samples == len(values)
        assert last.candidate_value == pytest.approx(sum(values) / len(values))
        assert last.candidate_value != max(values)
        state = played.process.shell.record.envelope.core
        (answer,) = (
            item
            for item in state.evaluation.evidence
            if item.purpose == "official" and item.kind is EvidenceKind.BENCHMARK
        )
        assert answer.reading is not None
        assert answer.reading.value == last.candidate_value, "ranking reads what was compared"
        peeked = played.writer.seen[-1]
        assert peeked is not None
        assert peeked["candidate_samples"] == 4
        assert peeked["candidate_value"] == pytest.approx(sum(values[:4]) / 4)


async def test_a_resumed_process_measures_with_the_identity_its_run_began_with(
    tmp_path: Path,
) -> None:
    """A host that resumes a run measures as the run's own facts say, not by what it computed.

    A resumed process derives its own environment identity (live, a Slurm profiler broker's
    socket and token differ in every process) while the run keeps the one it began with, which
    the strategy's official measurement uses. Were the agent's runs and the base's measured
    under the process's, they would be another measurement than the official run: the official
    run would be compared with no base, and the round would report its lone reading, another
    number than the comparison the agent was told.
    """
    process_digest = "cd" * 32
    policy = replace(POLICY, environment_digest=process_digest)
    script = (Turn(speed=104.0), Turn(note="again"), Turn(note="and again"), Turn(peek=True))
    async with play(tmp_path, script, setting=Setting(policy=policy)) as played:
        facts = played.process.shell.record.envelope.core.run.facts
        assert facts.environment_digest != process_digest
        assert {r.plan.environment_digest for r in played.submissions} == {facts.environment_digest}
        values = played.cluster.runs[104.0]
        assert len(values) >= 3
        last = _final(played)
        assert last.candidate_samples == len(values), "every run of the input is one measurement"


async def test_a_decisive_result_is_answered_by_the_agents_own_runs_without_another_run(
    tmp_path: Path,
) -> None:
    """The final check reuses the agent's samples when they leave no close call."""
    async with play(tmp_path, (Turn(speed=150.0), Turn(peek=True))) as played:
        assert len(played.cluster.runs[150.0]) == 1
        official = [r for r in played.submissions if r.plan.purpose == "official"]
        assert [r.reuses is not None for r in official] == [True]


async def test_the_run_events_carry_what_core_compared(tmp_path: Path) -> None:
    """Each comparison core logs is emitted once, in order, with what the tool told the agent."""
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=NO_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    heard: list[CoreEvent] = []
    integration.events.subscribe(heard.append)
    observer = integration.core_commit_observer(RUN_ID, None, "comparison-events")
    async with play(tmp_path, (Turn(speed=130.0), Turn(peek=True)), commits=observer) as played:
        emitted = [
            event.data
            for event in heard
            if event.type is CoreEventType.MEASUREMENT_COMPARED
            and isinstance(event.data, MeasurementComparedData)
        ]
        logged = played.comparisons
        assert len(emitted) == len(logged) >= 1
        for data, comparison in zip(emitted, logged, strict=True):
            assert data.verdict is comparison.verdict
            assert data.ratio == comparison.ratio
            assert data.noise == comparison.noise
            assert data.base_commit == comparison.base.revision_id.root
            assert data.candidate_samples == comparison.candidate_samples
            assert data.base_samples == comparison.base_samples
        told = played.writer.seen[-1]
        assert told is not None
        assert told["ratio"] == emitted[-1].ratio


async def test_an_egregious_count_of_evaluations_is_warned_of_once_and_none_is_refused(
    tmp_path: Path,
) -> None:
    """Evaluations have no allowance: the third call reaches the limit, the run warns, all run."""
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=NO_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    heard: list[CoreEvent] = []
    integration.events.subscribe(heard.append)
    observer = integration.core_commit_observer(RUN_ID, None, "runaway-events")
    script = (Turn(speed=110.0), Turn(speed=120.0), Turn(speed=130.0), Turn(speed=140.0))
    task = TERMS.model_copy(update={"runaway_evaluations": 3})
    async with play(tmp_path, script, commits=observer, setting=Setting(task=task)) as played:
        calls = played.process.shell.record.envelope.core.evaluation.agent_calls
        assert len([c for c in calls if c.request_id is not None and not c.repeat]) == 4
        assert all(call.rejection is None for call in calls)
        warned = [
            event.data
            for event in heard
            if event.type is CoreEventType.FRAMEWORK_WARNING
            and isinstance(event.data, FrameworkWarningData)
            and event.data.source_label == "evaluations"
        ]
        assert len(warned) == 1
        assert "3 evaluations" in (warned[0].detail or "")


STALING = Pace(runtime=60.0, base_staleness=20.0)
"""Jobs that take three times as long as a base sample stays fresh."""


async def test_the_agent_is_told_only_the_comparison_its_fresh_base_samples_make(
    tmp_path: Path,
) -> None:
    """The tool's reply and the resume receipt drop a base sample that went stale.

    Each job takes three times as long as a base sample stays fresh (``STALING``), so the
    strategy's baseline is stale when the first candidate's run ends, and core measures the base
    again. The agent does not wait for that run: it evaluates its second candidate at once, and
    the base run lands beside it, so one fresh base run serves both candidates and only it
    counts. Both readers of the comparison,
    the resume receipt (core's authorization) and the tool's reply (the bridge), must take the
    run clock: read at any other moment, the stale baseline run still counts as a second base
    sample.
    """
    script = (Turn(speed=130.0), Turn(speed=140.0), Turn(peek=True))
    async with play(tmp_path, script, setting=Setting(pace=STALING)) as played:
        state = played.process.shell.record.envelope.core
        assert len(played.cluster.runs[BASE_SPEED]) == 2, "the stale base was measured again"
        assert {c.candidate_value for c in played.comparisons} >= {played.cluster.runs[130.0][0]}, (
            "the first candidate, whose base went stale while it ran, was compared too"
        )
        last = _final(played)
        assert last.candidate_value == played.cluster.runs[140.0][0]
        assert last.base_samples == 1, "only the fresh base run counts"
        told = played.writer.seen[-1]
        assert told is not None, "the tool reported no comparison to the agent"
        assert (told["base_samples"], told["base_value"]) == (1, last.base_value)
        receipts = [
            wait.authorization_receipt.comparisons
            for wait in state.evaluation.continuations
            if wait.authorization_receipt is not None and wait.authorization_receipt.comparisons
        ]
        assert receipts, "a resume published the comparison"
        # The first candidate, re-compared over the fresh base run, may be told again beside it.
        assert last in receipts[-1]
        assert all(c.base_samples == 1 for c in receipts[-1]), "only the fresh base run counts"


SLOW = Pace(runtime=10.0, base_staleness=100_000.0)
"""Jobs that take ten seconds; the base never goes stale."""


@pytest.mark.regression
async def test_an_agent_that_resumed_on_a_provisional_comparison_is_told_where_it_ended(
    tmp_path: Path,
) -> None:
    """A later resume tells of an earlier candidate's comparison once core's runs changed it.

    With no declared noise a verdict needs three runs a side, which core takes after the agent
    resumed. The resume that waited for the first candidate marks its comparison provisional.
    Before, a later resume told only of the candidates it waited for, so the agent never learned
    the first candidate's verdict unless it evaluated it again, which the resume told it not to.
    """
    task = TERMS.model_copy(update={"relative_noise": 0.0})
    script = (Turn(speed=130.0), Turn(speed=131.0), Turn(speed=132.0), Turn(speed=133.0))
    async with play(tmp_path, script, setting=Setting(task=task, pace=SLOW)) as played:
        state = played.process.shell.record.envelope.core
        receipts = [
            wait.authorization_receipt
            for wait in state.evaluation.continuations
            if wait.authorization_receipt is not None
        ]
        first = played.comparisons[0].candidate_input
        told = [
            (c.verdict, c.candidate_input in receipt.settling)
            for receipt in receipts
            for c in receipt.comparisons
            if c.candidate_input == first
        ]
        assert told[0] == (ComparisonVerdict.INCONCLUSIVE, True), "first told as provisional"
        assert told[-1] == (ComparisonVerdict.BETTER, False), "later told where it ended"
        assert len(told) == len(set(told)), "each change is told once"


@pytest.mark.regression
async def test_a_repeat_evaluation_tells_the_agent_its_comparison_is_still_settling(
    tmp_path: Path,
) -> None:
    """The tool's reply to a repeat call marks a comparison provisional, as a resume does.

    With no declared noise core runs both sides again after the agent resumed. The resume
    tells the agent to evaluate the unchanged content again to read the comparison once those
    runs land; before, that reply carried no settling flag, so the agent could not tell a
    provisional comparison from a final one.
    """
    task = TERMS.model_copy(update={"relative_noise": 0.0})
    script = (Turn(speed=130.0), Turn(speed=131.0), Turn(peek=True))
    async with play(tmp_path, script, setting=Setting(task=task, pace=SLOW)) as played:
        (told,) = played.writer.seen
        assert told is not None, "the tool reported no comparison to the agent"
        assert told["settling"] is True, "core's runs of the comparison were still under way"
        assert told["candidate_samples"] < 3


@pytest.mark.regression
async def test_the_final_check_of_an_idea_waits_for_the_runs_its_close_call_earned(
    tmp_path: Path,
) -> None:
    """The strategy's verdict rests on the comparison once core's runs have landed.

    With no declared noise a verdict needs three runs a side. The implementer never evaluates, so
    the strategy's final check is the candidate's only run, and its comparison with the input is a
    close call that earns both sides more runs. The strategy takes its verdict as soon as the
    check returns, so before, the run ended with those runs cancelled and the idea judged on one
    sample a side.
    """
    task = TERMS.model_copy(update={"relative_noise": 0.0})
    async with play(
        tmp_path, (Turn(speed=104.0, evaluates=False),), setting=Setting(task=task, pace=SLOW)
    ) as played:
        last = _final(played)
        assert (last.candidate_samples, last.base_samples) == (3, 3)
        assert len(played.cluster.runs[104.0]) == 3
        assert len(played.cluster.runs[BASE_SPEED]) == 3
