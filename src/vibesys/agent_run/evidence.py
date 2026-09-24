"""Pure record, metric, and artifact text transformations for agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.agent_run.hypotheses import trusted_perf_provenance
from vibesys.agent_run.state import HypothesisResolution
from vibesys.evaluators.metrics import Measurement, MetricSpace, Objective
from vibesys.schemas import CandidateDisposition, HypothesisOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_loop_state.api import RoundRecord

_FAILED_HYPOTHESIS_OUTCOMES = (
    frozenset(outcome.value for outcome in HypothesisOutcome)
    - {
        HypothesisOutcome.CONTINUE.value,
        HypothesisOutcome.SUPPORTED.value,
        HypothesisOutcome.NOMINATED.value,
    }
) | {"rejected"}
_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT = 8
_PLATEAU_THRESHOLD_PCT = 5.0
_PLATEAU_MIN_STREAK = 3


def _record_candidate_metrics(record: RoundRecord) -> dict[str, float]:
    """Return the comparable objective row associated with *record*.

    Official metrics remain authoritative when present. Candidate metrics are
    a separate, explicitly provisional channel for representative evaluations
    that are useful for branch retention but must not update the canonical
    headline trajectory.
    """
    if record.official_evaluation and record.metrics:
        return record.metrics
    if _record_candidate_retained(record) is True:
        return record.candidate_metrics
    return {}


def _record_candidate_retained(record: RoundRecord) -> bool | None:
    """Read framework retention, with one isolated legacy-record adapter."""
    if record.candidate_retained is not None:
        return record.candidate_retained
    if record.judge_verdict is not None:
        # New records always carry the framework's typed verdict marker. For
        # them, explicit unknown retention must remain unknown.
        return None
    if record.candidate_disposition in {
        CandidateDisposition.PARETO_FRONTIER.value,
        CandidateDisposition.PREREQUISITE.value,
    }:
        return True
    if record.candidate_disposition == CandidateDisposition.DISCARD.value:
        return False
    if record.hypothesis_outcome == HypothesisResolution.PROVEN.value:
        return True
    return None


def _provisional_candidate_retained(
    disposition: CandidateDisposition,
) -> bool | None:
    """Translate an implementer disposition into provisional branch retention."""
    if disposition is CandidateDisposition.DISCARD:
        return False
    if disposition in {
        CandidateDisposition.PREREQUISITE,
        CandidateDisposition.PARETO_FRONTIER,
    }:
        return True
    return None


def _trusted_candidate_records(records: list[RoundRecord], space: MetricSpace) -> list[RoundRecord]:
    """Return reviewed checkpoints with complete comparable objective rows."""
    trusted: list[RoundRecord] = []
    for record in records:
        if not space.complete(_record_candidate_metrics(record)):
            continue
        if not record.commit or not record.passed or not record.reviewed:
            continue
        if not trusted_perf_provenance(record.perf_provenance):
            # An implementer self-reported headline metric may keep its commit
            # as a provisional claim, but it must never seed the archive as a
            # trusted Pareto parent or dominate later candidates.
            continue
        if _record_candidate_retained(record) is not True:
            continue
        trusted.append(record)
    return trusted


def _pareto_frontier_records(
    records: list[RoundRecord],
    space: MetricSpace,
) -> list[RoundRecord]:
    """Compute the noise-aware frontier over independently reviewed points."""
    return space.frontier(
        _trusted_candidate_records(records, space),
        _record_candidate_metrics,
    )


def _pareto_archive_dominators(
    candidate_metrics: dict[str, float],
    records: list[RoundRecord],
    space: MetricSpace,
) -> list[RoundRecord]:
    """Return trusted archive points that dominate a proposed candidate row."""
    if not space.complete(candidate_metrics):
        return []
    return [
        record
        for record in _trusted_candidate_records(records, space)
        if space.dominates(_record_candidate_metrics(record), candidate_metrics)
    ]


def _format_metric_row(metrics: dict[str, float], objectives: Sequence[Objective]) -> str:
    return ", ".join(
        f"{objective.name}={metrics[objective.name]:.6g} ({objective.direction})"
        for objective in objectives
        if objective.name in metrics
    )


def _pareto_archive_summary(records: list[RoundRecord], space: MetricSpace) -> str:
    """Render trusted frontier parents and any measured points awaiting review."""
    objectives = space.objectives
    latest = max(records, key=lambda record: record.round_number, default=None)
    latest_metrics = (
        _format_metric_row(latest.metrics, objectives)
        if latest is not None
        and latest.official_evaluation
        and trusted_perf_provenance(latest.perf_provenance)
        and objectives
        and space.complete(latest.metrics)
        else (
            f"{latest.perf_metric:.6g} {latest.perf_unit or ''}".strip()
            if latest is not None
            and latest.official_evaluation
            and trusted_perf_provenance(latest.perf_provenance)
            and latest.perf_metric is not None
            else "(none)"
        )
    )
    latest_line = (
        "Latest completed round: none."
        if latest is None
        else (
            f"Latest completed round: round {latest.round_number}, "
            f"commit {(latest.commit or '(missing)')[:12]}, "
            f"official metrics: {latest_metrics}; "
            f"retained: {_record_candidate_retained(latest)}."
        )
    )
    if not objectives:
        return (
            "No objective axes are configured. Use objectives.toml to enable "
            "multi-objective checkpoint retention; official scalar tracking remains active.\n"
            f"{latest_line}"
        )

    lines = [
        "Configured axes: "
        + ", ".join(f"{objective.name}:{objective.direction}" for objective in objectives),
        (
            "Dominance is variance-aware: a point removes another only when it is no worse "
            f"within {space.relative_noise:.0%} on every axis and better by more than "
            f"{space.relative_noise:.0%} on at least one."
        ),
        latest_line,
    ]
    frontier = _pareto_frontier_records(records, space)
    if frontier:
        lines.append("Trusted frontier parents:")
        for record in frontier:
            assert record.commit is not None  # noqa: S101  # tracked: #288
            evidence = "official" if record.official_evaluation else "reviewed provisional"
            operating_point = record.candidate_operating_point or "canonical workload row"
            artifact = record.candidate_evaluation_artifact or record.evaluation_artifact
            lines.append(
                f"- round {record.round_number}, commit {record.commit[:12]}, {evidence}: "
                f"{_format_metric_row(_record_candidate_metrics(record), objectives)}; "
                f"operating point: {operating_point}; artifact: {artifact or '(missing)'}"
            )
    else:
        lines.append("Trusted frontier parents: none recorded yet.")

    trusted_rounds = {record.round_number for record in _trusted_candidate_records(records, space)}
    pending = [
        record
        for record in records
        if record.round_number not in trusted_rounds
        and record.commit
        and _record_candidate_retained(record) is True
        and all(objective.name in record.candidate_metrics for objective in objectives)
    ]
    if pending:
        pending.sort(key=lambda record: record.round_number)
        lines.append(
            "Measured frontier claims not yet usable as trusted parents (retain the commit, "
            "but do not treat it as a parent). A row lands here because its hard invariants "
            "have not passed independent review, or because its numbers are the "
            "implementer's own report rather than a framework measurement:"
        )
        omitted = pending[:-_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT]
        if omitted:
            # This line is read by a model, so it agrees with itself: one
            # omitted claim says "1 older untrusted claim", and a single
            # omitted round says "round 4" rather than the degenerate
            # "rounds 4-4".
            claims = "claim" if len(omitted) == 1 else "claims"
            first = omitted[0].round_number
            last = omitted[-1].round_number
            rounds = f"round {first}" if first == last else f"rounds {first}-{last}"
            lines.append(
                f"- {len(omitted)} older untrusted {claims} omitted from this context "
                f"({rounds}); do not treat any omitted claim as a trusted parent."
            )
        for record in pending[-_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT:]:
            assert record.commit is not None  # noqa: S101  # tracked: #288
            lines.append(
                f"- round {record.round_number}, commit {record.commit[:12]}: "
                f"{_format_metric_row(record.candidate_metrics, objectives)}; "
                f"operating point: {record.candidate_operating_point or '(unspecified)'}; "
                f"artifact: {record.candidate_evaluation_artifact or '(missing)'}; "
                f"reason: {record.candidate_retention_reason or '(unspecified)'}"
            )
    return "\n".join(lines)


def _headline_measurement(record: RoundRecord) -> Measurement | None:
    """Return a record's scalar headline as a typed measurement."""
    if record.perf_metric is None or record.perf_unit is None:
        return None
    return Measurement(
        metric=record.perf_unit,
        value=record.perf_metric,
        direction=record.perf_direction,
    )


