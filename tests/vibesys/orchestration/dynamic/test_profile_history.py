"""A profile reaches the planner as typed evidence, and only a real failure reads as one.

Live defect: four captures passed (every required field measured, exit 0), yet each later
planner prompt said "Why it failed: profile produced no trusted evidence" and showed only the
head of the capture log, so the planner profiled again. The planner's history row is built from
the finished workstream's record alone; these properties range over what a profile can have
left: a passed or failed reading of any output length, no reading at all, or still running.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._values import (
    BASE,
    config,
    evidence_ref,
    finished_profile,
    hypothesis_record,
    reading_of_ref,
    revision,
    run_record,
)

from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import MetricRow
from vibesys.orchestration.dynamic.search.api import (
    Failure,
    FailureKind,
    FinishedWork,
    ProfileHistoryRow,
    ProfileHistoryStatus,
    Proof,
    RunRecord,
    WorkKind,
    WorkPlan,
)
from vs_core.api import ArtifactId, ArtifactRef, EvidenceKind, ObservationStatus
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.search.api import PlannerPrompt

OUTPUT = string.ascii_letters + string.digits + " "
CUT_AT = 1200
PARENT = revision("parent")
FIELDS = st.lists(st.sampled_from(["hip_api_timing", "decode_timing"]), unique=True, max_size=2)
METRICS = st.lists(
    st.builds(
        MetricRow,
        name=st.text(string.ascii_lowercase, min_size=3, max_size=8),
        value=st.floats(0, 1000, allow_nan=False, allow_infinity=False),
        direction=st.sampled_from(["max", "min"]),
    ),
    max_size=5,
    unique_by=lambda row: row.name,
).map(tuple)


def planned_profile(required: tuple[str, ...]) -> WorkPlan:
    return WorkPlan(
        kind=WorkKind.PROFILE,
        work_id="prof-1",
        question="Where does the time go?",
        decision_impact="Pick the next kernel.",
        required_fields=required,
        parent_hypothesis_id="target",
    )


def prompt_of(run: RunRecord) -> PlannerPrompt:
    prompt = run.world().prompt()
    assert prompt.template.value == "portfolio"
    return prompt  # ty: ignore[invalid-return-type]


def profile_rows(run: RunRecord) -> list[ProfileHistoryRow]:
    return [item for item in prompt_of(run).history if isinstance(item, ProfileHistoryRow)]


@dataclass(frozen=True)
class Left:
    """What a profile workstream left behind."""

    passed: bool | None
    """The capture's reading verdict, or None when it left no reading."""
    metrics: tuple[MetricRow, ...] = ()
    output: str = ""
    required: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    failure: Failure | None = None


def finished(left: Left) -> RunRecord:
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
            plan=planned_profile(left.required),
            parent=PARENT,
            parent_hypothesis_id="target",
            failure=left.failure,
            readings=readings,
            proof=Proof(refs=(ref,)),
        )
    )
    return run


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    metrics=METRICS,
    output=st.text(OUTPUT, max_size=6000),
    required=FIELDS,
    names=st.lists(st.text(string.ascii_lowercase, min_size=3, max_size=8), max_size=3),
)
def test_a_passed_profile_is_observed_evidence_whatever_its_output_length(
    metrics: tuple[MetricRow, ...], output: str, required: list[str], names: list[str]
) -> None:
    artifacts = tuple(
        ArtifactRef(artifact_id=ArtifactId(root=f"art-{name}"), digest="d") for name in names
    )
    left = Left(
        passed=True, metrics=metrics, output=output, required=tuple(required), artifacts=artifacts
    )
    (row,) = profile_rows(finished(left))

    assert row.status is ProfileHistoryStatus.OBSERVED
    assert row.failure is None
    assert row.failure_output == ""
    # The typed statistics, the fields and the artifacts are never cut.
    assert row.metrics == metrics
    assert row.captured_fields == tuple(required)
    assert row.artifacts == tuple(item.artifact_id.root for item in artifacts)
    assert [item.root for item in row.evidence_ids] == ["profile-evidence"]
    # Only the raw output is bounded, and the cut keeps its end, where the tables are.
    assert row.diagnosis.endswith(output[-CUT_AT:])
    assert len(row.diagnosis) <= CUT_AT + len("[cut] ")


@settings(max_examples=examples(pr=8, nightly=100))
@given(output=st.text(OUTPUT, min_size=1, max_size=3000), metrics=METRICS)
def test_a_failed_profile_states_its_failure_and_offers_no_success_evidence(
    output: str, metrics: tuple[MetricRow, ...]
) -> None:
    failure = Failure(kind=FailureKind.NO_TRUSTED_EVIDENCE, status=ObservationStatus.FAILED)
    left = Left(passed=False, metrics=metrics, output=output, failure=failure)
    (row,) = profile_rows(finished(left))

    assert row.status is ProfileHistoryStatus.FAILED
    assert row.failure == failure
    assert row.metrics == ()
    assert row.captured_fields == ()
    assert row.artifacts == ()
    assert row.diagnosis == ""
    assert row.failure_output.endswith(output[-CUT_AT:])


@settings(max_examples=examples(pr=8, nightly=100))
@given(output=st.text(OUTPUT, max_size=200))
def test_a_finished_profile_with_no_reading_is_a_failure_that_shows_its_own_output(
    output: str,
) -> None:
    failure = Failure(
        kind=FailureKind.NO_TRUSTED_EVIDENCE, status=ObservationStatus.FAILED, output=output
    )
    (row,) = profile_rows(finished(Left(passed=None, failure=failure)))
    assert row.status is ProfileHistoryStatus.FAILED
    assert row.failure == failure
    assert row.failure_output == output
    assert [item.root for item in row.evidence_ids] == ["profile-evidence"]


def test_a_running_profile_carries_neither_failure_nor_success_evidence() -> None:
    run = run_record(config(profiling=True, max_in_flight=2))
    run.accepted.append(planned_profile(("hip_api_timing",)))
    run.ready_to_start(1)
    (row,) = profile_rows(run)
    assert row.status is ProfileHistoryStatus.RUNNING
    assert (row.failure, row.failure_output, row.diagnosis) == (None, "", "")
    assert (row.metrics, row.artifacts, row.captured_fields, row.evidence_ids) == ((), (), (), ())
    assert row.revision == BASE  # it looks at the revision it was scheduled on


@settings(max_examples=examples(pr=8, nightly=100))
@given(count=st.integers(0, 30))
def test_the_planner_is_shown_the_latest_rows_in_schedule_order_and_told_the_ids_it_is_not_shown(
    count: int,
) -> None:
    run = run_record(config(max_rounds=40))
    for sequence in range(1, count + 1):
        record = hypothesis_record(f"h{sequence}", HypothesisOutcome.INCONCLUSIVE)
        run.hypotheses[record.hypothesis_id] = record.model_copy(
            update={"first_sequence": sequence}
        )
    run.finished_work.append(finished_profile("prof", count + 1))
    prompt = prompt_of(run)

    shown = [row.sequence for row in prompt.history]
    assert shown == sorted(shown)
    assert len(shown) == min(count + 1, 20)
    assert shown == list(range(count + 2 - len(shown), count + 2))
    hidden = [f"h{n}" for n in range(1, count + 2 - len(shown))]
    assert list(prompt.older_ids) == hidden
