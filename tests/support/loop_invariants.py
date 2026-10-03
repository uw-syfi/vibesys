"""Dynamic-loop invariants checked from one run's own records.

A whole-loop test (the real-agent smoke tier, or an in-process harness with
generative Fake agents) runs the loop, loads its records into
:class:`RunRecords`, and asserts ``check(records) == []``. The checks read
only records, never agent choices, so they hold for any agent behavior:

- ``core-events.jsonl``: core events as JSON objects (an in-process harness
  passes ``event.model_dump(mode="json")`` for each ``CoreEvent``);
- ``dynamic/state.json``: the dynamic plugin's durable state;
- ``usage.jsonl``: one row per finished agent turn, in the run's and each
  workspace's ``logs`` directory;
- the Fake Slurm cluster's job ledger (``vs_slurm.fake_connector`` state).

Each violated invariant yields a :class:`Violation` naming the
:class:`Invariant` and the offending record.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

type Record = Mapping[str, object]


class Invariant(StrEnum):
    """One loop invariant a run's records must satisfy."""

    # The run ends in exactly one typed terminal event.
    TERMINAL_STATUS = "terminal_status"
    # A run that completes scheduled at least one workstream or profile.
    EMPTY_COMPLETION = "empty_completion"
    # An offered capability is served at least once or withdrawn after a
    # typed ``unsupported``; it is never offered again after withdrawal.
    CAPABILITY_UNSERVED = "capability_unserved"
    # No MCP tool call ends in a client-side timeout or goes unanswered.
    TOOL_TIMEOUT = "tool_timeout"
    # Every run path a prompt names exists when the turn starts.
    MISSING_PROMPT_PATH = "missing_prompt_path"
    # No evaluation is submitted after a stop is requested.
    EVALUATION_AFTER_STOP = "evaluation_after_stop"
    # A stopped run ends within its grace bound.
    STOP_OVERRAN = "stop_overran"
    # No cluster job is left pending or running after the run ends.
    CLUSTER_JOB_LEFT = "cluster_job_left"
    # Every finished agent turn records its token usage.
    USAGE_UNRECORDED = "usage_unrecorded"


@dataclass(frozen=True, slots=True)
class Violation:
    """One invariant violation and the evidence for it."""

    invariant: Invariant
    detail: str


