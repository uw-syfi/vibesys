"""One read-only scheduler query for the queue a run's jobs wait in, and its parser.

The query asks, in one remote shell call, for the scheduler's clock, this user's own
pending and running jobs (``squeue --me``), every user's pending and running jobs
reduced to state, partition and priority, and each node's GPUs and how many are in
use (``sinfo``). Other users' jobs are read without any identifier (no job id, name or
user), so nothing built from a ``QueueSnapshot`` can name, cancel or modify them; they
are only counted. Every program it runs (``date``, ``echo``, ``squeue``, ``sinfo``)
only reads.

Times are read in the scheduler's own clock and local time zone
(``SLURM_TIME_FORMAT=standard``, ``%Y-%m-%dT%H:%M:%S``), so a wait is a difference of
two readings from one clock, never a comparison with this host's clock.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import datetime

MINE_MARK = "__VS_QUEUE_MINE__"
EVERYONE_MARK = "__VS_QUEUE_EVERYONE__"
NODES_MARK = "__VS_QUEUE_NODES__"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
_STATES = "PENDING,RUNNING,CONFIGURING"
#: Own jobs: id, state, pending reason, submit time, start (estimated when pending),
#: partitions, priority, then the name last because only it may contain ``|``.
MINE_FORMAT = "%i|%T|%r|%V|%S|%P|%Q|%j"
EVERYONE_FORMAT = "%T|%P|%Q"
NODES_FORMAT = "NodeHost:120|,PartitionName:60|,StateCompact:30|,Gres:300|,GresUsed:300|"
_CLOCK, _OWN_JOB, _QUEUED_JOB, _NODE = "clock", "own job", "queued job", "node"
_MINE_FIELDS = 8
_EVERYONE_FIELDS = 3
_NODE_FIELDS = 5
#: Node states (``sinfo`` compact form, flags stripped) whose GPUs can take a new job.
SCHEDULABLE_NODE_STATES = frozenset(
    {"idle", "mix", "mixed", "alloc", "allocated", "comp", "completing", "plnd", "planned"}
)


class QueueViewError(ValueError):
    """The scheduler's answer does not have the shape the query asks for."""

    @classmethod
    def missing_section(cls, mark: str) -> QueueViewError:
        """Name the section marker that never arrived."""
        return cls(f"queue reading has no {mark} section")

    @classmethod
    def bad_line(cls, section: str, line: str) -> QueueViewError:
        """Name the section and the line that does not parse."""
        return cls(f"queue reading {section} line does not parse: {line[:120]!r}")


@dataclass(frozen=True, slots=True)
class OwnJob:
    """One of this user's pending or running jobs."""

    job_id: str
    name: str
    state: str
    reason: str | None
    submitted: datetime | None
    start: datetime | None
    """When it started (running) or the scheduler's estimate (pending); ``None`` if unknown."""
    partitions: frozenset[str]
    priority: int | None


@dataclass(frozen=True, slots=True)
class QueuedJob:
    """Anyone's pending or running job, with nothing that identifies it."""

    state: str
    partitions: frozenset[str]
    priority: int | None


@dataclass(frozen=True, slots=True)
class NodeGpus:
    """One node in one partition: its state and its GPUs, configured and in use."""

    node: str
    partition: str
    state: str
    gpus: int
    gpus_used: int

    @property
    def schedulable(self) -> bool:
        """Whether the node can take a new job (it is not down, drained or failing)."""
        return self.state in SCHEDULABLE_NODE_STATES


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """One reading of the scheduler: its clock, this user's jobs, everyone's, and the nodes."""

    now: datetime
    mine: tuple[OwnJob, ...]
    everyone: tuple[QueuedJob, ...]
    nodes: tuple[NodeGpus, ...]


def queue_snapshot_command() -> str:
    """The one shell command whose output :func:`parse_queue_snapshot` reads."""
    squeue = f"SLURM_TIME_FORMAT=standard squeue --noheader --states={_STATES}"
    return " && ".join(
        (
            f"date +{shlex.quote(_TIME_FORMAT)}",
            f"echo {MINE_MARK}",
            f"{squeue} --me --format={shlex.quote(MINE_FORMAT)}",
            f"echo {EVERYONE_MARK}",
            f"{squeue} --format={shlex.quote(EVERYONE_FORMAT)}",
            f"echo {NODES_MARK}",
            f"sinfo --noheader --Node --Format={shlex.quote(NODES_FORMAT)}",
        )
    )


def sbatch_partitions(arguments: tuple[str, ...]) -> frozenset[str]:
    """The partitions ``sbatch`` arguments ask for (``--partition=a,b``, ``-p a``, ``-pa``)."""
    named: list[str] = []
    for index, argument in enumerate(arguments):
        following = arguments[index + 1] if index + 1 < len(arguments) else ""
        if argument in {"--partition", "-p"}:
            named.append(following)
        elif argument.startswith("--partition="):
            named.append(argument.removeprefix("--partition="))
        elif argument.startswith("-p") and not argument.startswith("--"):
            named.append(argument.removeprefix("-p"))
    return frozenset(part for value in named for part in _partitions(value))


