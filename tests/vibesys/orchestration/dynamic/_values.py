"""Generated values of the dynamic search: plans, ledgers, evidence, parents and run records.

The pure helpers of the search take plain values, so their tests draw those values here and call
the helpers directly. Identifiers come from a tiny alphabet on purpose: collisions (a repeated
hypothesis, a reused profile ID) are the interesting cases, and a large alphabet never makes
them.
"""

from __future__ import annotations

from typing import Literal

from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import TERMS

from vibesys.candidates.api import InputReading, Measured
from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.metrics import AcceptedReading, MetricRow, PartialRow
from vibesys.orchestration.dynamic.models import (
    CombinePlan,
    MergePlan,
    PlannerHandoff,
    PlannerHypothesisUpdate,
    PortfolioPlan,
    ProfileDecision,
    StrategyReason,
    WorkstreamExpectation,
    WorkstreamPlan,
)
from vibesys.orchestration.dynamic.search.api import (
    Candidate,
    DynamicConfig,
    Evidence,
    FinishedWork,
    HypothesisRecord,
    ParentSnapshot,
    Proof,
    Result,
    RoundRecord,
    RunRecord,
    Verdict,
    WorkKind,
    WorkPlan,
    WorkstreamDone,
    WorkstreamStart,
    workstream_key,
)
from vs_core.api import (
    AttemptId,
    AttemptRef,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    InvocationId,
    InvocationRef,
    ObservationStatus,
    RequestId,
    RevisionId,
    RevisionRef,
    RunId,
    Scope,
    SessionId,
    Settlement,
    SettlementId,
)

IDS = st.sampled_from("abcdefgh")
"""Hypothesis and profile IDs: few, so plans collide with the ledger and with themselves."""
SCOPE = Scope(owner=RunId(root="run"), generation=0)
BASE = RevisionRef(revision_id=RevisionId(root="base"), digest="digest-base")


def revision(name: str) -> RevisionRef:
    """A revision named ``name`` (id and digest follow the name)."""
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"digest-{name}")


def evidence_ref(
    name: str,
    kind: EvidenceKind,
    candidate: RevisionRef,
    *,
    request: str | None = None,
    digest: str = "x",
    **changes: object,
) -> EvidenceRef:
    """One trusted, succeeded, official evidence record of ``candidate``."""
    ref = EvidenceRef(
        evidence_id=EvidenceId(root=name),
        kind=kind,
        purpose="official",
        scope=SCOPE,
        source_request=RequestId(root=request or f"request-{name}"),
        candidate=candidate,
        observation_sequence=1,
        evaluator_digest=f"evaluator-{digest}",
        workload_digest=f"workload-{digest}",
        environment_digest=f"environment-{digest}",
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )
    return ref.model_copy(update=changes)


def reading_of_ref(
    ref: EvidenceRef,
    *,
    passed: bool = True,
    metrics: tuple[MetricRow, ...] = (),
    partial: PartialRow | None = None,
    feedback: str = "",
) -> AcceptedReading:
    """The decoded reading of ``ref``, bound to its request."""
    return AcceptedReading(
        source_request=ref.source_request,
        evidence_id=ref.evidence_id,
        kind=ref.kind,
        passed=passed,
        stage=ref.kind.value,
        metrics=metrics,
        partial=partial,
        feedback=feedback,
    )


def partial_row(
    value: float, *, completed: int = 71, direction: Literal["max", "min"] = "max"
) -> PartialRow:
    """A partial throughput reading of a benchmark that did not finish."""
    return PartialRow(
        name="throughput",
        value=value,
        direction=direction,
        unit="tokens/s",
        completed=completed,
        required=72,
        progress_unit="rounds",
    )


