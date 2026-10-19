"""The tools that expose one run's read-model to experiment chat.

Instead of copying a run's portable state and raw logs into the chat agent's sandbox
for `rg`/`tail`/`jq`, these tools read the run's own read-model (a `RunStore`) and offer it as
a handful of read-only tools, in the vocabulary the desktop app shows: an *idea* (what the
code calls a hypothesis) is one thing the run tried, a *step* (a round) is one pass of work on
it. `launch.agents` serves them on the host through `vs_evaluation`'s tool relay, so the run's
state stays on the host side of the trust boundary and the agent's container, whatever
interpreter it has, speaks only MCP to the relay.

Tool set:

- `run_summary`: this run's top-level facts (loop, status, current step, active idea,
  experiment revision).
- `list_ideas`: one line per idea (id, title, step range, resolved outcome).
- `get_idea`: one idea's full record, including its per-step history.
- `list_steps`: every completed step, run-wide, in chronological order.
- `list_targets`: the roles, attempts and items a steer may name in this run, from the run's
  own committed state (the ids `propose_steer` accepts).
- `list_state_files` / `read_state_file`: raw access to this run's portable state
  documents (`RunRecord.portable_documents`, read whichever branch is checked out), for
  detail the tools above do not carry. The raw per-attempt/event logs live in `log_dir`,
  a machine-local path outside the project root, which is the only thing this
  relay can resolve into.
- `propose_steer`: the agent's only steering tool. It checks a suggestion (its target must be
  one `list_targets` shows) and returns it as
  a `SteerProposalReceipt`, which the run server publishes as a `steer_proposed` event for
  the operator to edit, send or dismiss. It has no way to send: sending is the operator's
  `command.control`, so the agent cannot send guidance because no tool here does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from vibesys.api import (
    STEER_PROPOSAL_KIND,
    SteerMode,
    SteerProposal,
    SteerProposalReceipt,
    SteerTarget,
)
from vibesys.api.hypothesis import agent_projection
from vs_mcp.api import ToolSpec

if TYPE_CHECKING:
    from vibesys.api import RunStore, SteerTargets

PROPOSE_STEER_TOOL = "propose_steer"
"""The one steering tool; the tool-list test holds the chat agent to it."""

READ_TOOLS = (
    "run_summary",
    "list_ideas",
    "get_idea",
    "list_steps",
    "list_targets",
    "list_state_files",
    "read_state_file",
)
"""The read-only tools, in the order they are offered."""


class _NoArgs(BaseModel):
    """Argument schema for tools that take no input."""

    model_config = ConfigDict(extra="forbid")


class _IdeaArgs(BaseModel):
    """Argument schema for `get_idea`."""

    model_config = ConfigDict(extra="forbid")

    idea_id: str = Field(description="Idea id, as returned by list_ideas.")


class _StateFileArgs(BaseModel):
    """Argument schema for `read_state_file`."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(
        description="Portable state file path, as returned by list_state_files."
    )


