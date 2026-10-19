"""Canonical public request and read models for VibeSys runs."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    field_validator,
    model_validator,
)

from vibesys.config import Config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.events import RunFailure, RunFailureKind
from vibesys.inputs import InputBundle
from vibesys.repository import RepositoryVisibility
from vs_project.api import OrchestrationDescriptor, ProjectStateError, validate_run_id
from vs_runtime.api.infrastructure import RunEnvironmentSpec

_EMPTY_ID = "id must not be empty"
_EMPTY_TEXT = "must not be empty"
_EMPTY_REVISION = "revision must not be empty"
_MAX_HEADLINE_METRICS = 4
_TOO_MANY_HEADLINE_METRICS = "headline_metrics must contain at most 4 items"


def _validate_identity_value(value: str, *, field: str) -> str:
    try:
        return validate_run_id(value)
    except ProjectStateError as exc:
        message = f"{field}: {exc}"
        raise ValueError(message) from exc


class ResumeRef(BaseModel):
    """Identify a prior run that a new session should resume from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str

    @field_validator("run_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _validate_identity_value(value, field="ResumeRef.run_id")


class ProfilerKind(StrEnum):
    """Known profiler modes selectable in a run request."""

    AUTO = "auto"
    NONE = "none"
    NSYS = "nsys"
    NCU = "ncu"
    ROCPROF = "rocprof"
    OTEL = "otel"
    TORCH = "torch"
    NEURON = "neuron"
    MACOS_CPU = "macos_cpu"
    LINUX_CPU = "linux_cpu"
    HEADROOM = "headroom"


class RunRequest(BaseModel):
    """Resolved input and execution settings for one orchestration run.

    The selected policy owns and validates ``orchestration.options`` before
    run setup begins.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    orchestration: OrchestrationDescriptor
    config: Config
    input_bundle: InputBundle
    objective: str | None = None
    resume: ResumeRef | None = None
    exp_name: str | None = None
    run_id: str | None = None
    runs_dir: Path | None = None
    profiler_kind: ProfilerKind = ProfilerKind.AUTO
    skills_dirs: list[str] | None = None
    run_environment: RunEnvironmentSpec | None = None
    agent_backend: str | None = None
    cli_provider: str | None = None
    allow_unvetted_model: bool = False
    """Run a model outside the vetted harnesses: the terminal's explicit opt-out, or a resume."""
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND
    remote_repo: str | None = None
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE

    @field_validator("run_id")
    @classmethod
    def _validate_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identity_value(value, field="RunRequest.run_id")

    @model_validator(mode="after")
    def _validate_resume_identity(self) -> RunRequest:
        if self.resume is not None and self.run_id not in (None, self.resume.run_id):
            message = "RunRequest.run_id must match resume.run_id"
            raise ValueError(message)
        return self

    @property
    def resolved_run_id(self) -> str:
        """Return the resume target or the identity of a fresh run."""
        if self.resume is not None:
            return self.resume.run_id
        if self.run_id is not None:
            return self.run_id
        if self.exp_name is None:
            message = "RunRequest.exp_name must be set for a fresh (non-resume) run"
            raise ValueError(message)
        return self.exp_name

    @property
    def resolved_project_root(self) -> Path:
        """Return the canonical project root that owns this run.

        A fresh collection run is materialized below ``runs_dir``. Resumes and
        direct runs already name their canonical project in ``project_root``.
        """
        return self.project_root_for(self.resolved_run_id)

    def project_root_for(self, run_id: str) -> Path:
        """Return the canonical project root for the resolved ``run_id``."""
        if self.resume is None and self.runs_dir is not None:
            return self.runs_dir.expanduser().resolve() / run_id
        return self.project_root.expanduser().resolve()


class RunStatus(StrEnum):
    """Lifecycle status the public API can report for a run.

    Persisted runs have no lifecycle field and report ``UNKNOWN``.
    A requested cooperative stop reports ``STOPPED`` after cleanup.
    """

    UNKNOWN = "unknown"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class RunResult(BaseModel):
    """Terminal outcome of one run, including a cooperative requested stop."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    succeeded: bool
    status: Literal[RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED] = RunStatus.FAILED
    failure: RunFailure | None = None
    stop_reason: str | None = None
    """Why a stopped run stopped, as its record says; None when the stop left no reason."""

    @model_validator(mode="before")
    @classmethod
    def _terminal_status(cls, value: object) -> object:
        if isinstance(value, dict) and "status" not in value:
            return {
                **value,
                "status": RunStatus.COMPLETED if value.get("succeeded") else RunStatus.FAILED,
            }
        return value

    @model_validator(mode="after")
    def _consistent_status(self) -> RunResult:
        if self.succeeded != (self.status is RunStatus.COMPLETED):
            message = "RunResult.succeeded must agree with status"
            raise ValueError(message)
        if self.failure is not None and self.status is not RunStatus.FAILED:
            message = "only a failed RunResult carries a failure"
            raise ValueError(message)
        if self.stop_reason is not None and self.status is not RunStatus.STOPPED:
            message = "only a stopped RunResult carries a stop reason"
            raise ValueError(message)
        return self


class RoundFailureSummary(BaseModel):
    """Why one round's workstream ended without a measured candidate, strategy-neutral.

    ``kind`` is a plain string rather than a policy's own closed failure
    enum: this module stays below every policy in the dependency graph, so it
    carries the enum's value, not the enum. ``WORKSTREAM_FINISHED``'s wiring
    is the one place that turns it back into the wire's
    ``WorkstreamFailureKind``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: str
    role: str | None = None
    turns: int | None = None
    limit: int | None = None
    text: str = ""
    operator_text: str | None = None
    """Credential-safe projection used only by the public run event."""


class InputFailureSummary(BaseModel):
    """Why the run's input revision failed its own trusted measurement, strategy-neutral.

    Like `RoundFailureSummary`, ``kind`` carries a policy enum's value (the candidates
    library's ``InputFailureKind``), not the enum; ``INPUT_MEASUREMENT_FAILED``'s wiring turns it
    back into the wire's ``InputMeasurementFailureKind``. ``stage`` names the gate stage whose
    reading failed, when one did; ``output`` is what that stage printed, unredacted, so it is
    redacted before it reaches an event.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: str
    stage: str | None = None
    output: str = ""
    attempts: int = Field(default=1, ge=1, le=2)
    """How many times the input was measured: twice when the first failure showed no fault of
    the input and it was measured again."""
    compared: bool = False
    """Whether candidates are still compared with the input: its benchmark passed although its
    accuracy check failed, so its numbers stay the bar."""


class RoundSummary(BaseModel):
    """One policy round in the strategy-neutral shape used for run events."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    number: int
    status: Literal["completed", "failed"]
    attempts: int
    judge_verdict: Literal["pass", "fail", "skipped"] | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    profile_skipped: bool = False
    last_error: RoundFailureSummary | None = None
    # True exactly when the round's last turn was refused another turn
    # because it repeated an earlier one (``FailureKind.REPEATED_TURN``, the
    # dynamic policy's only policy that currently sets this); every other
    # policy leaves it at the default.
    repeated_error: bool = False


class IterationKind(StrEnum):
    """Policy-neutral kinds a client may use to label navigation rows."""

    ROUND = "round"
    WORKSTREAM = "workstream"
    ATTEMPT = "attempt"
    GENERATION = "generation"
    CANDIDATE = "candidate"
    BOARD = "board"
    ITEM = "item"


class IterationStatus(StrEnum):
    """Lifecycle shared by every policy's iteration projection."""

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IterationSupport(StrEnum):
    """Declared iteration-navigation disposition of one policy registration."""

    UNMAPPED = "unmapped"
    PROJECTED = "projected"
    EXTERNAL_STATE_UNAVAILABLE = "external_state_unavailable"


class IterationMetric(BaseModel):
    """One bounded headline measurement attached to an iteration row."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    value: FiniteFloat | None = None
    unit: str | None = None
    direction: Literal["max", "min"] | None = None

    @field_validator("id")
    @classmethod
    def _nonempty_id(cls, value: str) -> str:
        if not value:
            raise ValueError(_EMPTY_ID)
        return value


class IterationSummary(BaseModel):
    """One stable node in a policy's iteration hierarchy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    parent_id: str | None = None
    kind: IterationKind
    label: str
    ordinal: Annotated[int, Field(ge=0)]
    status: IterationStatus
    child_count: Annotated[int, Field(ge=0)] = 0
    headline_metrics: tuple[IterationMetric, ...] = ()

    @field_validator("id", "label")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value:
            raise ValueError(_EMPTY_TEXT)
        return value

    @field_validator("headline_metrics")
    @classmethod
    def _bounded_metrics(cls, value: tuple[IterationMetric, ...]) -> tuple[IterationMetric, ...]:
        if len(value) > _MAX_HEADLINE_METRICS:
            raise ValueError(_TOO_MANY_HEADLINE_METRICS)
        return value


def _index_iteration_nodes(
    nodes: tuple[IterationSummary, ...],
) -> tuple[dict[str, IterationSummary], dict[str, int]]:
    by_id: dict[str, IterationSummary] = {}
    sibling_ordinals: dict[str | None, set[int]] = {}
    child_counts: dict[str, int] = {}
    for index, node in enumerate(nodes):
        if node.id in by_id:
            message = f"nodes[{index}].id duplicates {node.id!r}"
            raise ValueError(message)
        by_id[node.id] = node
        ordinals = sibling_ordinals.setdefault(node.parent_id, set())
        if node.ordinal in ordinals:
            message = (
                f"nodes[{index}].ordinal duplicates {node.ordinal} under parent {node.parent_id!r}"
            )
            raise ValueError(message)
        ordinals.add(node.ordinal)
        if node.parent_id is not None:
            child_counts[node.parent_id] = child_counts.get(node.parent_id, 0) + 1
    return by_id, child_counts


def _validate_iteration_links(
    nodes: tuple[IterationSummary, ...],
    by_id: dict[str, IterationSummary],
    child_counts: dict[str, int],
) -> None:
    for index, node in enumerate(nodes):
        if node.parent_id is not None and node.parent_id not in by_id:
            message = f"nodes[{index}].parent_id references unknown id {node.parent_id!r}"
            raise ValueError(message)
        if node.child_count != child_counts.get(node.id, 0):
            message = (
                f"nodes[{index}].child_count is {node.child_count}, "
                f"expected {child_counts.get(node.id, 0)}"
            )
            raise ValueError(message)
    for index, node in enumerate(nodes):
        visited = {node.id}
        parent_id = node.parent_id
        while parent_id is not None:
            if parent_id in visited:
                message = f"nodes[{index}].parent_id creates a cycle at {parent_id!r}"
                raise ValueError(message)
            visited.add(parent_id)
            parent_id = by_id[parent_id].parent_id


class IterationTree(BaseModel):
    """A complete, validated policy projection before bounded transport paging."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["tree"] = "tree"
    schema_version: Literal[1] = 1
    revision: str
    nodes: tuple[IterationSummary, ...] = ()

    @field_validator("revision")
    @classmethod
    def _nonempty_revision(cls, value: str) -> str:
        if not value:
            raise ValueError(_EMPTY_REVISION)
        return value

    @model_validator(mode="after")
    def _validate_tree(self) -> Self:
        by_id, child_counts = _index_iteration_nodes(self.nodes)
        _validate_iteration_links(self.nodes, by_id, child_counts)
        return self

    @classmethod
    def from_nodes(cls, nodes: tuple[IterationSummary, ...]) -> IterationTree:
        """Build a projection whose revision is stable for identical typed content."""
        ordered = tuple(
            sorted(
                nodes,
                key=lambda node: (
                    node.parent_id is not None,
                    node.parent_id or "",
                    node.ordinal,
                    node.id,
                ),
            )
        )
        content = [node.model_dump(mode="json") for node in ordered]
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
        revision = hashlib.sha256(encoded.encode()).hexdigest()
        return cls(revision=revision, nodes=ordered)


class IterationUnavailableReason(StrEnum):
    """Closed reasons why a policy cannot expose iteration navigation."""

    EXTERNAL_STATE_UNAVAILABLE = "external_state_unavailable"
    POLICY_NOT_MAPPED = "policy_not_mapped"
    PROJECTION_INVALID = "projection_invalid"
    """The policy's projection of the committed state failed validation: a product bug."""
    STATE_UNAVAILABLE = "state_unavailable"


class IterationUnavailable(BaseModel):
    """Typed absence, kept distinct from a supported empty tree."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["unavailable"] = "unavailable"
    schema_version: Literal[1] = 1
    reason: IterationUnavailableReason
    detail: str | None = None
    """For an operator, what went wrong (for `PROJECTION_INVALID`, the validation error)."""


IterationProjection = Annotated[IterationTree | IterationUnavailable, Field(discriminator="kind")]


class PluginProjection(BaseModel):
    """Policy-owned JSON plus product-generic facts exposed by VibeSys."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    payload: dict[str, JsonValue] | None
    iterations: IterationProjection = IterationUnavailable(
        reason=IterationUnavailableReason.POLICY_NOT_MAPPED
    )
    rounds: tuple[RoundSummary, ...] = ()
    experiment_revision: int | None = None
    input_failure: InputFailureSummary | None = None
    """Why the input failed its trusted measurement; None while it is pending or measured."""


class RunView(BaseModel):
    """Read-only run identity, lifecycle, and policy-owned JSON projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    status: RunStatus
    projection: dict[str, JsonValue] | None = None
    iterations: IterationProjection = IterationUnavailable(
        reason=IterationUnavailableReason.STATE_UNAVAILABLE
    )
    rounds: tuple[RoundSummary, ...] = ()
    experiment_revision: int | None = None
    input_failure: InputFailureSummary | None = None
    """Why the input failed its trusted measurement; None while it is pending or measured."""

    @property
    def projection_failure(self) -> str | None:
        """Why the policy's projection of the committed state failed validation; None if not."""
        iterations = self.iterations
        if (
            isinstance(iterations, IterationUnavailable)
            and iterations.reason is IterationUnavailableReason.PROJECTION_INVALID
        ):
            return iterations.detail or iterations.reason.value
        return None


__all__ = [
    "InputFailureSummary",
    "IterationKind",
    "IterationMetric",
    "IterationProjection",
    "IterationStatus",
    "IterationSummary",
    "IterationSupport",
    "IterationTree",
    "IterationUnavailable",
    "IterationUnavailableReason",
    "PluginProjection",
    "ProfilerKind",
    "ResumeRef",
    "RoundSummary",
    "RunFailure",
    "RunFailureKind",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "RunView",
]