def snapshot(
    hypothesis: str,
    ordinal: int,
    *,
    value: float | None = None,
    direction: Literal["max", "min"] = "max",
    digest: str = "x",
) -> ParentSnapshot:
    """The retained revision ``ordinal`` of ``hypothesis`` with the evidence that proves it.

    With a ``value`` it carries a benchmark whose partial reading has that value.
    """
    rev = revision(f"{hypothesis}-r{ordinal}")
    accuracy_ref = evidence_ref(f"accuracy-{hypothesis}-{ordinal}", EvidenceKind.CORRECTNESS, rev)
    accuracy_ref = accuracy_ref.model_copy(
        update={
            "evaluator_digest": f"evaluator-{digest}",
            "workload_digest": f"workload-{digest}",
            "environment_digest": f"environment-{digest}",
        }
    )
    proof = [accuracy_ref]
    benchmark = None
    if value is not None:
        ref = evidence_ref(
            f"benchmark-{hypothesis}-{ordinal}", EvidenceKind.BENCHMARK, rev, digest=digest
        )
        proof.append(ref)
        benchmark = reading_of_ref(
            ref,
            passed=False,
            partial=partial_row(value, completed=71 if ordinal == 1 else 65, direction=direction),
            metrics=(MetricRow(name="throughput", value=value, direction=direction),),
        )
    return ParentSnapshot(
        hypothesis_id=hypothesis,
        revision=rev,
        submission_index=ordinal,
        accuracy=reading_of_ref(accuracy_ref),
        benchmark=benchmark,
        proof=tuple(proof),
    )


@st.composite
def snapshots(draw: st.DrawFn) -> ParentSnapshot:
    """A buildable retained revision of some hypothesis, with or without a benchmark partial."""
    value = draw(st.none() | st.floats(-1000, 1000, allow_nan=False, allow_infinity=False))
    return snapshot(
        draw(IDS),
        draw(st.integers(1, 6)),
        value=value,
        direction=draw(st.sampled_from(["max", "min"])),
        digest=draw(st.sampled_from("xy")),
    )


# -- plans -------------------------------------------------------------------------------------


def implement_entry(
    identifier: str, *, task: str | None = None, **changes: object
) -> WorkstreamPlan:
    """A planner entry that implements hypothesis ``identifier``."""
    fields: dict[str, object] = {
        "hypothesis_id": identifier,
        "title": f"title {identifier}",
        "hypothesis": f"claim {identifier}",
        "task": task or f"task {identifier}",
        "pass_criteria": "faster",
        **changes,
    }
    return WorkstreamPlan.model_validate(fields)


def profile_entry(identifier: str, **changes: object) -> ProfileDecision:
    """A planner entry that profiles the base or a hypothesis's revision."""
    fields: dict[str, object] = {
        "kind": "profile",
        "profile_id": identifier,
        "target_hypothesis_id": None,
        "question": "Where does the time go?",
        "decision_impact": "Pick the next kernel.",
        **changes,
    }
    return ProfileDecision.model_validate(fields)


def merge_entry(identifier: str, *candidates: str, **changes: object) -> MergePlan:
    """A planner entry that merges the latest verified candidates of ``candidates`` into main."""
    fields: dict[str, object] = {
        "kind": "merge",
        "merge_id": identifier,
        "candidates": list(candidates),
        "instructions": "Apply them in order.",
        "watch_for": "Both touch the scheduler.",
        **changes,
    }
    return MergePlan.model_validate(fields)


def combine_entry(identifier: str, *parts: tuple[str, str], **changes: object) -> CombinePlan:
    """A planner entry that combines the exact (hypothesis, revision) ``parts`` into one candidate."""
    fields: dict[str, object] = {
        "kind": "combine",
        "hypothesis_id": identifier,
        "title": f"title {identifier}",
        "hypothesis": "the parts work together",
        "parents": [{"hypothesis_id": item, "revision": revision} for item, revision in parts],
        "instructions": "Apply them in order.",
        "watch_for": "Both touch the scheduler.",
        "pass_criteria": "faster than the best part",
        **changes,
    }
    return CombinePlan.model_validate(fields)


def update_entry(
    identifier: str, disposition: Literal["parked", "abandoned"] = "parked"
) -> PlannerHypothesisUpdate:
    """A planner decision to park or abandon a finished hypothesis."""
    return PlannerHypothesisUpdate(
        hypothesis_id=identifier,
        disposition=disposition,
        reason="no further progress",
        reason_kind=StrategyReason.BLOCKED,
    )


