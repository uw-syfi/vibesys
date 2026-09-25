"""Candidate archive selection and rendering for the agent loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent.hypotheses import trusted_perf_provenance
from vibesys.loops.metrics import Measurement, MetricSpace, Objective
from vibesys.schemas import CandidateDisposition
from vs_loop_state.api import HypothesisResolution

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_loop_state.api import RoundRecord

_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT = 8


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
            if record.commit is None:
                continue
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
            if record.commit is None:
                continue
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
