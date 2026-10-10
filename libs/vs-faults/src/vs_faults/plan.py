"""The declarative, seeded fault plan every boundary wrapper reads.

A plan is a list of rules. A rule names a boundary, an optional target at that
boundary (an agent role for agent turns, a tool name for tool calls, a cluster
operation for cluster commands), the ordinal of the matching call it fires on
(the plan's clock: calls are counted per boundary and target, so a schedule is
deterministic regardless of wall time), and the fault. A call that no rule
matches passes through unchanged, so an empty plan is the identity.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vs_sim.api.testing import fault_stream, match_rule

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import SeededRandom


class Boundary(StrEnum):
    """An interface a fault is injected at."""

    AGENT_TURN = "agent_turn"
    TOOL_CALL = "tool_call"
    CLUSTER = "cluster"
    EXECUTOR_REQUEST = "executor_request"  # a request the host's shell hands to an executor
    DURABLE_WRITE = "durable_write"  # one atomic write of the host's durable run record
    PROCESS_OUTPUT = "process_output"  # one stdout line of a long-lived agent process
    CONVERSATION_TURN = "conversation_turn"  # one turn of a provider conversation


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


class HostFault(StrEnum):
    """What a faulted host-boundary call does.

    Crash is the first kind; later kinds (delay, reorder, external-state lag) are new
    members handled by the gate, so the wrappers do not change.
    """

    CRASH_AFTER = "crash_after"  # the call completes and takes effect, then the host process dies


class ProcessFault(StrEnum):
    """What a faulted line of a long-lived agent process's output does instead of arriving.

    The ordinal counts stdout lines across every process one executor
    spawned, so a rule names a position in the protocol exchange without
    knowing the protocol.
    """

    DIE = "die"  # the process is killed before this line is delivered
    HANG = "hang"  # this line and every later one never arrive; the process stays up
    MALFORMED = "malformed"  # this line arrives corrupted, as output that is not a message
    CONTAINER_REPLACED = (
        "container_replaced"  # every live process dies at once; conversations are lost
    )


class ConversationFault(StrEnum):
    """What a faulted turn of a provider conversation does instead of answering."""

    TRANSIENT = "transient"  # the provider reports an overload or rate limit
    FAILED = "failed"  # the turn fails with an unclassified provider error
    RESUME_REFUSED = "resume_refused"  # the provider no longer has the conversation
    TIMEOUT = "timeout"  # the turn outlives its budget
    EXITED = "exited"  # the provider process exits mid-turn
    MALFORMED = "malformed"  # the turn ends with text that is not the reply it should be


type Fault = AgentFault | ToolFault | ClusterFault | HostFault | ProcessFault | ConversationFault

_FAULT_TYPES: dict[Boundary, type[Fault]] = {
    Boundary.AGENT_TURN: AgentFault,
    Boundary.TOOL_CALL: ToolFault,
    Boundary.CLUSTER: ClusterFault,
    Boundary.EXECUTOR_REQUEST: HostFault,
    Boundary.DURABLE_WRITE: HostFault,
    Boundary.PROCESS_OUTPUT: ProcessFault,
    Boundary.CONVERSATION_TURN: ConversationFault,
}


class FaultRule(BaseModel):
    """Fire ``fault`` on the ``at``-th call (1-based) at ``boundary`` matching ``target``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary: Boundary
    target: str | None = None
    at: int = Field(ge=1)
    fault: Fault

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
    Boundary.EXECUTOR_REQUEST: tuple(HostFault),
    Boundary.DURABLE_WRITE: tuple(HostFault),
    Boundary.PROCESS_OUTPUT: tuple(ProcessFault),
    Boundary.CONVERSATION_TURN: tuple(ConversationFault),
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
        return match_rule(self.rules, boundary.value, target, ordinal)

    def rng(self, *scope: object) -> SeededRandom:
        """Return a stream seeded by the plan seed and ``scope`` (stable across runs)."""
        return fault_stream(self.seed, *scope)

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
                fault: Fault = cluster_fault
                target: str | None = rng.choice(_CLUSTER_FAULTS[cluster_fault]).value
            else:
                fault = rng.choice(_FAULTS[boundary])
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
