"""A profile reaches the planner as typed evidence, and as the words of its prompt.

`test_profile_history.py` pins the typed history row built from a finished profile. These tests
pin the two ends of it: the row of a profile the flow ran (its question, its target and the
revision it looked at, its evidence), and what the planner's prompt says of the row. The live
defect behind both: four captures passed, yet each later planner prompt said "Why it failed" and
showed only the head of the capture log, so the planner profiled again.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, begin
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    profile,
    reviewed,
)
from tests.vibesys.orchestration.dynamic._values import (
    config,
    evidence_ref,
    reading_of_ref,
    revision,
    run_record,
)
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.metrics import MetricRow
from vibesys.orchestration.dynamic.models import ImplementerResult, ProfilerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    Failure,
    FailureKind,
    FinishedWork,
    PlannerPrompt,
    ProfileHistoryRow,
    ProfileHistoryStatus,
    PromptTemplate,
    Proof,
    RunRecord,
    WorkKind,
    WorkPlan,
)
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    Capabilities,
    EvidenceKind,
    ObservationStatus,
    Operation,
    Retained,
    RevisionRef,
    StartAttempt,
    TurnFailureKind,
)
from vs_core.api.testing import FakeView
from vs_sim.api.testing import examples

MODES = across_restarts
OUTPUT = string.ascii_letters + string.digits + " "
PARENT = revision("parent")
FIELDS = st.lists(st.sampled_from(["hip_api_timing", "decode_timing"]), unique=True, max_size=2)
METRICS = st.lists(
    st.builds(
        MetricRow,
        name=st.text(string.ascii_lowercase, min_size=3, max_size=8),
        value=st.floats(0, 1000, allow_nan=False, allow_infinity=False),
        direction=st.sampled_from(["max", "min"]),
        unit=st.sampled_from(["fraction", "ms"]),
    ),
    max_size=5,
    unique_by=lambda row: row.name,
).map(tuple)


def planned(required: tuple[str, ...] = ()) -> WorkPlan:
    return WorkPlan(
        kind=WorkKind.PROFILE,
        work_id="prof-1",
        question="Where does the time go?",
        decision_impact="Pick the next kernel.",
        required_fields=required,
        parent_hypothesis_id="target",
    )


@dataclass(frozen=True)
class Left:
    """What a profile workstream left behind (``passed`` None: no reading at all)."""

    passed: bool | None
    metrics: tuple[MetricRow, ...] = ()
    output: str = ""
    required: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    failure: Failure | None = None


def run_with(left: Left) -> RunRecord:
    """A run whose one profile workstream ended as ``left`` says."""
    run = run_record(config(profiling=True))
    ref = evidence_ref("profile-evidence", EvidenceKind.PROFILING, PARENT, purpose="profile")
    ref = ref.model_copy(update={"artifacts": left.artifacts})
    readings = ()
    if left.passed is not None:
        readings = (
            reading_of_ref(ref, passed=left.passed, metrics=left.metrics, feedback=left.output),
        )
    run.finished_work.append(
        FinishedWork(
            sequence=1,
            plan=planned(left.required),
            parent=PARENT,
            parent_hypothesis_id="target",
            failure=left.failure,
            readings=readings,
            proof=Proof(refs=(ref,)),
        )
    )
    return run


def prompt_of(run: RunRecord) -> PlannerPrompt:
    prompt = run.world().prompt()
    assert isinstance(prompt, PlannerPrompt)
    return prompt


def row_of(prompt: PlannerPrompt) -> ProfileHistoryRow:
    (row,) = [item for item in prompt.history if isinstance(item, ProfileHistoryRow)]
    return row


def said(run: RunRecord) -> tuple[ProfileHistoryRow, str]:
    """The profile row the planner is shown and the prompt text it is rendered into."""
    prompt = prompt_of(run)
    return row_of(prompt), render(PromptTemplate.PORTFOLIO, prompt)


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    metrics=METRICS,
    output=st.text(OUTPUT, max_size=6000),
    required=FIELDS,
    names=st.lists(st.text(string.ascii_lowercase, min_size=3, max_size=8), max_size=3),
)
def test_a_passed_profile_reads_as_observed_evidence_in_the_prompt_whatever_its_output_length(
    metrics: tuple[MetricRow, ...], output: str, required: list[str], names: list[str]
) -> None:
    artifacts = tuple(
        ArtifactRef(artifact_id=ArtifactId(root=f"art-{name}"), digest="d") for name in names
    )
    row, text = said(
        run_with(
            Left(
                passed=True,
                metrics=metrics,
                output=output,
                required=tuple(required),
                artifacts=artifacts,
            )
        )
    )

    assert row.status is ProfileHistoryStatus.OBSERVED
    assert "Why it failed" not in text
    assert "no trusted evidence" not in text
    # The verdict, the typed statistics, the fields and the artifacts are never cut.
    assert "Verdict: the trusted profile succeeded" in text
    for field in required:
        assert field in text
    for item in metrics:
        assert f"{item.name} = {item.value}" in text
    for item in artifacts:
        assert item.artifact_id.root in text
    # Only the raw output is bounded, and the cut keeps its end, where the tables are.
    assert output[-40:].strip() in text
    assert len(row.diagnosis) < 1300


@settings(max_examples=examples(pr=8, nightly=100))
@given(output=st.text(OUTPUT, min_size=1, max_size=3000), metrics=METRICS)
def test_a_failed_profile_states_its_failure_and_its_output_and_no_success_evidence(
    output: str, metrics: tuple[MetricRow, ...]
) -> None:
    failure = Failure(kind=FailureKind.NO_TRUSTED_EVIDENCE, status=ObservationStatus.FAILED)
    row, text = said(run_with(Left(passed=False, metrics=metrics, output=output, failure=failure)))

    assert row.status is ProfileHistoryStatus.FAILED
    assert "Why it failed" in text
    assert "Failure(" not in text
    assert "kind=" not in text
    assert output[-40:].strip() in text
    assert "Verdict: the trusted profile succeeded" not in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(output=st.text(OUTPUT, min_size=1, max_size=200))
def test_a_finished_profile_with_no_reading_is_a_failure_that_shows_its_own_output(
    output: str,
) -> None:
    failure = Failure(
        kind=FailureKind.NO_TRUSTED_EVIDENCE, status=ObservationStatus.FAILED, output=output
    )
    row, text = said(run_with(Left(passed=None, failure=failure)))

    assert row.status is ProfileHistoryStatus.FAILED
    assert "Why it failed" in text
    assert output.strip()[-40:] in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    kind=st.sampled_from(
        [FailureKind.OUT_OF_BUDGET, FailureKind.STOPPED, FailureKind.PROFILE_CAPTURE_UNAVAILABLE]
    )
)
def test_a_profile_that_ended_in_failure_reports_the_workstreams_own_reason(
    kind: FailureKind,
) -> None:
    _, text = said(run_with(Left(passed=None, failure=Failure(kind=kind))))
    assert "Failure(" not in text
    assert "kind=" not in text


def test_a_running_profile_carries_neither_failure_nor_success_evidence_in_the_prompt() -> None:
    run = run_record(config(profiling=True, max_in_flight=2))
    run.accepted.append(planned(("hip_api_timing",)))
    run.ready_to_start(1)
    row, text = said(run)

    assert row.status is ProfileHistoryStatus.RUNNING
    assert "prof-1" in text
    assert "Why it failed" not in text
    assert "no trusted evidence" not in text
    assert "Verdict: the trusted profile succeeded" not in text


QUESTION, IMPACT = "Where does queue_step spend device time?", "Optimize queue_step."


def planner_after_a_profile(*, restarting: bool, profiler_fails: bool) -> PlannerPrompt:
    """Run h1, then a profile of it, and take the prompt of the planner's next turn."""
    view = FakeView(
        changes={"capabilities": Capabilities(lifecycle=frozenset({"profile-capture"}))}
    )
    # no backlog: h1 keeps its agent slot while measured, so the profile is the next work
    cfg = config(max_rounds=3, max_in_flight=1, max_backlog=0, profiling=True)
    host = begin(cfg, restarting=restarting, view=view)
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("A")))
    host.answer(host.run.expect(StartAttempt, label="start"))
    result = ImplementerResult.model_validate_json(implemented("inconclusive", next_step="more"))
    host.run.reply(
        host.run.expect_turn(role="implementer"),
        result,
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
    )
    host.run.reply(host.run.expect_turn(role="judge"), ReviewResult.model_validate_json(reviewed()))
    host.drain_until_turn(PLANNER_LABEL)

    entry = profile("prof-A", target_hypothesis_id="A", question=QUESTION, decision_impact=IMPACT)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(entry))
    for verifying in host.open(Operation):  # the parent is verified before the child starts
        host.answer(verifying)
    host.answer(host.run.expect(StartAttempt, label="start"))
    asked = host.run.expect_turn(role="profiler")
    if profiler_fails:
        host.run.fail_turn(asked, TurnFailureKind.PROVIDER_FAILED)
    else:
        host.run.reply(
            asked, ProfilerResult.model_validate_json(implemented("supported")), checkpoint=None
        )
    host.drain_until_turn(PLANNER_LABEL)
    context = host.run.expect_turn(role=PLANNER_LABEL).context
    assert isinstance(context, PlannerPrompt)
    return context


