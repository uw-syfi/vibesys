"""Project one `AgentRunState` into a `RunView`.

Owned by the agent orchestration adapter: it imports
the core-private aggregate types (`vibesys.loops.agent.model.AgentRunState`,
`.Hypothesis`, ...) locked to stay core-private forever (see
`/tmp/vibesys-api-boundary-plan.md`). Everything below is a one-way
projection, mirroring `server.api.experiments`/`server.api.performance`
field-for-field: it copies authoritative facts, never groups rounds, selects
a baseline, or infers a resolution from individual round fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from vibesys.loops.agent.hypotheses import measurement_delta_reason
from vibesys.loops.agent.model import HypothesisResolution
from vibesys.loops.agent.projection import (
    AgentRunProjection,
    HypothesisRoundView,
    HypothesisView,
    RoundView,
)
from vibesys.orchestration.view import RunStatus, RunView
from vibesys.schemas import CandidateDisposition, HypothesisOutcome, derive_hypothesis_title

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.loops.agent.model import AgentRunState, Hypothesis
    from vs_loop_state.api import RoundRecord


def project_run_view(
    state: AgentRunState,
    *,
    run_id: str,
    status: RunStatus,
    experiment_revision: int,
    loop: str,
) -> RunView:
    """Return *state* as the `RunView` a caller sees across the boundary.

    `status` and `experiment_revision` are supplied by the caller rather than
    read off `state` unconditionally: `RunStore.get_run` has no lifecycle
    signal at all (see `RunStatus`), and a live `RunQuery.view()` may report a
    revision newer than the last snapshot this function was handed. `loop` is
    not in `state` either (`AgentRunState` carries no outer-loop field); the
    caller already knows it from the run manifest or its own `RunRequest`.

    `current_round` is `len(state.rounds)`: the count of completed rounds
    across every hypothesis, `state`'s own chronological round count. There is
    no other "current round" concept in core state; anything richer (a live
    in-progress round, a server-journal round label) is journal-derived and
    out of scope for a core-state projection.
    """
    projection = AgentRunProjection(
        current_round=len(state.rounds),
        active_hypothesis_id=state.active_hypothesis_id,
        experiment_revision=experiment_revision,
        hypotheses=[
            _hypothesis_view(hypothesis, active_id=state.active_hypothesis_id)
            for hypothesis in state.hypotheses
        ],
        rounds=[_round_view(record) for record in state.rounds],
    )
    return RunView(
        run_id=run_id,
        loop=loop,
        status=status,
        projection=projection.model_dump(mode="json"),
    )


def project_committed_run_view(state: BaseModel, *, run_id: str, loop: str) -> RunView:
    """Project a just-committed agent state into a `RunView`, entirely in memory.

    For a committed-state listener registered on `LocalRunIntegration`
    (`namespace == "agent"` only): *state* is the exact object the run loop
    just committed via `LoopContext.publish_committed_state`, so this is a
    pure in-memory reshape, never a disk read. `status` is always
    `RunStatus.ACTIVE` (a run only publishes commits while it is the live
    run) and `experiment_revision` is read off *state* itself, the same way
    `RunSession.view()` and the loop's own commit call sites source it.
    """
    agent_state = cast("AgentRunState", state)
    return project_run_view(
        agent_state,
        run_id=run_id,
        status=RunStatus.ACTIVE,
        experiment_revision=agent_state.experiment_revision,
        loop=loop,
    )


def _hypothesis_view(hypothesis: Hypothesis, *, active_id: str | None) -> HypothesisView:
    """Copy one domain hypothesis into its boundary-safe DTO.

    Field-for-field match of `server.api.experiments._to_hypothesis_entry`'s
    source facts, with `plan.title`/`plan.hypothesis`/`plan.task` read and
    `plan` itself never exposed (`OrchestratorPlan` is core-owned but not part
    of this DTO's contract).
    """
    rounds = hypothesis.rounds
    measurement = hypothesis.measurement
    return HypothesisView(
        hypothesis_id=hypothesis.hypothesis_id,
        title=_text(hypothesis.plan.title) or derive_hypothesis_title(hypothesis.plan.hypothesis),
        claim=_text(hypothesis.plan.hypothesis),
        action=_text(hypothesis.plan.task),
        first_round=hypothesis.started_round,
        last_round=rounds[-1].round_number if rounds else hypothesis.started_round,
        rounds=[_hypothesis_round_view(record) for record in rounds],
        resolved_outcome=(
            hypothesis.resolution.value if hypothesis.resolution is not None else None
        ),
        judge_verdict=_judge_verdict(hypothesis.review.value),
        perf_metric=measurement.value if measurement is not None else None,
        perf_unit=_text(measurement.unit) if measurement is not None else None,
        perf_delta_pct=measurement.delta_pct if measurement is not None else None,
        perf_metric_round=measurement.round if measurement is not None else None,
        perf_metric_name=_text(measurement.metric) if measurement is not None else None,
        perf_direction=measurement.direction if measurement is not None else None,
        perf_baseline_value=measurement.baseline_value if measurement is not None else None,
        perf_baseline_round=measurement.baseline_round if measurement is not None else None,
        perf_baseline_commit=(
            _text(measurement.baseline_commit) if measurement is not None else None
        ),
        perf_delta_reason=measurement_delta_reason(hypothesis),
        kept=hypothesis.candidate_retained,
        strategy_disposition=hypothesis.strategy.value,
        strategy_reason=hypothesis.strategy_reason,
        active=hypothesis.hypothesis_id == active_id,
        last_experiment_revision=hypothesis.last_experiment_revision,
        parent_commit=hypothesis.parent_commit,
    )


def _hypothesis_round_view(record: RoundRecord) -> HypothesisRoundView:
    """Copy one round record, matching `server.api.experiments._round`.

    `judge_verdict` is copied verbatim (unlike the hypothesis-level field
    below, which narrows to the two terminal values): a round's own verdict
    legitimately holds `"deferred"` under sparse-review policy, and
    `HypothesisRoundView.judge_verdict` is typed to carry that third value so
    the server's external protocol keeps reporting it unchanged.
    """
    return HypothesisRoundView(
        round_number=record.round_number,
        passed=record.passed,
        reviewed=record.reviewed,
        hypothesis_outcome=_outcome(record.hypothesis_outcome),
        judge_verdict=record.judge_verdict,
        perf_metric=record.perf_metric,
        perf_unit=_text(record.perf_unit),
        perf_delta_pct=record.perf_delta_pct,
        commit=_text(record.commit),
        official_evaluation=record.official_evaluation,
        candidate_disposition=_disposition(record.candidate_disposition),
    )


def _round_view(record: RoundRecord) -> RoundView:
    """Copy one run-wide round, matching `server.api.service.performance_rounds`'s fields."""
    return RoundView(
        round_number=record.round_number,
        commit=_text(record.commit),
        perf_metric=record.perf_metric,
        perf_unit=_text(record.perf_unit),
        passed=record.passed,
        profile_skipped=record.profile_skipped,
        official_evaluation=record.official_evaluation,
    )


def _outcome(value: str | None) -> str | None:
    """Read a stored outcome under either vocabulary that can produce it.

    A round record holds the implementer's declared outcome unless the
    framework resolved the hypothesis, in which case it holds the resolution
    instead. Matches `server.api.experiments._outcome`, save that it returns
    the resolved string rather than the enum member: the two vocabularies
    (`HypothesisOutcome`, `HypothesisResolution`) are both core-private, and a
    single field can hold either, so a boundary DTO can only expose their
    shared `str` value, never a member of either type. Anything else is a
    legacy or retired value with no meaning for a client, so it projects as
    "not recorded" rather than failing the log.
    """
    if not value:
        return None
    for vocabulary in (HypothesisOutcome, HypothesisResolution):
        member = vocabulary.__members__.get(value.upper())
        if member is not None and member.value == value:
            return member.value
    return None


def _disposition(value: str | None) -> str | None:
    """Read a stored disposition, dropping values the framework retired."""
    if not value:
        return None
    member = CandidateDisposition.__members__.get(value.upper())
    return member.value if member is not None and member.value == value else None


def _judge_verdict(value: str | None) -> Literal["pass", "fail"] | None:
    """Filter a raw verdict to the two terminal values a boundary DTO reports."""
    return value if value in ("pass", "fail") else None


def _text(value: str | None) -> str | None:
    return value or None
