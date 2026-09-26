"""Project authoritative hypothesis-search state into typed run views.

The boundary models copy recorded facts without inferring resolutions. Lives
next to :mod:`vibesys.loops.registry`, not inside any one strategy folder,
because every hypothesis-driven strategy's registered projector
(``MultiProjector``, ``SingleProjector``, ``ProfileMultiProjector``,
``ProfileSingleProjector``) shares it; strategies never import each other, so
shared read-model logic cannot live inside one strategy's package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from framework.api import RoundSummary, RunStatus, RunView
from vibesys.loops.state.api import HypothesisResolution
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    PerfDeltaReason,
    derive_hypothesis_title,
)
from vibesys.search.hypothesis import HypothesisSearch

if TYPE_CHECKING:
    from vibesys.loops.state.api import RoundRecord
    from vibesys.search.hypothesis.state import Hypothesis, HypothesisState


class HypothesisRoundView(BaseModel):
    """One hypothesis's round, copied verbatim from its authoritative record.

    A one-way projection, matching `server.api.experiments._round`: it copies
    fields, never groups rounds, selects a baseline, or infers a resolution.
    `hypothesis_outcome`/`candidate_disposition` are pre-resolved to their
    plain string value (rather than exposing the enum members
    `server.api.experiments` resolves them to) because a round's outcome can
    legitimately hold either of two distinct core-private vocabularies
    (`HypothesisOutcome` or `HypothesisResolution`), which a single typed
    field on a boundary DTO cannot express without leaking those types.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_number: int
    passed: bool
    reviewed: bool
    hypothesis_outcome: str | None = None
    judge_verdict: Literal["pass", "fail", "deferred"] | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    perf_delta_pct: float | None = None
    commit: str | None = None
    official_evaluation: bool = False
    candidate_disposition: str | None = None


class HypothesisView(BaseModel):
    """One hypothesis's full history, matching `server.api.experiments.HypothesisEntry`.

    `title`, `resolved_outcome`, `strategy_disposition`, and `perf_delta_reason`
    are precomputed from `vibesys.search.hypothesis.state.Hypothesis`
    (core-private: `plan: OrchestratorPlan`, `strategy: HypothesisStrategy`,
    ...) so this DTO exposes only plain strings and the already-boundary-safe
    `PerfDeltaReason`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str
    title: str | None = None
    claim: str | None = None
    action: str | None = None
    first_round: int
    last_round: int
    rounds: list[HypothesisRoundView] = Field(default_factory=list)
    resolved_outcome: str | None = None
    judge_verdict: Literal["pass", "fail"] | None = None
    # The measurement fields below are copied as one tuple from the
    # hypothesis's single headline measurement: pairing a newer per-round
    # metric with an older causal delta would misreport the measurement.
    perf_metric: float | None = None
    perf_unit: str | None = None
    perf_delta_pct: float | None = None
    # The round that produced the measurement above: `server.api.performance.
    # _latest_measurement` selects the newest headline measurement across
    # hypotheses by this round number, without needing `HypothesisMeasurement`
    # itself.
    perf_metric_round: int | None = None
    perf_metric_name: str | None = None
    perf_direction: Literal["max", "min"] | None = None
    perf_baseline_value: float | None = None
    perf_baseline_round: int | None = None
    perf_baseline_commit: str | None = None
    perf_delta_reason: PerfDeltaReason | None = None
    kept: bool | None = None
    strategy_disposition: str
    strategy_reason: str | None = None
    active: bool
    last_experiment_revision: int
    parent_commit: str | None = None


class RoundView(BaseModel):
    """One run-wide round, in chronological order across all hypotheses.

    Unlike `server.api.service.performance_rounds`, this does not drop rounds
    with no recorded measurement: it is the general-purpose round history,
    not the performance-plot series.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_number: int
    commit: str | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    passed: bool
    profile_skipped: bool = False
    official_evaluation: bool = False
    # The two fields below exist so `RunContext.state.commit` can derive a
    # byte-identical `RoundFinishedData` from this view alone, without any
    # strategy-specific knowledge (see `vibesys.orchestration.runtime`).
    attempts: int = 1
    judge_verdict: Literal["pass", "fail", "skipped"] | None = None


class AgentRunProjection(BaseModel):
    """Agent-owned experiment and round facts carried in a generic run view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent"] = "agent"
    current_round: int
    active_hypothesis_id: str | None = None
    experiment_revision: int
    hypotheses: list[HypothesisView] = Field(default_factory=list)
    rounds: list[RoundView] = Field(default_factory=list)


def agent_projection(view: RunView) -> AgentRunProjection | None:
    """Decode agent-owned facts, or return None for a different policy."""
    payload = view.projection
    if payload is None or payload.get("kind") != "agent":
        return None
    return AgentRunProjection.model_validate(payload)


def project_run_view(
    state: HypothesisState,
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
    not in `state` either (`HypothesisState` carries no outer-loop field); the
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
        rounds=tuple(_round_summary(record) for record in projection.rounds),
        experiment_revision=experiment_revision,
    )


def project_committed_run_view(state: BaseModel, *, run_id: str, loop: str) -> RunView:
    """Project a just-committed agent state into a `RunView`, entirely in memory.

    *state* is the exact typed value published by a completed host checkpoint.
    The registered strategy projector supplies its own namespace and identity;
    this function reshapes the value without a disk read. Active publication
    uses `RunStatus.ACTIVE` and the state's experiment revision.
    """
    agent_state = cast("HypothesisState", state)
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
        perf_delta_reason=HypothesisSearch.delta_reason(hypothesis),
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
        attempts=record.attempts,
        judge_verdict=_round_finished_verdict(record.judge_verdict),
    )


def _round_summary(view: RoundView) -> RoundSummary:
    """Reshape a `RoundView` into the host's strategy-agnostic `RoundSummary`.

    `status` mirrors what the strategies used to emit by hand for
    `RoundFinishedData`: only a "fail" verdict is a failed round.
    """
    return RoundSummary(
        number=view.round_number,
        status="failed" if view.judge_verdict == "fail" else "completed",
        attempts=view.attempts,
        judge_verdict=view.judge_verdict,
        perf_metric=view.perf_metric,
        perf_unit=view.perf_unit,
        profile_skipped=view.profile_skipped,
    )


def _round_finished_verdict(value: str | None) -> Literal["pass", "fail", "skipped"] | None:
    """Map a round's stored verdict to the coarser `RoundFinishedData` vocabulary.

    `"deferred"` (sparse-review policy skipped both the judge and the official
    gates) and a legacy record with no recorded verdict both read as
    `"skipped"`, matching what the strategies used to emit by hand.
    """
    if value in ("pass", "fail"):
        return value
    return "skipped"


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