#: Terminal core event types and the statuses they may carry.
_TERMINAL_TYPES = frozenset({"run_finished", "run_failed"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
#: Tool-result text of a client-side MCP timeout (Claude CLI, Codex CLI, MCP SDK).
_TIMEOUT = re.compile(r"-32001|timed out|timeout exceeded|deadline exceeded", re.IGNORECASE)
#: Tool-result text of a capability the executor cannot serve.
_UNSERVABLE = re.compile(
    r"cannot produce evidence kind|not supported|unsupported|not available", re.IGNORECASE
)
#: An absolute path in prompt text, up to whitespace, a quote, or a closing bracket.
_PATH = re.compile(r"(?<![\w.])/[\w.@+-][^\s`'\"<>()\[\]{},;]*")
#: A workspace-relative file in backticks: a name with an extension, or a slashed path.
_RELATIVE = re.compile(
    r"`([\w@+-][\w.@+-]*(?:/[\w.@+-]+)*\.[A-Za-z]{1,5}|[\w.@+-]+(?:/[\w.@+-]+)+/?)`"
)
_SUBMITTED = "submitted"


@dataclass(frozen=True)
class RunRecords:
    """The records of one finished run."""

    events: Sequence[Record]
    state: Record | None = None
    usage: Sequence[Record] = ()
    # Fake Slurm job id -> its ledger line ("" while the job script runs).
    cluster_jobs: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def load(
        cls, logs_dir: Path, state_file: Path | None = None, cluster: Path | None = None
    ) -> RunRecords:
        """Load records from a run's logs directory, state file, and Fake cluster state."""
        state = (
            json.loads(state_file.read_text(encoding="utf-8"))
            if state_file is not None and state_file.is_file()
            else None
        )
        jobs_dir = cluster / "jobs" if cluster is not None else None
        jobs = (
            {path.name: path.read_text(encoding="utf-8").strip() for path in jobs_dir.iterdir()}
            if jobs_dir is not None and jobs_dir.is_dir()
            else {}
        )
        return cls(
            events=_jsonl(logs_dir / "core-events.jsonl"),
            state=state,
            # A turn's usage row lands in its workspace's log directory:
            # the run's own for root-workspace roles, a candidate's otherwise.
            usage=[
                row
                for path in (
                    logs_dir / "usage.jsonl",
                    *sorted(logs_dir.parent.glob("runtime/workspaces/*/logs/usage.jsonl")),
                )
                for row in _jsonl(path)
            ],
            cluster_jobs=jobs,
        )


def _jsonl(path: Path) -> list[Record]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _data(event: Record) -> Mapping[str, object]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _time(event: Record) -> datetime:
    return datetime.fromisoformat(str(event["timestamp"]))


def check(
    records: RunRecords,
    *,
    roots: Iterable[Path] = (),
    exists: Callable[[Path], bool] | None = None,
    stop_grace_s: float | None = None,
) -> list[Violation]:
    """Return every invariant violation in ``records``.

    ``roots`` bounds the prompt-path check to run-owned directories; ``exists``
    answers whether a path existed when its turn started. A relative path is
    relative to the turn's workspace, which only the caller knows, so the
    default ``exists`` checks absolute paths now and accepts relative ones.
    ``stop_grace_s`` bounds the time from the first stop request to the end.
    """
    return [
        *terminal_status(records),
        *capabilities(records),
        *tool_timeouts(records),
        *prompt_paths(records, tuple(roots), exists or _exists_now),
        *stop_bound(records, stop_grace_s),
        *cluster_jobs(records),
        *usage(records),
    ]


def terminal_event(records: RunRecords) -> Record | None:
    """Return the run's terminal event, if it has one."""
    terminal = [event for event in records.events if event.get("type") in _TERMINAL_TYPES]
    return terminal[-1] if terminal else None


def terminal_status(records: RunRecords) -> list[Violation]:
    """The run ends in one typed terminal event; a completed run did work."""
    terminal = [event for event in records.events if event.get("type") in _TERMINAL_TYPES]
    if len(terminal) != 1:
        return [Violation(Invariant.TERMINAL_STATUS, f"{len(terminal)} terminal events")]
    status = terminal[0].get("status")
    if status not in _TERMINAL_STATUSES:
        return [Violation(Invariant.TERMINAL_STATUS, f"terminal status {status!r}")]
    if status == "completed" and records.state is not None:
        scheduled = len(_list(records.state, "workstreams")) + len(_list(records.state, "profiles"))
        if scheduled == 0:
            return [Violation(Invariant.EMPTY_COMPLETION, "completed with zero workstreams")]
    return []


def _list(record: Record, key: str) -> list[Record]:
    value = record.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def capabilities(records: RunRecords) -> list[Violation]:
    """Profiles and MCP tools are served once or withdrawn after ``unsupported``."""
    violations: list[Violation] = []
    profiles = _list(records.state, "profiles") if records.state is not None else []
    statuses = [_profile_status(profile) for profile in profiles]
    unsupported_calls = [
        int(str(profile["planning_call"]))
        for profile, status in zip(profiles, statuses, strict=True)
        if status == "unsupported"
    ]
    if unsupported_calls:
        withdrawn_at = min(unsupported_calls)
        violations.extend(
            Violation(
                Invariant.CAPABILITY_UNSERVED,
                f"profile {profile['profile_id']} planned in call {profile['planning_call']} "
                f"after profiles were withdrawn in call {withdrawn_at}",
            )
            for profile in profiles
            if int(str(profile["planning_call"])) > withdrawn_at
        )
    ended = [status for status in statuses if status is not None]
    if ended and "observed" not in ended and "unsupported" not in ended:
        violations.append(
            Violation(
                Invariant.CAPABILITY_UNSERVED,
                f"profile kind offered, never served or withdrawn: outcomes {ended}",
            )
        )
    violations.extend(_unservable_tools(records.events))
    return violations


def _profile_status(profile: Record) -> str | None:
    outcome = profile.get("outcome")
    return str(outcome.get("status")) if isinstance(outcome, dict) else None


def _tool_results(events: Iterable[Record]) -> dict[str, tuple[Record, Record | None]]:
    """Pair each MCP tool call with its result, keyed by call id."""
    calls: dict[str, tuple[Record, Record | None]] = {}
    for event in events:
        data = _data(event)
        tool = str(data.get("tool", ""))
        if not tool.startswith("mcp__"):
            continue
        call_id = str(data.get("call_id"))
        if event.get("type") == "tool_call":
            calls[call_id] = (event, None)
        elif event.get("type") == "tool_result" and call_id in calls:
            calls[call_id] = (calls[call_id][0], event)
    return calls


def _unservable_tools(events: Sequence[Record]) -> list[Violation]:
    """A tool whose every call was refused as unservable was offered in error."""
    outcomes: dict[str, list[bool]] = {}
    for call, result in _tool_results(events).values():
        if result is None:
            continue
        data = _data(result)
        refused = bool(data.get("is_error")) and bool(
            _UNSERVABLE.search(str(data.get("content", "")))
        )
        outcomes.setdefault(str(_data(call)["tool"]), []).append(refused)
    return [
        Violation(Invariant.CAPABILITY_UNSERVED, f"tool {tool}: all {len(refusals)} calls refused")
        for tool, refusals in outcomes.items()
        if all(refusals)
    ]


def tool_timeouts(records: RunRecords) -> list[Violation]:
    """No MCP call times out client-side or goes unanswered in a run that ended normally."""
    terminal = terminal_event(records)
    ended_normally = terminal is not None and terminal.get("status") == "completed"
    violations: list[Violation] = []
    for call_id, (call, result) in _tool_results(records.events).items():
        tool = _data(call)["tool"]
        if result is None:
            if ended_normally:
                violations.append(
                    Violation(Invariant.TOOL_TIMEOUT, f"{tool} call {call_id} never answered")
                )
            continue
        data = _data(result)
        if data.get("is_error") and _TIMEOUT.search(str(data.get("content", ""))):
            violations.append(
                Violation(
                    Invariant.TOOL_TIMEOUT,
                    f"{tool} call {call_id}: {str(data.get('content'))[:200]}",
                )
            )
    return violations


def _exists_now(path: Path) -> bool:
    return path.exists() if path.is_absolute() else True


def prompt_paths_of(event: Record, roots: Sequence[Path]) -> list[Path]:
    """Return the paths an agent turn's prompts name: run-owned absolute and relative ones."""
    data = _data(event)
    text = f"{data.get('system_prompt') or ''}\n{data.get('user_prompt') or ''}"
    absolute = (Path(match.rstrip(".:")) for match in _PATH.findall(text))
    relative = {Path(match) for match in _RELATIVE.findall(text) if not match.startswith("/")}
    owned = {path for path in absolute if any(path.is_relative_to(root) for root in roots)}
    return sorted(owned | relative)


def prompt_paths(
    records: RunRecords, roots: Sequence[Path], exists: Callable[[Path], bool]
) -> list[Violation]:
    """Every run path a turn's prompt names exists when the turn starts."""
    return [
        Violation(
            Invariant.MISSING_PROMPT_PATH,
            f"{event.get('agent_kind')} turn (sequence {event.get('sequence')}) names {path}",
        )
        for event in records.events
        if event.get("type") == "agent_execution_started"
        for path in prompt_paths_of(event, roots)
        if not exists(path)
    ]


def stop_bound(records: RunRecords, grace_s: float | None) -> list[Violation]:
    """After a stop request, nothing new is submitted and the run ends within the grace."""
    stops = [event for event in records.events if event.get("type") == "stop_requested"]
    if not stops:
        return []
    stopped_at = _time(stops[0])
    violations = [
        Violation(
            Invariant.EVALUATION_AFTER_STOP,
            f"evaluation {_data(event).get('operation_id')} submitted after the stop request",
        )
        for event in records.events
        if event.get("type") == "async_operation_lifecycle"
        and _data(event).get("operation_kind") == "evaluation"
        and _data(event).get("state") == _SUBMITTED
        and _time(event) > stopped_at
    ]
    terminal = terminal_event(records)
    if grace_s is not None and terminal is not None:
        elapsed = (_time(terminal) - stopped_at).total_seconds()
        if elapsed > grace_s:
            violations.append(
                Violation(Invariant.STOP_OVERRAN, f"ended {elapsed:.1f} s after stop > {grace_s}")
            )
    return violations


def cluster_jobs(records: RunRecords) -> list[Violation]:
    """No Fake cluster job is pending or still running once the run has ended."""
    return [
        Violation(Invariant.CLUSTER_JOB_LEFT, f"job {job} left {line or 'RUNNING'}")
        for job, line in sorted(records.cluster_jobs.items())
        if line in {"", "PENDING"}
    ]


def usage(records: RunRecords) -> list[Violation]:
    """Every agent turn that finished records its usage under its role."""
    finished: dict[str, int] = {}
    for event in records.events:
        if event.get("type") == "agent_execution_finished":
            kind = str(event.get("agent_kind"))
            finished[kind] = finished.get(kind, 0) + 1
    recorded: dict[str, int] = {}
    for row in records.usage:
        kind = str(row.get("kind"))
        recorded[kind] = recorded.get(kind, 0) + 1
    return [
        Violation(
            Invariant.USAGE_UNRECORDED,
            f"{kind}: {count} finished turns, {recorded.get(kind, 0)} usage rows",
        )
        for kind, count in sorted(finished.items())
        if recorded.get(kind, 0) < count
    ]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Totals one run records: wall time, tokens, and agent cost."""

    status: str | None
    wall_s: float | None
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float

    def line(self) -> str:
        """Return the one-line summary."""
        wall = f"{self.wall_s:.0f} s" if self.wall_s is not None else "?"
        return (
            f"status={self.status} wall={wall} turns={self.turns} "
            f"input_tokens={self.input_tokens} output_tokens={self.output_tokens} "
            f"cost=${self.cost_usd:.3f}"
        )


def summarize(records: RunRecords) -> RunSummary:
    """Return the run's recorded totals."""
    started = next((e for e in records.events if e.get("type") == "run_started"), None)
    terminal = terminal_event(records)
    wall = (
        (_time(terminal) - _time(started)).total_seconds()
        if started is not None and terminal is not None
        else None
    )

    def total(key: str) -> int:
        return sum(int(str(row.get(key) or 0)) for row in records.usage)

    return RunSummary(
        status=str(terminal.get("status")) if terminal is not None else None,
        wall_s=wall,
        turns=len(records.usage),
        # Providers report input_tokens inclusive of cached input.
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        cost_usd=sum(float(str(row.get("total_cost_usd") or 0.0)) for row in records.usage),
    )


__all__ = [
    "Invariant",
    "RunRecords",
    "RunSummary",
    "Violation",
    "check",
    "prompt_paths_of",
    "summarize",
    "terminal_event",
]