class _ProposeSteerArgs(BaseModel):
    """Argument schema for `propose_steer`: the fields of a steer the operator may send."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        description="The guidance, written to the agents that will read it.",
    )
    target: SteerTarget = Field(
        default_factory=SteerTarget,
        description=(
            "Whom it is for: {kind: run} for the whole run (the default), or {kind: role, id}, "
            "{kind: attempt, id} or {kind: item, id}."
        ),
    )
    mode: SteerMode = Field(
        default=SteerMode.NEXT_TURN,
        description=(
            "next_turn: the target's next turn takes it, once. standing: every turn of the "
            "target from now on, until the operator withdraws it. interrupt: a turn running "
            "now, else the next one."
        ),
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
            f"current_step: {projection.current_round}\n"
            f"active_idea_id: {projection.active_hypothesis_id or '(none)'}\n"
            f"experiment_revision: {projection.experiment_revision}\n"
            f"idea_count: {len(projection.hypotheses)}\n"
            f"step_count: {len(projection.rounds)}"
        )

    return ToolSpec(
        name="run_summary",
        description=(
            "Return this run's top-level status: outer loop, current step, active idea, "
            "and experiment revision."
        ),
        input_schema=_NoArgs,
        handler=handler,
    )


def _list_ideas_tool(store: RunStore, run_id: str) -> ToolSpec[_NoArgs]:
    def handler(_args: _NoArgs) -> str:
        projection = agent_projection(store.get_run(run_id))
        if projection is None or not projection.hypotheses:
            return "(no ideas)"
        lines = []
        for idea in projection.hypotheses:
            marker = " [active]" if idea.hypothesis_id == projection.active_hypothesis_id else ""
            lines.append(
                f"{idea.hypothesis_id}{marker}: {idea.title or '(untitled)'} "
                f"(steps {idea.first_round}-{idea.last_round}, "
                f"outcome={idea.resolved_outcome or 'pending'})"
            )
        return "\n".join(lines)

    return ToolSpec(
        name="list_ideas",
        description="List every idea tried in this run, with its step range and outcome.",
        input_schema=_NoArgs,
        handler=handler,
    )


def _get_idea_tool(store: RunStore, run_id: str) -> ToolSpec[_IdeaArgs]:
    def handler(args: _IdeaArgs) -> str:
        projection = agent_projection(store.get_run(run_id))
        idea = (
            None
            if projection is None
            else next((h for h in projection.hypotheses if h.hypothesis_id == args.idea_id), None)
        )
        if idea is None:
            return f"(no idea {args.idea_id!r}; see list_ideas)"
        lines = [
            f"id: {idea.hypothesis_id}",
            f"title: {idea.title or '(none)'}",
            f"claim: {idea.claim or '(none)'}",
            f"action: {idea.action or '(none)'}",
            f"steps: {idea.first_round}-{idea.last_round}",
            f"resolved_outcome: {idea.resolved_outcome or '(pending)'}",
            f"judge_verdict: {idea.judge_verdict or '(none)'}",
            f"strategy_disposition: {idea.strategy_disposition}",
            f"active: {idea.active}",
            f"best: {idea.best}",
        ]
        if idea.perf_metric is not None:
            lines.append(
                f"perf_metric: {idea.perf_metric} {idea.perf_unit or ''} "
                f"(step {idea.perf_metric_round}, delta {idea.perf_delta_pct})"
            )
        lines.append("steps:")
        lines.extend(
            f"  step {step.round_number}: passed={step.passed} "
            f"reviewed={step.reviewed} outcome={step.hypothesis_outcome or '-'} "
            f"judge={step.judge_verdict or '-'} commit={step.commit or '-'} "
            f"perf={step.perf_metric} {step.perf_unit or ''}"
            for step in idea.rounds
        )
        return "\n".join(lines)

    return ToolSpec(
        name="get_idea",
        description=(
            "Return one idea's full record: claim, action, disposition, and per-step history."
        ),
        input_schema=_IdeaArgs,
        handler=handler,
    )


def _list_steps_tool(store: RunStore, run_id: str) -> ToolSpec[_NoArgs]:
    def handler(_args: _NoArgs) -> str:
        projection = agent_projection(store.get_run(run_id))
        if projection is None or not projection.rounds:
            return "(no steps)"
        return "\n".join(
            f"step {step.round_number}: passed={step.passed} "
            f"commit={step.commit or '-'} perf={step.perf_metric} "
            f"{step.perf_unit or ''} official={step.official_evaluation} "
            f"profile_skipped={step.profile_skipped}"
            for step in projection.rounds
        )

    return ToolSpec(
        name="list_steps",
        description="List every completed step in this run, run-wide and in order.",
        input_schema=_NoArgs,
        handler=handler,
    )


def _list_targets_tool(store: RunStore, run_id: str) -> ToolSpec[_NoArgs]:
    def handler(_args: _NoArgs) -> str:
        targets = store.get_record(run_id).steer_targets()
        if targets is None:
            return "(the run has committed no state yet: only {kind: run} can be named)"
        attempts = ", ".join(
            f"{row.attempt_id} (item {row.item_id}{', ended' if row.ended else ''})"
            for row in targets.attempts
        )
        return (
            f"roles: {', '.join(targets.roles) or '(none)'}\n"
            f"attempts: {attempts or '(none)'}\n"
            f"items: {', '.join(targets.items) or '(none)'}"
        )

    return ToolSpec(
        name="list_targets",
        description=(
            "List the roles, attempts and items a steer can name in this run, as the run holds "
            "them now. An attempt marked ended takes no steer. Use these ids in propose_steer."
        ),
        input_schema=_NoArgs,
        handler=handler,
    )


def _list_state_files_tool(store: RunStore, run_id: str) -> ToolSpec[_NoArgs]:
    def handler(_args: _NoArgs) -> str:
        documents = store.get_record(run_id).portable_documents()
        if not documents:
            return "(no state files)"
        return "\n".join(str(document.relative_path) for document in documents)

    return ToolSpec(
        name="list_state_files",
        description=(
            "List this run's raw portable state file paths (step records, run manifest, "
            "and similar), for use with read_state_file."
        ),
        input_schema=_NoArgs,
        handler=handler,
    )


def _read_state_file_tool(store: RunStore, run_id: str) -> ToolSpec[_StateFileArgs]:
    def handler(args: _StateFileArgs) -> str:
        for state_file in store.get_record(run_id).portable_documents():
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


def _propose_steer_tool(store: RunStore, run_id: str) -> ToolSpec[_ProposeSteerArgs]:
    """Build `propose_steer`.

    The arguments are validated by the same models the operator's `SteerRequest` uses (target
    kind and id agree, ids have the control schema's shape, mode is one of the three). The
    target must also be one the run holds as of its last committed state
    (`RunRecord.steer_targets`, the rule the run resolves every steer against), so a proposal
    the run would refuse is refused here, naming the valid ids. The run still resolves the
    target again when the operator sends: the run may have moved on since.
    """

    def handler(args: _ProposeSteerArgs) -> str:
        _check_target(store.get_record(run_id).steer_targets(), args.target)
        receipt = SteerProposalReceipt(
            kind=STEER_PROPOSAL_KIND,
            proposal=SteerProposal(text=args.text, target=args.target, mode=args.mode),
            note=(
                "Proposed to the operator, who can edit it, send it or dismiss it. It has not "
                "been sent and you cannot send it: say what you proposed and why, and that "
                "the operator decides."
            ),
        )
        return receipt.model_dump_json()

    return ToolSpec(
        name=PROPOSE_STEER_TOOL,
        description=(
            "Suggest guidance for the run's agents. The operator sees it as a card they can "
            "edit, send or dismiss; nothing reaches the run unless they send it, and you "
            "cannot send it. Read the steering skill first."
        ),
        input_schema=_ProposeSteerArgs,
        handler=handler,
    )


def _check_target(targets: SteerTargets | None, target: SteerTarget) -> None:
    """Raise `ValueError` naming why the run would refuse ``target`` and what it would accept."""
    if target.id is None:
        return
    if targets is None:
        message = (
            f"{target.kind.value} {target.id!r}: the run has committed no state yet, so it has "
            "nothing to name; propose a steer for the whole run ({kind: run})"
        )
        raise ValueError(message)
    refusal = targets.refusal(target)
    if refusal is not None:
        raise ValueError(refusal.reason)


def build_tools(store: RunStore, run_id: str) -> tuple[ToolSpec[Any], ...]:
    """Build every tool the chat agent gets for one run: the read-only ones and `propose_steer`."""
    # Touch the run once up front so a missing run_id fails fast and
    # identically across every tool, instead of each "get_run" discovering it lazily.
    store.get_run(run_id)
    return (
        _run_summary_tool(store, run_id),
        _list_ideas_tool(store, run_id),
        _get_idea_tool(store, run_id),
        _list_steps_tool(store, run_id),
        _list_targets_tool(store, run_id),
        _list_state_files_tool(store, run_id),
        _read_state_file_tool(store, run_id),
        _propose_steer_tool(store, run_id),
    )
