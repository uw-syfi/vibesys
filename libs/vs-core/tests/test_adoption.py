"""Adoption through the public kernel: select, adopt, verify, complete once.

Every event goes through ``core.step`` and every state is reloaded through the
public codec between steps. Executor answers are modelled as generated
observations, including duplicates, stale repeats, unknown and failed outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import sample_limits
from vs_sim.api.testing import examples

from .restart import restarts

RUN_SCOPE = core.Scope(owner=core.RunId(root="run"), generation=0)
ATTEMPT_SCOPE = core.Scope(owner=core.AttemptId(root="attempt"), generation=0)


def rev(name: str) -> core.RevisionRef:
    return core.RevisionRef(revision_id=core.RevisionId(root=name), digest=f"digest-{name}")


def reload(state: core.CoreState) -> core.CoreState:
    if not restarts():
        return state
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    codec = core.OperationRegistry()
    return codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    ).core


def settled(
    name: str, *, eligible: bool, retention: Literal["wip", "candidate"], revision: str
) -> core.Settlement:
    return core.Settlement(
        settlement_id=core.SettlementId(root=name),
        attempt=core.AttemptRef(attempt_id=core.AttemptId(root=f"attempt-{name}"), generation=0),
        candidate=rev(revision),
        assessments=(),
        eligible=eligible,
        retention=retention,
        outcome="succeeded" if eligible else "failed",
    )


def accuracy_proof(revision: str) -> core.EvidenceRef:
    request = core.RequestId(root=f"measure-{revision}")
    observation = core.Observation(
        event_id=core.EventId(root=f"event-{revision}"),
        request_id=request,
        scope=ATTEMPT_SCOPE,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
    )
    return core.EvidenceRef(
        evidence_id=core.EvidenceId(root=f"accuracy-{revision}"),
        kind=core.EvidenceKind.CORRECTNESS,
        purpose="official",
        scope=ATTEMPT_SCOPE,
        source_request=request,
        candidate=rev(revision),
        observation_sequence=1,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        provenance="trusted",
        status=core.ObservationStatus.SUCCEEDED,
        acceptance_receipt=core.EvidenceAcceptanceReceipt(observation=observation),
    )


# Two frozen parents with accepted accuracy, a better failed partial that is only
# work in progress, and a candidate that was marked eligible but has no proof.
SETTLEMENTS = (
    settled("parent-a", eligible=True, retention="candidate", revision="rev-a"),
    settled("parent-b", eligible=True, retention="candidate", revision="rev-b"),
    settled("partial", eligible=False, retention="wip", revision="rev-partial"),
    settled("unproven", eligible=True, retention="candidate", revision="rev-unproven"),
)

GOOD: tuple[core.Selection, ...] = (
    core.RetainedCandidate(settlement_id=core.SettlementId(root="parent-a"), revision=rev("rev-a")),
    core.RetainedCandidate(settlement_id=core.SettlementId(root="parent-b"), revision=rev("rev-b")),
    core.TrustedBaseline(revision=core.initial_state().run.facts.baseline),
)
BAD: tuple[core.Selection, ...] = (
    core.RetainedCandidate(
        settlement_id=core.SettlementId(root="partial"), revision=rev("rev-partial")
    ),
    core.RetainedCandidate(
        settlement_id=core.SettlementId(root="unproven"), revision=rev("rev-unproven")
    ),
    core.RetainedCandidate(
        settlement_id=core.SettlementId(root="parent-a"), revision=rev("rev-partial")
    ),
    core.RetainedCandidate(settlement_id=core.SettlementId(root="missing"), revision=rev("rev-a")),
    core.TrustedBaseline(revision=rev("foreign")),
)
SELECTIONS = (*GOOD, *BAD)


def world(max_retries: int = 0) -> core.CoreState:
    state = core.initial_state()
    return state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "limits": sample_limits(max_adoption_rounds=max_retries + 1, max_attempts=4)
                }
            ),
            "settlement": core.SettlementState(settlements=SETTLEMENTS),
            "evaluation": core.EvaluationState(
                evidence=(accuracy_proof("rev-a"), accuracy_proof("rev-b"))
            ),
        }
    )


@dataclass
class Run:
    """A strategy and an executor driving the core one public step at a time."""

    state: core.CoreState
    requests: list[core.Request] = field(default_factory=list)
    events: list[core.StrategyEvent] = field(default_factory=list)
    delivered: list[core.AdoptionObserved] = field(default_factory=list)
    sequences: dict[str, int] = field(default_factory=dict)
    proposals: int = 0

    def feed(self, event: core.CoreEvent) -> core.Transition:
        transition = core.step(self.state, event)
        assert transition == core.step(reload(self.state), event)
        self.state = reload(transition.state)
        self.requests.extend(transition.requests)
        self.events.extend(transition.events)
        return transition

    def propose(self, selection: core.Selection) -> core.Transition:
        self.proposals += 1
        decision = core.ProposeWinner(
            decision_id=core.DecisionId(root=f"winner-{self.proposals}"),
            scope=RUN_SCOPE,
            selection=selection,
        )
        return self.feed(
            core.DecisionSubmitted(
                decision=core.Wait(request=decision, until=core.Within(limit=100.0)),
                expected_revision=self.state.revision,
            )
        )

    def answer(
        self,
        request: core.Request,
        status: core.ObservationStatus,
        *,
        revision: core.RevisionRef | None = None,
        terminal: bool = True,
    ) -> core.Transition:
        assert request.request_id is not None
        key = request.request_id.root
        self.sequences[key] = self.sequences.get(key, 0) + 1
        observation = core.Observation(
            event_id=core.EventId(root=f"{key}:{self.sequences[key]}"),
            request_id=request.request_id,
            scope=RUN_SCOPE,
            sequence=self.sequences[key],
            observed_at=2.0,
            status=status,
            accepted=True,
            terminal=terminal,
        )
        event = core.AdoptionObserved(observation=observation, revision=revision)
        self.delivered.append(event)
        return self.feed(event)

    def adoption_requests(self) -> list[core.AdoptRevision | core.VerifyAdoption]:
        return [
            request
            for request in self.requests
            if isinstance(request, core.AdoptRevision | core.VerifyAdoption)
        ]

    def results(self) -> list[core.AdoptionResult]:
        return [event for event in self.events if isinstance(event, core.AdoptionResult)]

    def failures(self) -> list[core.AdoptionFailed]:
        return [event for event in self.events if isinstance(event, core.AdoptionFailed)]

    def view(self) -> core.AdoptionView:
        view = core.project(self.state).adoption
        assert view is not None
        return view

    def rejections(self) -> list[core.Rejected]:
        return [event for event in self.events if isinstance(event, core.Rejected)]


def start(max_retries: int = 0) -> Run:
    return Run(world(max_retries))


def new_requests(transition: core.Transition) -> list[core.AdoptRevision | core.VerifyAdoption]:
    return [
        request
        for request in transition.requests
        if isinstance(request, core.AdoptRevision | core.VerifyAdoption)
    ]


def test_a_winner_completes_only_after_a_positive_verification() -> None:
    run = start()
    selection = GOOD[0]
    proposed = run.propose(selection)
    (adopt,) = new_requests(proposed)
    assert isinstance(adopt, core.AdoptRevision)
    assert adopt.selection == selection
    assert adopt.scope == RUN_SCOPE
    assert adopt.decision_id == core.DecisionId(root="winner-1")
    applied = run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    (verify,) = new_requests(applied)
    assert isinstance(verify, core.VerifyAdoption)
    assert run.results() == []
    done = run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    (result,) = run.results()
    assert result.selection == selection
    assert result.observation.request_id == verify.request_id
    assert done.state.settlement.adoption == core.Adoption(
        selection=selection, observation=result.observation, verified=True
    )
    # Duplicates and a repeated proposal never complete or adopt a second time.
    assert (
        run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision).events
        == ()
    )
    assert run.propose(selection).requests == ()
    assert len(run.results()) == 1


def test_a_candidate_with_an_agent_record_and_the_official_accuracy_record_is_adopted() -> None:
    """The live path holds both: the agent's own evaluation and the official one, as samples."""
    agent = accuracy_proof("rev-a").model_copy(
        update={
            "kind": core.EvidenceKind.CORRECTNESS,
            "purpose": "local-validation",
            "evidence_id": core.EvidenceId(root="agent-rev-a"),
        }
    )
    state = world()
    both = core.EvaluationState(evidence=(agent, *state.evaluation.evidence))
    run = Run(state.model_copy(update={"evaluation": both}))
    (adopt,) = new_requests(run.propose(GOOD[0]))
    assert run.rejections() == []
    assert adopt.selection == GOOD[0]


