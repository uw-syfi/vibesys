"""Render semantic framework events for the headless terminal."""

from __future__ import annotations

from datetime import UTC, datetime

from vibesys.api import (
    ComparisonsReadData,
    ComparisonStatus,
    CoreEvent,
    EventStatus,
    FrameworkWarningData,
    GateFinishedData,
    GateStartedData,
    InputMeasurementFailedData,
    MeasurementComparedData,
    ProviderSwitchedData,
    QuotaAbandonedData,
    QuotaPausedData,
    QuotaResumedData,
    RateLimitUpdateData,
    RunConfiguredData,
    WinnerSelectedData,
    WorkspaceSnapshotData,
    describe_seconds,
)

_EXCLUDED_PATHS_SHOWN = 5
_SHA_ABBREV = 12


def format_framework_event(event: CoreEvent) -> str | None:
    """Return the terminal rendering of a framework event, or None."""
    data = event.data
    text: str | None = None
    match data:
        case GateStartedData():
            text = _format_gate_started(data)
        case GateFinishedData():
            text = _format_gate_finished(data, failed=event.status is EventStatus.FAILED)
        case WorkspaceSnapshotData():
            text = _format_workspace_snapshot(data)
        case RunConfiguredData():
            text = _format_run_configured(data)
        case FrameworkWarningData():
            text = _format_framework_warning(data)
        case WinnerSelectedData():
            text = _format_winner_selected(data)
        case InputMeasurementFailedData():
            text = _format_input_measurement_failed(data)
        case MeasurementComparedData():
            text = _format_measurement_compared(data)
        case ComparisonsReadData():
            text = _format_comparisons_read(data)
    return text


def format_rate_limit_event(event: CoreEvent) -> str | None:
    """Return the terminal rendering of a rate-limit report, or None.

    A window shows only once it is exhausted; routine reports stay in the run log.
    """
    data = event.data
    if not isinstance(data, RateLimitUpdateData) or not data.exhausted:
        return None
    name = " ".join(part for part in (data.provider, data.limit, data.window) if part)
    line = f"[rate-limit] {name or 'a provider'} window is exhausted"
    if data.resets_at is not None:
        reset = datetime.fromtimestamp(data.resets_at, tz=UTC)
        line += f"; resets {reset:%Y-%m-%d %H:%M} UTC"
    return line


def format_quota_event(event: CoreEvent) -> str | None:
    """Return the terminal rendering of a quota pause, resume or abandonment, or None."""
    data = event.data
    match data:
        case QuotaResumedData():
            return _format_quota_resumed(data)
        case ProviderSwitchedData():
            return (
                f"[quota] switched from {data.from_provider} to {data.to_provider} "
                f"({data.to_model}) by the {data.reason}; new sessions start there ({data.detail})"
            )
        case QuotaAbandonedData():
            return (
                f"[quota] {data.provider} {_quota_condition(data.condition)}: "
                f"{data.detail}; {data.reason}"
            )
        case QuotaPausedData():
            return _format_quota_paused(data)
        case _:
            return None


def _format_quota_resumed(data: QuotaResumedData) -> str:
    waited = describe_seconds(data.waited_seconds)
    if data.condition == "unavailable":
        return f"[quota] {data.provider} turn waited {waited} out of an outage; sending it again"
    by = "the wait elapsed" if data.reason == "wait_elapsed" else "resumed"
    after = f" after {waited}" if data.waited_seconds > 0 else ""
    return f"[quota] {by}{after}; sending the paused {data.provider} turn again"


def _format_quota_paused(data: QuotaPausedData) -> str:
    what = f"[quota] {data.provider} {_quota_condition(data.condition)}: {data.detail}"
    if data.condition == "unavailable":
        line = f"{what}; this turn waits, the run goes on"
        if data.resumes_at is not None:
            line += f"; trying again at {_utc(data.resumes_at)}"
        return line
    line = f"{what}; the run is paused"
    if data.resets_at is not None:
        line += f" (capacity returns {_utc(data.resets_at)})"
    if data.resumes_at is not None:
        line += f"; resuming by itself at {_utc(data.resumes_at)}"
    if data.fallback_provider is not None:
        line += f"; fallback: {data.fallback_provider} ({data.fallback_model})"
    return line


_QUOTA_CONDITIONS = {
    "quota_exhausted": "quota exhausted",
    "rate_limited": "rate limited",
    "unavailable": "unavailable",
}


def _quota_condition(condition: str) -> str:
    return _QUOTA_CONDITIONS[condition]


def outage_wait_seconds(event: CoreEvent) -> float:
    """Seconds a turn waited out a provider outage in this event; zero for any other event.

    A wait counts whether the turn was then sent again (``quota_resumed``) or the wait
    ended it (``quota_abandoned``: cancelled, or the run's deadline came).
    """
    data = event.data
    if isinstance(data, QuotaResumedData | QuotaAbandonedData) and data.condition == "unavailable":
        return data.waited_seconds
    return 0.0


