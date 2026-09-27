"""Render semantic framework events for the headless terminal."""

from __future__ import annotations

from vibesys.api import (
    CoreEvent,
    EventStatus,
    FrameworkWarningData,
    GateFinishedData,
    GateStartedData,
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