def test_a_successful_result_must_name_the_verified_adopted_winner() -> None:
    run = start()
    selection = GOOD[0]
    result = core.RunResultProposal(outcome="success", reason="done", selection=selection)

    def stop() -> list[core.Rejected]:
        decision = core.Finish(
            decision_id=core.DecisionId(root=f"stop-{len(run.state.run.receipts)}"),
            scope=RUN_SCOPE,
            mode="drain",
            result=result,
        )
        event = core.DecisionSubmitted(decision=decision, expected_revision=run.state.revision)
        return [e for e in core.step(run.state, event).events if isinstance(e, core.Rejected)]

    (early,) = stop()
    assert early.code == core.RejectionCode.EVIDENCE
    assert early.path == ("result", "selection")
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    assert [r.path for r in stop()] == [("result", "selection")]
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    assert stop() == []
    result = core.RunResultProposal(outcome="success", reason="done", selection=GOOD[1])
    assert [r.path for r in stop()] == [("result", "selection")]


def test_the_trusted_baseline_is_adopted_the_same_way() -> None:
    run = start()
    selection = GOOD[2]
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    assert [result.selection for result in run.results()] == [selection]


def test_a_better_failed_partial_is_never_adopted_over_the_frozen_parents() -> None:
    """r23: the best score was a failed partial and a lower-scoring parent won."""
    run = start()
    for index, selection in enumerate(BAD, start=1):
        transition = run.propose(selection)
        assert new_requests(transition) == []
        (rejection,) = [e for e in transition.events if isinstance(e, core.Rejected)]
        assert rejection.decision_id == core.DecisionId(root=f"winner-{index}")
        assert rejection.code == core.RejectionCode.EVIDENCE
        assert transition.state.settlement.adoption is None
    assert run.adoption_requests() == []
    good = run.propose(GOOD[1])
    (adopt,) = new_requests(good)
    assert adopt.selection == GOOD[1]