def parse_queue_snapshot(text: str) -> QueueSnapshot:
    """Read the output of :func:`queue_snapshot_command`; ``QueueViewError`` if malformed."""
    head, mine, everyone, nodes = _sections(text)
    now = _time(head.strip())
    if now is None:
        raise QueueViewError.bad_line(_CLOCK, head)
    return QueueSnapshot(
        now=now,
        mine=tuple(_own_job(line) for line in _lines(mine)),
        everyone=tuple(_queued_job(line) for line in _lines(everyone)),
        nodes=tuple(_node(line) for line in _lines(nodes)),
    )


def _sections(text: str) -> tuple[str, str, str, str]:
    """The clock line and the three sections, split at their marker lines."""
    marks = (MINE_MARK, EVERYONE_MARK, NODES_MARK)
    sections: list[list[str]] = [[]]
    for line in text.splitlines():
        if len(sections) <= len(marks) and line.strip() == marks[len(sections) - 1]:
            sections.append([])
        else:
            sections[-1].append(line)
    if len(sections) <= len(marks):
        raise QueueViewError.missing_section(marks[len(sections) - 1])
    head, mine, everyone, nodes = ("\n".join(lines) for lines in sections)
    return head, mine, everyone, nodes


def _lines(section: str) -> list[str]:
    return [line for line in section.splitlines() if line.strip()]


_SCHEDULER_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")


def _time(value: str) -> datetime | None:
    """A scheduler time, or ``None`` for its words for unknown (``N/A``, ``Unknown``, ``None``).

    The time has no zone: ``squeue`` and ``date`` print the scheduler host's local time,
    and only differences between two such readings are used.
    """
    value = value.strip()
    if _SCHEDULER_TIME.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _priority(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.isdigit() else None


def _partitions(value: str) -> frozenset[str]:
    return frozenset(part.strip().rstrip("*") for part in value.split(",") if part.strip())


def _own_job(line: str) -> OwnJob:
    fields = line.split("|", _MINE_FIELDS - 1)
    if len(fields) != _MINE_FIELDS or not fields[0].strip():
        raise QueueViewError.bad_line(_OWN_JOB, line)
    job_id, state, reason, submitted, start, partitions, priority, name = fields
    reason = reason.strip()
    return OwnJob(
        job_id=job_id.strip(),
        name=name.strip(),
        state=state.strip().upper(),
        reason=reason if reason and reason not in {"None", "(null)"} else None,
        submitted=_time(submitted),
        start=_time(start),
        partitions=_partitions(partitions),
        priority=_priority(priority),
    )


def _queued_job(line: str) -> QueuedJob:
    fields = line.split("|")
    if len(fields) != _EVERYONE_FIELDS:
        raise QueueViewError.bad_line(_QUEUED_JOB, line)
    state, partitions, priority = fields
    return QueuedJob(
        state=state.strip().upper(),
        partitions=_partitions(partitions),
        priority=_priority(priority),
    )


_PARENTHESIZED = re.compile(r"\([^)]*\)")
_STATE_FLAGS = "*~#!%$@^-+"
_GRES_PARTS = 2
"""A GPU GRES entry is ``gpu:count`` or ``gpu:type:count``: at least a name and a count."""


def gpu_count(gres: str) -> int:
    """GPUs in a ``sinfo`` GRES field: ``gpu:a100:4(S:0-1),gpu:2`` holds 6; ``(null)`` none."""
    total = 0
    for entry in _PARENTHESIZED.sub("", gres).split(","):
        parts = entry.strip().split(":")
        if parts[0] != "gpu" or len(parts) < _GRES_PARTS:
            continue
        count = parts[-1]
        if count.isdigit():
            total += int(count)
    return total


def _node(line: str) -> NodeGpus:
    fields = [field.strip() for field in line.split("|")]
    if fields and not fields[-1]:
        fields.pop()  # the last field's own separator
    if len(fields) != _NODE_FIELDS or not fields[0]:
        raise QueueViewError.bad_line(_NODE, line)
    node, partition, state, gres, used = fields
    return NodeGpus(
        node=node,
        partition=partition.rstrip("*"),
        state=state.rstrip(_STATE_FLAGS).lower(),
        gpus=gpu_count(gres),
        gpus_used=gpu_count(used),
    )


__all__ = [
    "EVERYONE_FORMAT",
    "MINE_FORMAT",
    "NODES_FORMAT",
    "SCHEDULABLE_NODE_STATES",
    "NodeGpus",
    "OwnJob",
    "QueueSnapshot",
    "QueueViewError",
    "QueuedJob",
    "gpu_count",
    "parse_queue_snapshot",
    "queue_snapshot_command",
    "sbatch_partitions",
]