def format_outage_summary(seconds: float) -> str | None:
    """The run-end line for the time turns spent waiting out provider outages, or None."""
    if seconds <= 0:
        return None
    return f"[quota] turns waited out provider outages for {describe_seconds(seconds)} in total"


def _utc(epoch: float) -> str:
    return f"{datetime.fromtimestamp(epoch, tz=UTC):%Y-%m-%d %H:%M} UTC"


def _format_gate_started(data: GateStartedData) -> str:
    tag = f"[framework-{data.gate.value}]"
    if data.recipe is not None and data.command is not None:
        return f"{tag} running {data.recipe}: {data.command}"
    if data.command is not None:
        return f"{tag} running: {data.command}"
    return f"{tag} running"


def _format_gate_finished(data: GateFinishedData, *, failed: bool) -> str:
    tag = f"[framework-{data.gate.value}]"
    if failed:
        parts = [part for part in (data.recipe, data.output_tail) if part]
        return f"{tag} FAIL: {': '.join(parts)}" if parts else f"{tag} FAIL"
    verdict = "reused PASS" if data.reused else "PASS"
    if data.metric is not None and data.value is not None:
        return f"{tag} {verdict}: {data.metric}={data.value}"
    if data.recipe is not None:
        return f"{tag} {verdict}: {data.recipe}"
    return f"{tag} {verdict}"


def _format_workspace_snapshot(data: WorkspaceSnapshotData) -> str:
    if data.baseline is not None:
        return f"[git-tracking] trusted input baseline: {data.baseline[:_SHA_ABBREV]}"
    if data.excluded_paths:
        shown = ", ".join(data.excluded_paths[:_EXCLUDED_PATHS_SHOWN])
        if len(data.excluded_paths) > _EXCLUDED_PATHS_SHOWN:
            shown += "…"
        return (
            f"[git-tracking] excluded {len(data.excluded_paths)} "
            f"unreadable path(s) from snapshot: {shown}"
        )
    if data.commit is not None:
        return f"[git-tracking] snapshot '{data.label}': {data.commit[:_SHA_ABBREV]}"
    return f"[git-tracking] no changes to commit for '{data.label}'"


def _format_run_configured(data: RunConfiguredData) -> str:
    lines = [
        f"[log] run log: {data.run_log_path}",
        f"[log] project root: {data.project_root}",
    ]
    if data.model is not None:
        lines.append(f"[log] model: {data.model}")
    if data.objective is not None:
        lines.append(f"[log] objective: {data.objective}")
    if data.search_policy is not None:
        lines.append(f"[log] search policy: {data.search_policy}")
    if data.pareto_objectives is not None:
        lines.append(f"[log] pareto objectives: {data.pareto_objectives}")
    if data.benchmark_contract:
        lines.append("[log] benchmark result contract declared; it owns candidate fitness")
    return "\n".join(lines)


def _format_framework_warning(data: FrameworkWarningData) -> str:
    if data.detail:
        return f"[warn] {data.summary}: {data.detail}"
    return f"[warn] {data.summary}"


def _format_measurement_compared(data: MeasurementComparedData) -> str:
    unit = f" {data.unit}" if data.unit else ""
    return (
        f"[compare] {data.candidate_commit[:_SHA_ABBREV]} vs base "
        f"{data.base_commit[:_SHA_ABBREV]}: {data.verdict.value}, "
        f"{data.candidate_value:g}{unit} vs {data.base_value:g}{unit}, "
        f"ratio {data.ratio:.3f} \u00b1 {data.noise:.3f} "
        f"({data.candidate_samples} and {data.base_samples} samples)"
    )


def _format_comparisons_read(data: ComparisonsReadData) -> str:
    parts = []
    for row in data.comparisons:
        if row.status is ComparisonStatus.MEASURED and row.value is not None:
            unit = f" {row.unit}" if row.unit else ""
            parts.append(f"{row.label} {row.value:.4g}{unit}")
        else:
            parts.append(f"{row.label} {row.status.value.replace('_', ' ')}")
    return "[baselines] " + ", ".join(parts)


def _format_input_measurement_failed(data: InputMeasurementFailedData) -> str:
    where = f", stage {data.stage}" if data.stage else ""
    line = (
        f"[input] the input's trusted measurement failed ({data.failure.value}{where}); "
        "the run goes on with no input baseline, and the planner is told why"
    )
    return f"{line}:\n{data.summary}" if data.summary else line


def _format_winner_selected(data: WinnerSelectedData) -> str:
    revision = data.revision[:_SHA_ABBREV]
    if data.selection == "baseline":
        return f"[result] selected the trusted input unchanged: no candidate beat it ({revision})"
    return f"[result] selected trusted winner {revision} (settlement {data.settlement_id})"