UNPROVEN = BAD[1]
"""A retained, eligible candidate with no accepted accuracy proof."""


def task_measuring(*measured: str) -> core.TaskTerms:
    """A task over the two gate stages that measures only ``measured`` of them."""
    return core.TaskTerms(
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        stages=tuple(core.TaskStage(stage_id=name, execution_seconds=1.0) for name in GATES),
        measured=measured,
        queue_allowance=1.0,
        accuracy_stage="accuracy" if "accuracy" in measured else None,
        operation_seconds=1.0,
    )


GATES = ("accuracy", "benchmark")


@given(
    measured=st.lists(st.sampled_from(GATES), unique=True).map(tuple),
    declared=st.booleans(),
)
def test_an_accuracy_proof_is_asked_only_of_a_task_that_measures_accuracy(
    measured: tuple[str, ...], *, declared: bool
) -> None:
    """The proof is the accuracy gate's product: a task without that gate cannot supply one."""
    state = world()
    facts = state.run.facts.model_copy(
        update={"task": task_measuring(*measured) if declared else None}
    )
    run = Run(state.model_copy(update={"run": state.run.model_copy(update={"facts": facts})}))

    transition = run.propose(UNPROVEN)

    needs_proof = not declared or "accuracy" in measured
    assert bool(new_requests(transition)) is not needs_proof
    assert bool([e for e in transition.events if isinstance(e, core.Rejected)]) is needs_proof