def _trusted_final_records(records: list[RoundRecord], space: MetricSpace) -> list[RoundRecord]:
    """Return retained records with canonical framework-owned measurements."""
    return [
        record
        for record in records
        if record.commit
        and record.passed
        and record.reviewed
        and record.official_evaluation
        and trusted_perf_provenance(record.perf_provenance)
        and _record_candidate_retained(record) is True
        and (
            space.complete(record.metrics)
            if space.objectives
            else space.direction(_headline_measurement(record)) is not None
        )
    ]


def _select_final_candidate(records: list[RoundRecord], space: MetricSpace) -> RoundRecord | None:
    """Select the latest noise-aware winner from trusted retained records."""
    newest_first = sorted(
        _trusted_final_records(records, space),
        key=lambda record: record.round_number,
        reverse=True,
    )
    if space.primary is not None:
        frontier_rounds = {
            record.round_number for record in _pareto_frontier_records(newest_first, space)
        }
        candidates = [record for record in newest_first if record.round_number in frontier_rounds]
        primary = space.primary

        def primary_measurement(record: RoundRecord) -> Measurement:
            return Measurement(
                metric=primary.name,
                value=record.metrics[primary.name],
                direction=primary.direction,
            )

        return space.best(candidates, primary_measurement)
    return space.best(newest_first, _headline_measurement)


