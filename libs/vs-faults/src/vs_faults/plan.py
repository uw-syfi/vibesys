"""The declarative, seeded fault plan every boundary wrapper reads.

A plan is a list of rules. A rule names a boundary, an optional target at that
boundary (an agent role for agent turns, a tool name for tool calls, a cluster
operation for cluster commands), the ordinal of the matching call it fires on
(the plan's clock: calls are counted per boundary and target, so a schedule is
deterministic regardless of wall time), and the fault. A call that no rule
matches passes through unchanged, so an empty plan is the identity.
"""

from __future__ import annotations

import random
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from pathlib import Path


class Boundary(StrEnum):
    """An interface a fault is injected at."""

    AGENT_TURN = "agent_turn"
    TOOL_CALL = "tool_call"
    CLUSTER = "cluster"


class AgentFault(StrEnum):
    """What a faulted agent turn does instead of answering."""

    CRASH = "crash"  # the CLI process dies mid-turn
    TIMEOUT = "timeout"  # the turn outlives the client's turn budget
    MALFORMED = "malformed"  # the reply holds no JSON object
    SCHEMA_INVALID = "schema_invalid"  # JSON that violates the declared schema
    EXTRA_KEYS = "extra_keys"  # a valid reply plus keys the schema does not declare
    WRONG_VALUES = "wrong_values"  # schema-valid, with ids reused from the prompt or invented


class ToolFault(StrEnum):
    """What a faulted tool call does."""

    ERROR = "error"  # the server answers with an error result and does nothing
    DROPPED = "dropped"  # the call never reaches the server; the client reports a failure
    TIMEOUT = "timeout"  # the server runs the call, but the reply arrives after the client gave up
    DUPLICATE = "duplicate"  # the client delivers the call twice; the agent sees the second reply


class ClusterOperation(StrEnum):
    """A cluster call, classified from the connector request."""

    SBATCH = "sbatch"
    SQUEUE = "squeue"
    SACCT = "sacct"
    SCANCEL = "scancel"
    EXEC = "exec"  # any other remote command
    TRANSFER = "transfer"  # put, get, or sync


class ClusterFault(StrEnum):
    """What a faulted cluster call does."""

    SSH_DOWN = "ssh_down"  # transport failure (exit 255), nothing reaches the cluster
    COMMAND_ERROR = "command_error"  # the Slurm command fails (controller timeout), nothing happens
    KILLED = "killed"  # sbatch: the job is submitted, then dies (OOM, preemption, node failure)
    WRONG_STATE = "wrong_state"  # squeue/sacct: the answer is garbage the parser has to reject


_FAULT_TYPES: dict[Boundary, type[AgentFault | ToolFault | ClusterFault]] = {
    Boundary.AGENT_TURN: AgentFault,
    Boundary.TOOL_CALL: ToolFault,
    Boundary.CLUSTER: ClusterFault,
}


class FaultRule(BaseModel):
    """Fire ``fault`` on the ``at``-th call (1-based) at ``boundary`` matching ``target``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary: Boundary
    target: str | None = None
    at: int = Field(ge=1)
    fault: AgentFault | ToolFault | ClusterFault

    @model_validator(mode="before")
    @classmethod
    def _fault_of_boundary(cls, data: object) -> object:
        """Parse ``fault`` as its boundary's fault type (fault names repeat across them)."""
        if isinstance(data, dict) and "boundary" in data and "fault" in data:
            kinds = _FAULT_TYPES[Boundary(data["boundary"])]
            return {**data, "fault": kinds(str(data["fault"]))}
        return data


_FAULTS: dict[Boundary, tuple[StrEnum, ...]] = {
    Boundary.AGENT_TURN: tuple(AgentFault),
    Boundary.TOOL_CALL: tuple(ToolFault),
    Boundary.CLUSTER: tuple(ClusterFault),
}
_CLUSTER_FAULTS: dict[ClusterFault, tuple[ClusterOperation, ...]] = {
    ClusterFault.SSH_DOWN: tuple(ClusterOperation),
    ClusterFault.COMMAND_ERROR: (
        ClusterOperation.SBATCH,
        ClusterOperation.SQUEUE,
        ClusterOperation.SACCT,
        ClusterOperation.SCANCEL,
        ClusterOperation.EXEC,
        ClusterOperation.TRANSFER,
    ),
    ClusterFault.KILLED: (ClusterOperation.SBATCH,),
    ClusterFault.WRONG_STATE: (ClusterOperation.SQUEUE, ClusterOperation.SACCT),
}


class FaultPlan(BaseModel):
    """A seeded fault schedule; ``seed`` also seeds every generated agent reply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int
    rules: tuple[FaultRule, ...] = ()

    def match(self, boundary: Boundary, target: str, ordinal: int) -> FaultRule | None:
        """Return the rule for the ``ordinal``-th call at ``boundary`` on ``target``, if any."""
        for rule in self.rules:
            if (
                rule.boundary is boundary
                and rule.at == ordinal
                and (rule.target is None or rule.target == target)
            ):
                return rule
        return None

    def rng(self, *scope: object) -> random.Random:
        """Return a generator seeded by the plan seed and ``scope`` (stable across runs)."""
        return random.Random(repr((self.seed, *scope)))  # noqa: S311  # LW-150001 [S311]; fault schedules and generated replies must be reproducible from a seed; the secrets module cannot be seeded and nothing here is security-sensitive.

    def save(self, path: Path) -> Path:
        """Write the plan as JSON (the cluster wrapper runs in another process)."""
        path.write_text(self.model_dump_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> FaultPlan:
        """Read a plan written by :meth:`save`, rejecting unknown keys."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @classmethod
    def generate(
        cls,
        seed: int,
        *,
        targets: dict[Boundary, tuple[str, ...]],
        faults: int,
        horizon: int = 6,
    ) -> FaultPlan:
        """Draw ``faults`` rules over ``targets`` within the first ``horizon`` calls of each.

        ``targets`` lists the agent roles and tool names of the run under test;
        cluster targets are the closed :class:`ClusterOperation` set. A boundary
        absent from ``targets`` gets no faults.
        """
        rng = cls(seed=seed).rng("plan")
        boundaries = sorted(targets)
        rules: list[FaultRule] = []
        for _ in range(faults if boundaries else 0):
            boundary = rng.choice(boundaries)
            if boundary is Boundary.CLUSTER:
                cluster_fault = rng.choice(tuple(ClusterFault))
                fault: AgentFault | ToolFault | ClusterFault = cluster_fault
                target: str | None = rng.choice(_CLUSTER_FAULTS[cluster_fault]).value
            else:
                fault = rng.choice(_FAULTS[boundary])  # ty: ignore[invalid-assignment]
                target = rng.choice(targets[boundary]) if targets[boundary] else None
            rules.append(
                FaultRule(
                    boundary=boundary,
                    target=target,
                    at=rng.randint(1, horizon),
                    fault=fault,
                )
            )
        return cls(seed=seed, rules=tuple(rules))
