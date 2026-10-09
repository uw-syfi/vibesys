"""Render semantic framework events for the headless terminal."""

from __future__ import annotations

from datetime import UTC, datetime

from vibesys.api import (
    CoreEvent,
    EventStatus,
    FrameworkWarningData,
    GateFinishedData,
    GateStartedData,
    ProviderSwitchedData,
    QuotaAbandonedData,
    QuotaPausedData,
    QuotaResumedData,
    RateLimitUpdateData,
    RunConfiguredData,
    WorkspaceSnapshotData,
)

_EXCLUDED_PATHS_SHOWN = 5
_SHA_ABBREV = 12


def format_framework_event(event: CoreEvent) -> str | None:
    """Return the terminal rendering of a framework event, or None."""
    data = event.data
    if isinstance(data, GateStartedData):
        return _format_gate_started(data)
    if isinstance(data, GateFinishedData):
        return _format_gate_finished(data, failed=event.status is EventStatus.FAILED)
    if isinstance(data, WorkspaceSnapshotData):
        return _format_workspace_snapshot(data)
    if isinstance(data, RunConfiguredData):
        return _format_run_configured(data)
    if isinstance(data, FrameworkWarningData):
        return _format_framework_warning(data)
    return None


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
    if isinstance(data, QuotaResumedData):
        by = "the wait elapsed" if data.reason == "wait_elapsed" else "resumed"
        return f"[quota] {by}; sending the paused {data.provider} turn again"
    if isinstance(data, ProviderSwitchedData):
        return (
            f"[quota] switched from {data.from_provider} to {data.to_provider} ({data.to_model}) "
            f"by the {data.reason}; new sessions start there ({data.detail})"
        )
    if isinstance(data, QuotaAbandonedData):
        return f"[quota] {data.provider} {_quota_condition(data.condition)}: {data.detail}; {data.reason}"
    if not isinstance(data, QuotaPausedData):
        return None
    line = f"[quota] {data.provider} {_quota_condition(data.condition)}: {data.detail}; the run is paused"
    if data.resets_at is not None:
        line += f" (capacity returns {_utc(data.resets_at)})"
    if data.resumes_at is not None:
        line += f"; resuming by itself at {_utc(data.resumes_at)}"
    if data.fallback_provider is not None:
        line += f"; fallback: {data.fallback_provider} ({data.fallback_model})"
    return line


def _quota_condition(condition: str) -> str:
    return "quota exhausted" if condition == "quota_exhausted" else "rate limited"


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
