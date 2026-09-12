"""Render typed framework events into stable run-log lines.

The bracket-tagged line format lives here, in one renderer, instead of in the
producers: gates, loops, and the Git tracker publish typed events, and this
module decides what those look like in ``run-*.log`` and on a headless
terminal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.run.events import (
    CoreEvent,
    EventStatus,
    FrameworkWarningData,
    GateFinishedData,
    GateStartedData,
    RunConfiguredData,
    WorkspaceSnapshotData,
)

if TYPE_CHECKING:
    from typing import TextIO

_EXCLUDED_PATHS_SHOWN = 5
_SHA_ABBREV = 12


def format_framework_event(event: CoreEvent) -> str | None:
    """Return the log rendering of a framework event, or None for others.

    The result may span multiple lines (``run_configured``) and never ends
    with a newline.
    """
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


class RunLogRenderer:
    """Write formatted framework events to the current run log file.

    Subscribed to the output sink beside the durable journal, holding the
    logger's switch-following writer, so converted producers stop calling
    ``lprint`` without the run log losing its lines.
    """

    def __init__(self, writer: TextIO) -> None:
        """Wrap the run logger's current-file writer."""
        self._writer = writer

    def handle(self, event: CoreEvent) -> None:
        """Append the log rendering of a framework event, if it has one."""
        line = format_framework_event(event)
        if line is None or getattr(self._writer, "closed", False):
            return
        self._writer.write(line + "\n")
        self._writer.flush()