def portfolio(
    *entries: WorkstreamPlan | ProfileDecision | MergePlan | CombinePlan,
    updates: tuple[PlannerHypothesisUpdate, ...] = (),
    expected: tuple[str, ...] = (),
    order: tuple[str, ...] = (),
) -> PortfolioPlan:
    """A planner reply of ``entries`` and ``updates``, expecting something of ``expected``
    and ranking evaluations in ``order``."""
    return PortfolioPlan(
        reasoning="portfolio",
        workstreams=entries,
        hypothesis_updates=updates,
        handoff=PlannerHandoff(
            expectations=tuple(
                WorkstreamExpectation(
                    work_id=item, expected="e", if_it_succeeds="s", if_it_fails="f"
                )
                for item in expected
            ),
            doubts=(),
        ),
        evaluation_order=order,
    )


@st.composite
def planner_entries(draw: st.DrawFn) -> WorkstreamPlan | ProfileDecision | MergePlan:
    """An implement, profile or merge entry over a small ID space, with parents drawn from it too."""
    identifier = draw(IDS)
    kind = draw(st.sampled_from(["implement", "profile", "merge"]))
    if kind == "profile":
        return profile_entry(identifier, target_hypothesis_id=draw(st.none() | IDS))
    if kind == "merge":
        return merge_entry(identifier, *draw(st.lists(IDS, min_size=1, max_size=3)))
    return implement_entry(
        identifier,
        continue_hypothesis=draw(st.booleans()),
        parent_hypothesis_id=draw(st.none() | IDS),
        parent_revision=draw(st.none() | st.sampled_from(["a-r1", "b-r2", "gone"])),
        task=draw(st.sampled_from(["same", "other"])),
    )


portfolios = st.builds(
    lambda entries, updates: portfolio(*entries, updates=tuple(updates)),
    st.lists(planner_entries(), min_size=1, max_size=6),
    st.lists(st.builds(update_entry, IDS, st.sampled_from(["parked", "abandoned"])), max_size=3),
)


# -- the record of a run -----------------------------------------------------------------------


def config(**changes: object) -> DynamicConfig:
    """A search config of a few rounds, overridden by the test."""
    fields = {"max_rounds": 3, "max_in_flight": 2}
    return DynamicConfig.model_validate({**fields, **changes})


def round_record(
    sequence: int,
    outcome: HypothesisOutcome | None,
    *,
    candidate: RevisionRef | None = None,
    eligible: bool = False,
) -> RoundRecord:
    """One finished round with this outcome."""
    return RoundRecord(
        sequence=sequence,
        attempt=AttemptId(root=f"attempt-{sequence}"),
        outcome=outcome,
        candidate=candidate,
        eligible=eligible,
    )


def hypothesis_record(
    identifier: str,
    *outcomes: HypothesisOutcome | None,
    task: str = "same",
    strategy: HypothesisStrategy = HypothesisStrategy.AVAILABLE,
) -> HypothesisRecord:
    """A hypothesis that has finished one round per ``outcomes``."""
    return HypothesisRecord(
        hypothesis_id=identifier,
        title=f"title {identifier}",
        hypothesis=f"claim {identifier}",
        first_sequence=1,
        last_task=task,
        strategy=strategy,
        rounds=tuple(round_record(index, item) for index, item in enumerate(outcomes, 1)),
    )


def finished_profile(identifier: str, sequence: int) -> FinishedWork:
    """A profile workstream that ended."""
    return FinishedWork(
        sequence=sequence,
        plan=WorkPlan(kind=WorkKind.PROFILE, work_id=identifier, question="q"),
        parent=BASE,
    )


def finished_merge(identifier: str, sequence: int) -> FinishedWork:
    """A merge workstream that ended."""
    return FinishedWork(
        sequence=sequence,
        plan=WorkPlan(kind=WorkKind.MERGE, work_id=identifier, candidates=("a",)),
        parent=BASE,
    )