def the_profile_row(context: PlannerPrompt) -> ProfileHistoryRow:
    (row,) = [item for item in context.history if isinstance(item, ProfileHistoryRow)]
    target = next(item for item in context.buildable if item.hypothesis_id == "A")
    assert (row.profile_id, row.target_hypothesis_id) == ("prof-A", "A")
    assert row.revision == target.revision
    assert row.revision != context.base_revision
    assert (row.question, row.decision_impact) == (QUESTION, IMPACT)
    return row


@MODES
def test_the_next_plan_receives_a_completed_profiles_intent_target_and_trusted_evidence(
    *, restarting: bool
) -> None:
    context = planner_after_a_profile(restarting=restarting, profiler_fails=False)
    row = the_profile_row(context)

    assert row.status is ProfileHistoryStatus.OBSERVED
    assert [item.root.split(":")[0] for item in row.evidence_ids] == ["profile"]
    text = render(PromptTemplate.PORTFOLIO, context)
    for expected in ("prof-A", QUESTION, IMPACT, row.evidence_ids[0].root):
        assert expected in text


@MODES
def test_the_next_plan_receives_a_failed_profiles_intent_target_and_typed_failure(
    *, restarting: bool
) -> None:
    row = the_profile_row(planner_after_a_profile(restarting=restarting, profiler_fails=True))

    assert row.status is ProfileHistoryStatus.FAILED
    assert row.failure is not None
    assert row.failure.kind is FailureKind.TURN_FAILED
    assert row.metrics == ()