def test_applied_but_unverified_is_recovered_by_verify_not_by_adopting_again() -> None:
    run = start()
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    assert run.propose(selection).requests == ()
    # A ledger that lost the verify record gets exactly that request back, never AdoptRevision.
    lost = run.state.model_copy(
        update={
            "records": run.state.records.model_copy(
                update={
                    "records": tuple(
                        row
                        for row in run.state.records.records
                        if row.request_id != verify.request_id
                    )
                }
            )
        }
    )
    recovered = core.step(
        lost,
        core.DecisionSubmitted(
            decision=core.Wait(
                request=core.ProposeWinner(
                    decision_id=core.DecisionId(root="again"), scope=RUN_SCOPE, selection=selection
                ),
                until=core.Within(limit=100.0),
            ),
            expected_revision=lost.revision,
        ),
    )
    assert [r.request_id for r in new_requests(recovered)] == [verify.request_id]
    assert isinstance(new_requests(recovered)[0], core.VerifyAdoption)


def test_unknown_is_inspected_and_then_retried_within_the_retry_budget() -> None:
    run = start(max_retries=1)
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(run.answer(adopt, core.ObservationStatus.UNKNOWN, terminal=False))
    assert isinstance(verify, core.VerifyAdoption)
    (retry,) = new_requests(run.answer(verify, core.ObservationStatus.UNKNOWN, terminal=False))
    assert isinstance(retry, core.AdoptRevision)
    assert retry.request_id != adopt.request_id
    (second_verify,) = new_requests(
        run.answer(retry, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    assert second_verify.request_id != verify.request_id
    run.answer(second_verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    assert len(run.results()) == 1


def test_exhausted_retries_fail_the_round_and_release_the_fence() -> None:
    run = start(max_retries=0)
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(run.answer(adopt, core.ObservationStatus.UNKNOWN, terminal=False))
    failed = run.answer(verify, core.ObservationStatus.UNKNOWN, terminal=False)
    assert new_requests(failed) == []
    assert run.results() == []
    assert not _start_root_attempt(run).rejections_for_fence()
    (event,) = run.failures()
    assert event.reason == core.AdoptionFailureReason.RETRIES_EXHAUSTED
    assert event.rounds == 1
    # max_retries=0 allows one round, so proposing again is refused, not re-run.
    again = run.propose(selection)
    assert new_requests(again) == []
    assert [r.code for r in run.rejections()] == [core.RejectionCode.BUDGET]


def test_a_failed_round_is_reported_once_and_the_view_shows_it() -> None:
    run = start(max_retries=1)
    (adopt,) = new_requests(run.propose(GOOD[0]))
    assert (run.view().phase, run.view().rounds) == ("adopting", 1)
    run.answer(adopt, core.ObservationStatus.REJECTED)
    run.feed(run.delivered[-1])
    (event,) = run.failures()
    assert event.reason == core.AdoptionFailureReason.ADOPT_FAILED
    assert run.view().phase == "failed"
    assert run.view().failure == core.AdoptionFailureReason.ADOPT_FAILED


def test_re_proposals_share_the_retry_bound_with_automatic_retries() -> None:
    """D4: proposing again after a failure must not buy rounds beyond max_retries."""
    run = start(max_retries=2)
    selection = GOOD[0]
    for _ in range(3):
        (adopt,) = new_requests(run.propose(selection))
        run.answer(adopt, core.ObservationStatus.REJECTED)
    assert len(run.failures()) == 3
    assert new_requests(run.propose(selection)) == []
    assert run.rejections()[-1].code == core.RejectionCode.BUDGET
    assert run.view().rounds == 3
    # Another selection has its own rounds.
    assert len(new_requests(run.propose(GOOD[1]))) == 1


@pytest.mark.parametrize("revision", [None, rev("rev-b")])
def test_a_verification_naming_other_content_fails_fast_with_an_event(
    revision: core.RevisionRef | None,
) -> None:
    run = start(max_retries=3)
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    again = run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=revision)
    assert new_requests(again) == []
    (event,) = run.failures()
    assert event.reason == core.AdoptionFailureReason.CONTENT_MISMATCH
    assert not _start_root_attempt(run).rejections_for_fence()
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=revision)
    assert len(run.failures()) == 1


def test_a_wrong_revision_in_a_verification_never_completes() -> None:
    run = start(max_retries=3)
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=rev("rev-b"))
    assert run.results() == []
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=None)
    assert run.results() == []