def _pareto_archive_conflict(
    *,
    candidate_disposition: CandidateDisposition,
    candidate_metrics: dict[str, float],
    records: list[RoundRecord],
    space: MetricSpace,
) -> str | None:
    """Explain why a claimed frontier row is dominated by the live archive."""
    if candidate_disposition is not CandidateDisposition.PARETO_FRONTIER:
        return None
    dominators = _pareto_archive_dominators(candidate_metrics, records, space)
    if not dominators:
        return None
    rows = "; ".join(
        f"round {record.round_number} ({_format_metric_row(_record_candidate_metrics(record), space.objectives)})"
        for record in dominators
    )
    return (
        "The candidate's `pareto_frontier` disposition conflicts with the live "
        f"noise-aware archive: it is dominated by {rows}. A numeric archive gate "
        "frozen into the hypothesis plan does not override the current archive. "
        "Report this row as `discard` unless its metrics or configured objective "
        "comparability were recorded incorrectly; do not rerun an unchanged "
        "candidate merely to repair the disposition."
    )


def _detect_plateau(
    records: list[RoundRecord],
    *,
    threshold_pct: float = _PLATEAU_THRESHOLD_PCT,
    min_streak: int = _PLATEAU_MIN_STREAK,
) -> str | None:
    """Return a warning string if the most recent ``min_streak`` rounds
    with **fresh, same-unit** perf metrics stayed within ``threshold_pct``
    of each other; else None.

    Rules:
    - ``profile_skipped`` rounds don't count as fresh measurements (their
      perf was reused from earlier).
    - Only rounds the framework measured itself count. An implementer's
      self-reported number is not evidence that the search has stopped
      making progress, and telling the orchestrator it has plateaued on
      the strength of its own reports is a feedback loop.
    - Only rounds with the *same* ``perf_unit`` as the latest fresh round
      count toward the streak — comparing latency_ms against tok/s as raw
      floats is a category error.
    - Failed rounds (``passed=False`` or no perf_metric) are stepped over.

    The orchestrator gets this verbatim in its prompt; phrasing is
    user-facing.
    """  # noqa: D205  # tracked: #288
    fresh = [
        r
        for r in records
        if r.passed
        and r.official_evaluation
        and r.perf_metric is not None
        and trusted_perf_provenance(r.perf_provenance)
        and not r.profile_skipped
    ]
    if len(fresh) < min_streak:
        return None
    latest_unit = fresh[-1].perf_unit
    same_unit = [r for r in fresh if r.perf_unit == latest_unit]
    if len(same_unit) < min_streak:
        return None
    tail = same_unit[-min_streak:]
    perfs = [r.perf_metric for r in tail if r.perf_metric is not None]
    hi = max(perfs)
    lo = min(perfs)
    if hi <= 0:
        return None
    spread_pct = (hi - lo) / hi * 100
    if spread_pct >= threshold_pct:
        return None
    unit_suffix = f" {latest_unit}" if latest_unit else ""
    rounds = [r.round_number for r in tail]
    return (
        f"The last {min_streak} rounds with a fresh perf measurement (rounds "
        f"{rounds[0]}–{rounds[-1]}) all landed in {lo:.2f}–{hi:.2f}{unit_suffix} "  # noqa: RUF001  # tracked: #288
        f"— a {spread_pct:.2f}% spread, well within bench noise. Whatever you've "
        f"been working on for those rounds is not actually moving the headline "
        f"metric."
    )


@dataclass
class CarryOver:
    """Record-derived guidance passed to the next planning turn."""

    regression_info: str | None = None
    exhaustion_info: str | None = None


