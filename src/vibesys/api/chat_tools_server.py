"""Stdio MCP server exposing one run's read-model to experiment chat.

Replaces the old file-materialization evidence path
(`server.chat.evidence.TrajectoryEvidence`, deleted): instead of copying a
run's portable state and raw logs into the chat agent's sandbox for `rg`/
`tail`/`jq`, this subprocess opens the run's own read-model
(`vibesys.api.open_run_store`) and serves it as a handful of read-only MCP
tools. A chat agent driver launches `python -m vibesys.api.chat_tools_server
--run-id <id> --project-root <path>` per `vibesys.api.session
._OpenedAgentEnvironment.investigation_tools`; this module rebuilds its own
tool list from those two primitives, mirroring
`vs_issue_board.mcp`'s standalone-server shape.

Tool set:

- `run_summary`: this run's top-level facts (loop, status, current round,
  active hypothesis, experiment revision).
- `list_hypotheses`: one line per hypothesis (id, title, round range,
  resolved outcome) -- the structured replacement for grepping
  `trajectory/state/agent/hypotheses.json`-shaped files by hand.
- `get_hypothesis`: one hypothesis's full record, including its per-round
  history.
- `list_rounds`: every completed round, run-wide, in chronological order.
- `list_state_files` / `read_state_file`: raw access to this run's portable
  state documents (`vs_project.ProjectState.portable_run_export`), for
  detail the DTOs above do not carry. This is the direct equivalent of what
  `TrajectoryEvidence` used to copy into `trajectory/state/`; the raw
  per-attempt/event logs it also copied from `log_dir` are not reproduced
  here because `log_dir` lives outside the project root
  (`vs_project.ProjectState.log_directory`, a machine-local path), which is
  the only thing this subprocess's `--project-root` argument can resolve
  into.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from framework.api import Project, ToolSpec, serve_stdio
from vibesys.api.store import open_run_store
from vibesys.loops.hypothesis_readmodel import agent_projection

if TYPE_CHECKING:
    from vibesys.api.store import RunStore


class _NoArgs(BaseModel):
    """Argument schema for tools that take no input."""


class _HypothesisArgs(BaseModel):
    """Argument schema for `get_hypothesis`."""

    hypothesis_id: str = Field(description="Hypothesis id, as returned by list_hypotheses.")


class _StateFileArgs(BaseModel):
    """Argument schema for `read_state_file`."""

    relative_path: str = Field(
        description="Portable state file path, as returned by list_state_files."
    )


def _run_summary_tool(store: RunStore, run_id: str) -> ToolSpec[_NoArgs]:
    def handler(_args: _NoArgs) -> str:
        view = store.get_run(run_id)
        summary = f"run_id: {view.run_id}\nloop: {view.loop}\nstatus: {view.status.value}"
        projection = agent_projection(view)
        if projection is None:
            return summary
        return (
            f"{summary}\n"
            f"current_round: {projection.current_round}\n"
            f"active_hypothesis_id: {projection.active_hypothesis_id or '(none)'}\n"
            f"experiment_revision: {projection.experiment_revision}\n"
            f"hypothesis_count: {len(projection.hypotheses)}\n"
            f"round_count: {len(projection.rounds)}"
        )

    return ToolSpec(
        name="run_summary",
        description=(
            "Return this run's top-level status: outer loop, current round, active "
            "hypothesis, and experiment revision."
        ),
        input_schema=_NoArgs,
        handler=handler,
    )


def _list_hypotheses_tool(store: RunStore, run_id: str) -> ToolSpec[_NoArgs]:
    def handler(_args: _NoArgs) -> str:
        projection = agent_projection(store.get_run(run_id))
        if projection is None or not projection.hypotheses:
            return "(no hypotheses)"
        lines = []
        for hypothesis in projection.hypotheses:
            marker = (
                " [active]" if hypothesis.hypothesis_id == projection.active_hypothesis_id else ""
            )
            lines.append(
                f"{hypothesis.hypothesis_id}{marker}: {hypothesis.title or '(untitled)'} "
                f"(rounds {hypothesis.first_round}-{hypothesis.last_round}, "
                f"outcome={hypothesis.resolved_outcome or 'pending'})"
            )
        return "\n".join(lines)

    return ToolSpec(
        name="list_hypotheses",
        description="List every hypothesis tried in this run, with its round range and outcome.",
        input_schema=_NoArgs,
        handler=handler,
    )


def _get_hypothesis_tool(store: RunStore, run_id: str) -> ToolSpec[_HypothesisArgs]:
    def handler(args: _HypothesisArgs) -> str:
        projection = agent_projection(store.get_run(run_id))
        if projection is None:
            return f"(no hypothesis {args.hypothesis_id!r}; see list_hypotheses)"
        hypothesis = next(
            (h for h in projection.hypotheses if h.hypothesis_id == args.hypothesis_id), None
        )
        if hypothesis is None:
            return f"(no hypothesis {args.hypothesis_id!r}; see list_hypotheses)"
        lines = [
            f"id: {hypothesis.hypothesis_id}",
            f"title: {hypothesis.title or '(none)'}",
            f"claim: {hypothesis.claim or '(none)'}",
            f"action: {hypothesis.action or '(none)'}",
            f"rounds: {hypothesis.first_round}-{hypothesis.last_round}",
            f"resolved_outcome: {hypothesis.resolved_outcome or '(pending)'}",
            f"judge_verdict: {hypothesis.judge_verdict or '(none)'}",
            f"strategy_disposition: {hypothesis.strategy_disposition}",
            f"active: {hypothesis.active}",
            f"kept: {hypothesis.kept}",
        ]
        if hypothesis.perf_metric is not None:
            lines.append(
                f"perf_metric: {hypothesis.perf_metric} {hypothesis.perf_unit or ''} "
                f"(round {hypothesis.perf_metric_round}, delta {hypothesis.perf_delta_pct})"
            )
        lines.append("rounds:")
        lines.extend(
            f"  round {round_view.round_number}: passed={round_view.passed} "
            f"reviewed={round_view.reviewed} outcome={round_view.hypothesis_outcome or '-'} "
            f"judge={round_view.judge_verdict or '-'} commit={round_view.commit or '-'} "
            f"perf={round_view.perf_metric} {round_view.perf_unit or ''}"
            for round_view in hypothesis.rounds
        )
        return "\n".join(lines)

    return ToolSpec(
        name="get_hypothesis",
        description=(
            "Return one hypothesis's full record: claim, action, disposition, and "
            "per-round history."
        ),
        input_schema=_HypothesisArgs,
        handler=handler,
    )


def _list_rounds_tool(store: RunStore, run_id: str) -> ToolSpec[_NoArgs]:
    def handler(_args: _NoArgs) -> str:
        projection = agent_projection(store.get_run(run_id))
        if projection is None or not projection.rounds:
            return "(no rounds)"
        lines = [
            f"round {round_view.round_number}: passed={round_view.passed} "
            f"commit={round_view.commit or '-'} perf={round_view.perf_metric} "
            f"{round_view.perf_unit or ''} official={round_view.official_evaluation} "
            f"profile_skipped={round_view.profile_skipped}"
            for round_view in projection.rounds
        ]
        return "\n".join(lines)

    return ToolSpec(
        name="list_rounds",
        description="List every completed round in this run, run-wide and in order.",
        input_schema=_NoArgs,
        handler=handler,
    )


def _list_state_files_tool(project: Project, run_id: str) -> ToolSpec[_NoArgs]:
    def handler(_args: _NoArgs) -> str:
        snapshot = project.state.portable_run_export(run_id)
        if not snapshot.files:
            return "(no state files)"
        return "\n".join(str(state_file.relative_path) for state_file in snapshot.files)

    return ToolSpec(
        name="list_state_files",
        description=(
            "List this run's raw portable state file paths (round records, run manifest, "
            "and similar), for use with read_state_file."
        ),
        input_schema=_NoArgs,
        handler=handler,
    )


def _read_state_file_tool(project: Project, run_id: str) -> ToolSpec[_StateFileArgs]:
    def handler(args: _StateFileArgs) -> str:
        snapshot = project.state.portable_run_export(run_id)
        for state_file in snapshot.files:
            if str(state_file.relative_path) != args.relative_path:
                continue
            try:
                return state_file.contents.decode("utf-8")
            except UnicodeDecodeError:
                return f"(binary file, {len(state_file.contents)} bytes)"
        return f"(no state file {args.relative_path!r}; see list_state_files)"

    return ToolSpec(
        name="read_state_file",
        description="Read one raw portable state file's contents by path from list_state_files.",
        input_schema=_StateFileArgs,
        handler=handler,
    )


def build_tools(store: RunStore, project: Project, run_id: str) -> tuple[ToolSpec[Any], ...]:
    """Build every read-only tool this server exposes for one run."""
    # Touch the run once up front so a missing run_id fails fast and
    # identically across every tool, instead of six different "get_run" calls
    # discovering it lazily.
    store.get_run(run_id)
    return (
        _run_summary_tool(store, run_id),
        _list_hypotheses_tool(store, run_id),
        _get_hypothesis_tool(store, run_id),
        _list_rounds_tool(store, run_id),
        _list_state_files_tool(project, run_id),
        _read_state_file_tool(project, run_id),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser this subprocess's own CLI is invoked with."""
    parser = argparse.ArgumentParser(
        prog="vibesys-run-mcp",
        description=(
            "Stdio MCP server exposing one run's read-model as read-only "
            "investigation tools for experiment chat."
        ),
    )
    parser.add_argument("--run-id", required=True, help="Run to expose.")
    parser.add_argument(
        "--project-root", required=True, help="Root of the project containing the run."
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse args, open this run's read-model, and serve its tools over stdio."""
    args = build_parser().parse_args(argv)
    project = Project.open(args.project_root)
    store = open_run_store(project)
    tools = build_tools(store, project, args.run_id)
    serve_stdio(tools, server_name="vibesys-run")


if __name__ == "__main__":
    main()