def test_a_rejected_adoption_fails_without_verifying() -> None:
    run = start(max_retries=3)
    (adopt,) = new_requests(run.propose(GOOD[0]))
    failed = run.answer(adopt, core.ObservationStatus.REJECTED)
    assert new_requests(failed) == []
    assert run.results() == []


@dataclass
class _FenceProbe:
    run: Run
    rejected: list[core.Rejected]

    def rejections_for_fence(self) -> list[core.Rejected]:
        return [
            r
            for r in self.rejected
            if r.code == core.RejectionCode.DEPENDENCY and r.path == ("workspace",)
        ]


def _start_root_attempt(run: Run) -> _FenceProbe:
    state = run.state
    decision = core.StartAttempt(
        decision_id=core.DecisionId(root=f"start-{len(run.state.run.receipts)}"),
        scope=RUN_SCOPE,
        attempt_id=core.AttemptId(root=f"fenced-{len(run.state.run.receipts)}"),
        item_id=core.ItemId(root="item"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    transition = core.step(
        state,
        core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=100.0)),
            expected_revision=state.revision,
        ),
    )
    return _FenceProbe(
        run, [event for event in transition.events if isinstance(event, core.Rejected)]
    )


def test_no_other_root_mutation_is_authorized_while_an_adoption_is_underway() -> None:
    run = start()
    selection = GOOD[0]
    assert not _start_root_attempt(run).rejections_for_fence()
    (adopt,) = new_requests(run.propose(selection))
    assert _start_root_attempt(run).rejections_for_fence()
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    assert _start_root_attempt(run).rejections_for_fence()
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    assert not _start_root_attempt(run).rejections_for_fence()


def test_another_winner_is_refused_while_in_flight_and_after_completion() -> None:
    run = start()
    first, other = GOOD[0], GOOD[1]
    (adopt,) = new_requests(run.propose(first))
    busy = run.propose(other)
    assert new_requests(busy) == []
    assert [r.code for r in busy.events if isinstance(r, core.Rejected)] == [
        core.RejectionCode.IDENTITY_CONFLICT
    ]
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=first.revision)
    )
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=first.revision)
    after = run.propose(other)
    assert new_requests(after) == []
    assert [r.code for r in after.events if isinstance(r, core.Rejected)] == [
        core.RejectionCode.ALREADY_SETTLED
    ]
    assert len(run.results()) == 1


def _adopted(run: Run, selection: core.Selection) -> None:
    """Propose ``selection`` and answer its adoption until it is verified."""
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)


def _control(action: Literal["stop", "resume"], index: int) -> core.RunControlEvent:
    identity = core.ControlId(root=f"op-{index}")
    control = (
        core.ControlInput(control_id=identity, action="stop", mode=core.StopMode.DRAIN)
        if action == "stop"
        else core.ControlInput(control_id=identity, action="resume", when="now")
    )
    return core.RunControlEvent(control=control, now_at=float(index))