def _provisional_candidates_since_official(records: list[RoundRecord]) -> int:
    """Count accepted candidate checkpoints after the latest official one."""
    count = 0
    for record in reversed(records):
        if record.official_evaluation:
            break
        if (
            record.passed
            and record.reviewed
            and (
                _record_candidate_retained(record) is True
                # An accepted-but-unmeasured hypothesis consumes cadence budget
                # like a proven one: it is exactly the checkpoint the next
                # official evaluation must measure.
                or record.hypothesis_outcome
                in {
                    HypothesisResolution.PROVEN.value,
                    HypothesisResolution.UNMEASURED.value,
                }
            )
        ):
            count += 1
    return count


def _terminal_workspace_notice(records: list[RoundRecord]) -> str | None:
    """Describe a terminal hypothesis whose edits remain in the workspace."""
    if not records:
        return None
    latest = records[-1]
    terminal_outcomes = {
        HypothesisOutcome.DISPROVEN.value,
        HypothesisOutcome.IMPLEMENTATION_FAILED.value,
        HypothesisOutcome.INCONCLUSIVE.value,
        HypothesisOutcome.BLOCKED.value,
    }
    if latest.hypothesis_outcome not in terminal_outcomes:
        return None

    if _record_candidate_retained(latest) is True:
        review_status = (
            "independently reviewed"
            if latest.passed and latest.reviewed
            else "awaiting independent review"
        )
        return (
            f"Hypothesis `{latest.hypothesis_id or 'unspecified'}` ended as "
            f"`{latest.hypothesis_outcome}` in round {latest.round_number}, but its "
            f"implementation reported a {review_status} Pareto checkpoint: "
            f"{latest.candidate_metrics or '(metrics missing)'}. Preserve commit "
            f"`{(latest.commit or '(missing)')[:12]}` as a distinct branch candidate. "
            "The causal forecast and checkpoint retention decision are separate: do "
            "not erase a credible throughput/latency tradeoff merely because another "
            "axis or the forecast missed. If review is pending, validate hard "
            "correctness and workload invariants before using it as a trusted parent. "
            "Choose this checkpoint only when the next hypothesis names which frontier "
            "gap it will improve; otherwise explicitly restore another frontier parent."
        )

    campaign_records = [latest]
    for record in reversed(records[:-1]):
        if record.hypothesis_id != latest.hypothesis_id:
            break
        campaign_records.append(record)
    campaign_records.reverse()
    started_round = campaign_records[0].round_number
    parent_round = next(
        (
            record.hypothesis_parent_round
            for record in campaign_records
            if record.hypothesis_parent_round is not None
        ),
        started_round - 1 if started_round > 1 else None,
    )
    parent_guidance = (
        f"The recorded pre-hypothesis parent is round {parent_round}; use "
        f"`revert_to_round={parent_round}` if that parent should be restored."
        if parent_round is not None
        else "No earlier recorded round exists, so identify the clean parent state explicitly."
    )
    latest_checkpoint = next(
        (
            record
            for record in reversed(records[:-1])
            if record.commit is not None
            and record.hypothesis_outcome in {HypothesisOutcome.CONTINUE.value, "proven"}
        ),
        None,
    )
    checkpoint_guidance = ""
    if latest_checkpoint is not None and latest_checkpoint.round_number != parent_round:
        review_label = "reviewed" if latest_checkpoint.reviewed else "provisional"
        checkpoint_guidance = (
            " The most recent earlier nonterminal checkpoint is round "
            f"{latest_checkpoint.round_number} "
            f"(`{latest_checkpoint.hypothesis_outcome}`, {review_label}). If the "
            "terminal evidence rejects only the newest child experiment, preserve "
            "that checkpoint instead of discarding prior gains; restore the original "
            "pre-hypothesis parent only when the evidence invalidates the full chain. "
            f"If metrics from round {latest_checkpoint.round_number} are the "
            "restoration gate, restore that checkpoint or preserve all production "
            "changes through it. An older implementation cannot be required to "
            "reproduce a later checkpoint's metric while those later gains are omitted."
        )
    return (
        f"Hypothesis `{latest.hypothesis_id or 'unspecified'}` ended as "
        f"`{latest.hypothesis_outcome}` in round {latest.round_number}, but its "
        "workspace edits are still present. Before building a new hypothesis, "
        "decide explicitly whether to roll those edits back or retain a reusable "
        "correctness/measurement prerequisite. Do not silently build on a "
        f"falsified performance mechanism. {parent_guidance}{checkpoint_guidance} "
        "If retaining any "
        "part, justify it and re-establish the end-to-end parent behavior."
    )
