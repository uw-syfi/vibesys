"""Typed read model for the built-in agent orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.schemas import PerfDeltaReason

if TYPE_CHECKING:
    from vibesys.api.contracts import RunView


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
    are precomputed from `vibesys.loops.agent.model.Hypothesis` (core-private:
    `plan: OrchestratorPlan`, `strategy: HypothesisStrategy`, ...) so this DTO
    exposes only plain strings and the already-boundary-safe `PerfDeltaReason`.
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
