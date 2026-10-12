"""A finished profile reaches the planner as typed evidence, and only a real failure reads as one.

Live defect: four rocprofv3 captures passed (every required field measured, exit 0), yet each
later planner prompt said "Why it failed: profile produced no trusted evidence" and showed only
the head of the capture log, so the planner profiled again. These properties render the planner
prompt from generated profile results (passed, failed, finished with no reading, running).
"""

from __future__ import annotations

import string
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS
from tests.vibesys.orchestration.dynamic.prompt_variables import context_variables
from tests.vibesys.orchestration.dynamic.strategy._views import empty_view, evidence_ref, revision

import vibesys.orchestration.dynamic.core_policy.prompts as templates
from vibesys.orchestration.dynamic.core_policy.api import run_prompt_variables

# test-isolation: planner_prompt is the strategy's internal projection of its ledger into the
# planner's typed context; no public entry accepts a hand-built ledger, and the production
# shell cannot script a profile that finished with no reading or one with arbitrary statistics.
from vibesys.orchestration.dynamic.strategy._context import planner_prompt
from vibesys.orchestration.dynamic.strategy._state import (
    AttemptRecord,
    DynamicStrategyState,
    WorkKind,
    WorkPhase,
    WorkPlan,
)
from vibesys.orchestration.dynamic.strategy.api import (
    AcceptedReading,
    DynamicConfig,
    MetricRow,
    PlannerPrompt,
    ProfileHistoryRow,
    ProfileHistoryStatus,
)
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    AttemptId,
    EvidenceKind,
    RunView,
)
from vs_prompts.api import TemplateRenderer

FAILURE_TEXT = "profile produced no trusted evidence"
_WORDS = string.ascii_letters + string.digits + " "
_CONFIG = DynamicConfig(
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
    max_rounds=3,
    profiling=True,
)


def _text(prompt: PlannerPrompt) -> str:
    variables = context_variables(prompt)
    root = Path(templates.__file__).parent
    return TemplateRenderer(root, fallback_roots=(CORE_PROTOCOL_DIR,)).render_template(
        "portfolio.j2",
        **variables,
        **run_prompt_variables(
            domain_sections=EMPTY_DOMAIN_SECTIONS,
            objective="Raise throughput.",
            environment_notes="",
            agent_evaluation=False,
        ),
    )


def _metrics() -> st.SearchStrategy[tuple[MetricRow, ...]]:
    row = st.builds(
        MetricRow,
        name=st.text(string.ascii_lowercase, min_size=3, max_size=10),
        value=st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False),
        direction=st.sampled_from(["max", "min"]),
        unit=st.sampled_from(["fraction", "ms"]),
    )
    return st.lists(row, max_size=6, unique_by=lambda item: item.name).map(tuple)


def _prompt(
    *,
    phase: WorkPhase,
    reading: AcceptedReading | None,
    failure: str | None = None,
    required_fields: tuple[str, ...] = (),
    artifacts: tuple[ArtifactRef, ...] = (),
) -> PlannerPrompt:
    plan = WorkPlan(
        kind=WorkKind.PROFILE,
        work_id="prof-1",
        question="Where does the time go?",
        decision_impact="Pick the next kernel to optimize.",
        required_fields=required_fields,
    )
    ref = evidence_ref("profile-evidence", EvidenceKind.PROFILING, revision("parent")).model_copy(
        update={"purpose": "profile", "artifacts": artifacts}
    )
    record = AttemptRecord(
        plan=plan,
        sequence=1,
        attempt=AttemptId(root="attempt-1"),
        parent=revision("parent"),
        phase=phase,
        generation=1,
        failure=failure,
        evidence=() if reading is None else (ref.key,),
        readings=() if reading is None else (reading,),
    )
    view: RunView = empty_view().model_copy(update={"measurements": (ref,)})
    state = DynamicStrategyState(attempts=(record,))
    return planner_prompt(state, _CONFIG, view)