@given(first=st.sampled_from(GOOD), later=st.sampled_from(GOOD))
@example(first=GOOD[0], later=GOOD[1])
@settings(max_examples=len(GOOD) ** 2, derandomize=True)
def test_a_lifted_stop_takes_back_the_adoption_it_completed(
    first: core.Selection, later: core.Selection
) -> None:
    """Regression: the winner of a run's later ending was refused as already settled.

    A stopped run adopted its best, a resume lifted the stop, and the run went on and found a
    better one. Its next ending proposed that winner, and core still held the first adoption as
    the run's answer, so it refused with ``already-settled`` and the run ended without adopting.
    """
    run = start()
    run.feed(_control("stop", 0))
    _adopted(run, first)
    run.feed(_control("resume", 1))
    assert run.state.settlement.adoption is None
    (adopt,) = new_requests(run.propose(later))
    assert adopt.selection == later
    assert run.rejections() == []
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=later.revision)
    )
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=later.revision)
    assert [result.selection for result in run.results()] == [first, later]


@given(first=st.sampled_from(GOOD), other=st.sampled_from(GOOD))
def test_an_adoption_no_lift_took_back_stays_the_runs_answer(
    first: core.Selection, other: core.Selection
) -> None:
    """A stop that stands keeps its adoption: only the lift reopens it."""
    run = start()
    run.feed(_control("stop", 0))
    _adopted(run, first)
    again = run.propose(other)
    assert new_requests(again) == []
    assert (first == other) or [r.code for r in run.rejections()] == [
        core.RejectionCode.ALREADY_SETTLED
    ]
    assert run.state.settlement.adoption is not None
    assert run.state.settlement.adoption.selection == first


def test_a_lift_leaves_an_adoption_underway_to_finish() -> None:
    run = start()
    run.feed(_control("stop", 0))
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    run.feed(_control("resume", 1))
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    assert [result.selection for result in run.results()] == [selection]