def run_record(cfg: DynamicConfig | None = None, **changes: object) -> RunRecord:
    """An empty record of a run whose input measured fine; ``changes`` set its fields."""
    record = RunRecord(
        cfg or config(),
        BASE,
        Measured(reading=InputReading(accuracy_passed=True, benchmark_passed=True)),
        TERMS,
    )
    for name, value in changes.items():
        setattr(record, name, value)
    return record


# -- finished workstreams ------------------------------------------------------------------------

CANDIDATE = revision("candidate")
BASELINE_THROUGHPUT = 50.0


def workstream_start(
    kind: WorkKind = WorkKind.IMPLEMENT, sequence: int = 1, work_id: str = "w"
) -> WorkstreamStart:
    baseline = Measured(
        reading=InputReading(
            accuracy_passed=True,
            benchmark_passed=True,
            metrics=(MetricRow(name="throughput", value=BASELINE_THROUGHPUT, direction="max"),),
        )
    )
    return WorkstreamStart(
        key=workstream_key(sequence, work_id),
        sequence=sequence,
        plan=WorkPlan(kind=kind, work_id=work_id),
        parent=BASE,
        parent_hypothesis_id=None,
        prior=None,
        config=config(),
        baseline=baseline,
    )


def evidence_of(
    *,
    accuracy: bool | None,
    benchmark: bool | None,
    throughput: float,
    candidate: RevisionRef = CANDIDATE,
) -> Evidence:
    """The trusted readings and the proof of a measured ``candidate``."""
    tag = candidate.revision_id.root
    readings = []
    refs = []
    if accuracy is not None:
        ref = evidence_ref(f"accuracy-{tag}", EvidenceKind.CORRECTNESS, candidate)
        refs.append(ref)
        readings.append(reading_of_ref(ref, passed=accuracy))
    if benchmark is not None:
        ref = evidence_ref(f"benchmark-{tag}", EvidenceKind.BENCHMARK, candidate)
        refs.append(ref)
        metric = MetricRow(name="throughput", value=throughput, direction="max")
        readings.append(reading_of_ref(ref, passed=benchmark, metrics=(metric,)))
    return Evidence(readings=tuple(readings), proof=Proof(refs=tuple(refs)))


def result_of(
    outcome: HypothesisOutcome, *, review: bool | None, revision_: RevisionRef | None = CANDIDATE
) -> Result:
    verdict = (
        None
        if review is None
        else Verdict(
            passed=review,
            invocation=InvocationRef(
                session_id=SessionId(root="s"), invocation_id=InvocationId(root="i"), generation=0
            ),
        )
    )
    return Result(
        candidate=Candidate(outcome=outcome, summary="done", revision=revision_),
        verdict=verdict,
    )


def settlement_of(
    *, eligible: bool, retention: str, candidate: RevisionRef | None = CANDIDATE, name: str = "1"
) -> Settlement:
    return Settlement.model_validate(
        {
            "settlement_id": SettlementId(root=f"settlement-{name}"),
            "attempt": AttemptRef(attempt_id=AttemptId(root=f"attempt-{name}"), generation=3),
            "candidate": candidate,
            "assessments": (),
            "eligible": eligible,
            "retention": retention,
            "outcome": "succeeded",
        }
    )


def finished_workstream(
    *,
    accuracy: bool | None,
    retention: str,
    kind: WorkKind = WorkKind.IMPLEMENT,
    with_proof: bool = True,
    eligible: bool = False,
) -> WorkstreamDone:
    shown = evidence_of(accuracy=accuracy, benchmark=True, throughput=100.0)
    if not with_proof:
        shown = Evidence(readings=shown.readings)
    return WorkstreamDone(
        start=workstream_start(kind),
        failure=None,
        generation=3,
        settlement=settlement_of(eligible=eligible, retention=retention),
        result=result_of(HypothesisOutcome.SUPPORTED, review=True),
        evidence=shown,
    )