def _reading(*, passed: bool, metrics: tuple[MetricRow, ...], feedback: str) -> AcceptedReading:
    ref = evidence_ref("profile-evidence", EvidenceKind.PROFILING, revision("parent"))
    return AcceptedReading(
        source_request=ref.source_request,
        evidence_id=ref.evidence_id,
        kind=EvidenceKind.PROFILING,
        passed=passed,
        stage="profile",
        metrics=metrics,
        feedback=feedback,
    )


def _row(prompt: PlannerPrompt) -> ProfileHistoryRow:
    (row,) = [item for item in prompt.history if getattr(item, "kind", None) == "profile"]
    assert isinstance(row, ProfileHistoryRow)
    return row


@given(
    metrics=_metrics(),
    output=st.text(_WORDS, max_size=6000),
    fields=st.lists(st.sampled_from(["hip_api_timing"]), max_size=1, unique=True).map(tuple),
    artifact_names=st.lists(st.text(string.ascii_lowercase, min_size=3, max_size=8), max_size=3),
)
def test_a_passed_profile_is_observed_evidence_whatever_its_output_length(
    metrics: tuple[MetricRow, ...],
    output: str,
    fields: tuple[str, ...],
    artifact_names: list[str],
) -> None:
    artifacts = tuple(
        ArtifactRef(artifact_id=ArtifactId(root=f"art-{name}"), digest="d")
        for name in artifact_names
    )
    prompt = _prompt(
        phase=WorkPhase.DONE,
        reading=_reading(passed=True, metrics=metrics, feedback=output),
        required_fields=fields,
        artifacts=artifacts,
    )
    row = _row(prompt)
    rendered = _text(prompt)

    assert row.status is ProfileHistoryStatus.OBSERVED
    assert row.failure == ""
    assert FAILURE_TEXT not in rendered
    assert "Why it failed" not in rendered
    # The verdict, the typed statistics, the fields and the artifacts are never cut.
    assert "Verdict: the trusted profile succeeded" in rendered
    for field in fields:
        assert field in rendered
    for item in metrics:
        assert f"{item.name} = {item.value}" in rendered
    for item in artifacts:
        assert item.artifact_id.root in rendered
    # Only the raw output is bounded, and the cut keeps its end, where the tables are.
    if output:
        assert output[-40:].strip() in rendered
    assert len(row.diagnosis) < 1300


@given(
    feedback=st.text(_WORDS, min_size=1, max_size=3000),
    metrics=_metrics(),
)
def test_a_failed_profile_states_its_failure_and_no_success_evidence(
    feedback: str, metrics: tuple[MetricRow, ...]
) -> None:
    prompt = _prompt(
        phase=WorkPhase.DONE, reading=_reading(passed=False, metrics=metrics, feedback=feedback)
    )
    row = _row(prompt)
    rendered = _text(prompt)

    assert row.status is ProfileHistoryStatus.FAILED
    assert "Why it failed" in rendered
    assert feedback[-40:].strip() in rendered
    assert "Verdict: the trusted profile succeeded" not in rendered
    assert row.metrics == ()


def test_a_finished_profile_with_no_reading_is_a_failure_with_the_fallback_text() -> None:
    prompt = _prompt(phase=WorkPhase.DONE, reading=None)
    assert _row(prompt).status is ProfileHistoryStatus.FAILED
    assert FAILURE_TEXT in _text(prompt)


@given(failure=st.text(_WORDS, min_size=1, max_size=200))
def test_a_profile_that_ended_in_failure_reports_the_attempts_own_reason(failure: str) -> None:
    prompt = _prompt(phase=WorkPhase.DONE, reading=None, failure=failure)
    assert failure.strip()[-40:] in _text(prompt)


def test_a_running_profile_carries_neither_failure_nor_success_evidence() -> None:
    prompt = _prompt(phase=WorkPhase.PROFILE, reading=None)
    rendered = _text(prompt)
    assert _row(prompt).status is ProfileHistoryStatus.RUNNING
    assert FAILURE_TEXT not in rendered
    assert "Why it failed" not in rendered
    assert "Verdict: the trusted profile succeeded" not in rendered