def test_a_winner_waits_while_an_attempt_still_holds_the_root() -> None:
    run = start()
    holder = core.AttemptView(
        attempt_id=core.AttemptId(root="holder"),
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=run.state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    run.state = run.state.model_copy(update={"attempts": core.AttemptsState(attempts=(holder,))})
    refused = run.propose(GOOD[0])
    assert new_requests(refused) == []
    assert [r.code for r in refused.events if isinstance(r, core.Rejected)] == [
        core.RejectionCode.DEPENDENCY
    ]


def test_an_observation_for_an_unknown_request_changes_nothing() -> None:
    run = start()
    (adopt,) = new_requests(run.propose(GOOD[0]))
    foreign = adopt.model_copy(update={"request_id": core.RequestId(root="foreign")})
    before = run.state
    transition = run.answer(foreign, core.ObservationStatus.SUCCEEDED, revision=GOOD[0].revision)
    assert transition.requests == ()
    assert transition.events == ()
    assert run.state.settlement == before.settlement


def test_a_bare_winner_event_is_validated_like_a_command() -> None:
    run = start()
    selection = GOOD[0]
    (adopt,) = new_requests(run.feed(core.WinnerProposed(selection=selection)))
    assert adopt.selection == selection
    assert adopt.decision_id is None
    bare = start()
    with pytest.raises(core.ContractValidationError):
        bare.feed(core.WinnerProposed(selection=BAD[0]))
    assert bare.state.settlement.adoption is None


def test_pending_observations_wait_and_late_repeats_change_nothing() -> None:
    run = start()
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    assert new_requests(run.answer(adopt, core.ObservationStatus.PENDING, terminal=False)) == []
    (verify,) = new_requests(
        run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    )
    assert new_requests(run.answer(verify, core.ObservationStatus.PENDING, terminal=False)) == []
    # A late adopt answer after verification started neither regresses nor re-verifies.
    late = run.answer(adopt, core.ObservationStatus.PENDING, terminal=False)
    assert late.requests == ()
    assert late.events == ()
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    assert len(run.results()) == 1


def test_a_later_adopt_answer_after_unknown_updates_without_a_second_verify() -> None:
    run = start()
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    (verify,) = new_requests(run.answer(adopt, core.ObservationStatus.UNKNOWN, terminal=False))
    later = run.answer(adopt, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    assert later.requests == ()
    adoption = run.state.settlement.adoption
    assert adoption is not None
    assert adoption.observation == run.delivered[-1].observation
    run.answer(verify, core.ObservationStatus.SUCCEEDED, revision=selection.revision)
    assert len(run.results()) == 1


def test_a_stale_sequence_cannot_overwrite_a_newer_answer() -> None:
    run = start()
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    run.answer(adopt, core.ObservationStatus.UNKNOWN, terminal=False)
    stale = run.delivered[0].model_copy(
        update={
            "observation": run.delivered[0].observation.model_copy(
                update={"sequence": 0, "status": core.ObservationStatus.REJECTED}
            )
        }
    )
    kept = run.state.settlement
    assert run.feed(stale).events == ()
    assert run.state.settlement == kept


STATUSES = (
    core.ObservationStatus.SUCCEEDED,
    core.ObservationStatus.UNKNOWN,
    core.ObservationStatus.PENDING,
    core.ObservationStatus.FAILED,
    core.ObservationStatus.REJECTED,
    core.ObservationStatus.CANCELLED,
)

actions = st.one_of(
    st.tuples(st.just("propose"), st.integers(0, len(SELECTIONS) - 1), st.just(0), st.just(0)),
    st.tuples(
        st.just("answer"),
        st.integers(0, 40),
        st.integers(0, len(STATUSES) - 1),
        st.integers(0, 2),
    ),
    st.tuples(st.just("repeat"), st.integers(0, 40), st.just(0), st.just(0)),
)


@settings(max_examples=examples(pr=5, nightly=60), deadline=None)
@given(
    plan=st.lists(actions, min_size=1, max_size=30),
    retries=st.integers(0, 2),
)
def test_adoption_properties_hold_for_any_order_of_proposals_and_observations(
    plan: list[tuple[str, int, int, int]], retries: int
) -> None:
    run = start(retries)
    good = set(GOOD)
    for kind, index, status, flavor in plan:
        if kind == "propose":
            run.propose(SELECTIONS[index])
        elif kind == "answer":
            pool = run.adoption_requests()
            if not pool:
                continue
            request = pool[index % len(pool)]
            revision = (
                request.selection.revision
                if flavor == 0
                else (rev("rev-b") if flavor == 1 else None)
            )
            run.answer(
                request,
                STATUSES[status],
                revision=revision,
                terminal=STATUSES[status] != core.ObservationStatus.UNKNOWN and index % 3 != 0,
            )
        elif run.delivered:
            run.feed(run.delivered[index % len(run.delivered)])
        # Never adopts an ineligible or partial revision.
        assert all(request.selection in good for request in run.adoption_requests())
        # Completes at most once, and only with the exact selection verified.
        results = run.results()
        assert len(results) <= 1
        # Rounds per selection never pass the first plus max_retries, however they began.
        for selection in SELECTIONS:
            rounds = sum(
                isinstance(request, core.AdoptRevision) and request.selection == selection
                for request in run.adoption_requests()
            )
            assert rounds <= retries + 1
        # A failure is reported once per terminal round, so never more often than rounds.
        assert len(run.failures()) <= len(
            [r for r in run.adoption_requests() if isinstance(r, core.AdoptRevision)]
        )
        adoption = run.state.settlement.adoption
        if adoption is not None and adoption.verified:
            assert len(results) == 1
            assert results[0].selection == adoption.selection
        for result in results:
            assert any(
                event.observation == result.observation
                and event.observation.status == core.ObservationStatus.SUCCEEDED
                and event.revision == result.selection.revision
                and any(
                    isinstance(request, core.VerifyAdoption)
                    and request.request_id == event.observation.request_id
                    for request in run.adoption_requests()
                )
                for event in run.delivered
            )
        # Request identities are unique per request.
        ids = [request.request_id for request in run.adoption_requests()]
        assert len(ids) == len(set(ids))


def _completion(run: Run, decision_id: core.DecisionId) -> core.CompletionStatus | None:
    receipt = next(item for item in run.state.run.receipts if item.decision_id == decision_id)
    return receipt.completion


def _executor_answer(
    run: Run,
    request: core.Request,
    status: core.ObservationStatus,
    revision: core.RevisionRef | None = None,
) -> core.Transition:
    """Answer through the record ledger, the way an executor's result enters the core."""
    assert request.request_id is not None
    key = request.request_id.root
    run.sequences[key] = run.sequences.get(key, 0) + 1
    if run.sequences[key] == 1:
        run.feed(core.DispatchAuthorized(request_id=request.request_id))
    observation = core.Observation(
        event_id=core.EventId(root=f"{key}:{run.sequences[key]}"),
        request_id=request.request_id,
        scope=RUN_SCOPE,
        sequence=run.sequences[key],
        observed_at=2.0,
        status=status,
        accepted=True,
        terminal=True,
        revision=revision,
    )
    return run.feed(core.RequestObserved(observation=observation, revision=revision))


def test_the_winner_decision_completes_only_after_its_adoption_is_verified() -> None:
    """RECORDS-A gap: completing on the restore alone would precede the verification."""
    run = start()
    selection = GOOD[0]
    (adopt,) = new_requests(run.propose(selection))
    decision = adopt.decision_id
    assert decision is not None
    assert _completion(run, decision) is None
    (verify,) = new_requests(
        _executor_answer(run, adopt, core.ObservationStatus.SUCCEEDED, selection.revision)
    )
    assert _completion(run, decision) is None
    _executor_answer(run, verify, core.ObservationStatus.SUCCEEDED, selection.revision)
    assert _completion(run, decision) == core.CompletionStatus.SUCCEEDED


def test_the_winner_decision_completes_as_failed_when_its_adoption_fails() -> None:
    run = start()
    (adopt,) = new_requests(run.propose(GOOD[0]))
    decision = adopt.decision_id
    assert decision is not None
    _executor_answer(run, adopt, core.ObservationStatus.REJECTED)
    assert run.failures()
    assert _completion(run, decision) == core.CompletionStatus.FAILED


_RECORD = st.tuples(
    st.sampled_from(("rev-a", "rev-b")),
    st.sampled_from(("baseline", "local-validation", "official", "profile")),
    st.sampled_from((core.ObservationStatus.SUCCEEDED, core.ObservationStatus.FAILED)),
    st.sampled_from(("trusted", "self-report")),
)
_KIND_OF = {
    "baseline": core.EvidenceKind.BENCHMARK,
    "local-validation": core.EvidenceKind.BENCHMARK,
    "official": core.EvidenceKind.BENCHMARK,
    "profile": core.EvidenceKind.PROFILING,
}
type _Record = tuple[
    str,
    Literal["baseline", "local-validation", "official", "profile"],
    core.ObservationStatus,
    Literal["trusted", "self-report"],
]


@settings(max_examples=examples(pr=30, nightly=300))
@given(records=st.lists(_RECORD, max_size=6))
def test_an_adoption_carries_exactly_the_winners_trusted_measurements(
    records: list[_Record],
) -> None:
    """The executor names what the adopted revision measured, and nothing it did not."""
    measured = tuple(
        accuracy_proof(revision).model_copy(
            update={
                "evidence_id": core.EvidenceId(root=f"measured-{index}"),
                "kind": _KIND_OF[purpose],
                "purpose": purpose,
                "status": status,
                "provenance": provenance,
                "acceptance_receipt": None,
            }
        )
        for index, (revision, purpose, status, provenance) in enumerate(records)
    )
    state = world()
    evaluation = core.EvaluationState(evidence=(*state.evaluation.evidence, *measured))
    run = Run(state.model_copy(update={"evaluation": evaluation}))
    (adopt,) = new_requests(run.propose(GOOD[0]))
    assert isinstance(adopt, core.AdoptRevision)
    assert adopt.evidence == tuple(
        ref
        for ref in evaluation.evidence
        if ref.candidate == GOOD[0].revision
        and ref.purpose in ("official", "baseline", "local-validation")
        and ref.status is core.ObservationStatus.SUCCEEDED
        and ref.provenance == "trusted"
    )
