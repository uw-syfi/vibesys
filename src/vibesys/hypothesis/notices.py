"""Typed notice data the hypothesis search hands to prompt consumers.

The search policy is pure and never renders text. It selects the facts an
agent needs (the Pareto archive, a regression or terminal-workspace notice,
an exhausted-review notice) and returns them as frozen models. Consumers pass
these models to their templates, and the shared partials under
``vibesys/prompts/shared/_notices/`` own the wording.

Fields hold raw facts (agent-supplied strings unmodified, ``None`` where a
fact is missing, numbers with their recorded ``int`` or ``float`` type); the
templates choose the fallback wording and number formatting.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ArchiveAxis",
    "ArchiveConflict",
    "ArchiveDominator",
    "ArchiveLatestRound",
    "ArchiveMetric",
    "ArchivePendingClaim",
    "ArchiveScalarReading",
    "ArchiveTrustedParent",
    "ExhaustionNotice",
    "OfficialCandidateNotRetained",
    "OmittedPendingClaims",
    "ParetoArchiveView",
    "RegressionNotice",
    "RetainedTerminalCheckpoint",
    "TerminalWorkspaceEdits",
    "WorkspaceCheckpoint",
]


class _Notice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArchiveAxis(_Notice):
    """One configured objective axis."""

    name: str
    direction: Literal["max", "min"]


class ArchiveMetric(_Notice):
    """One objective's value in a metric row, in configured axis order."""

    name: str
    value: float
    direction: Literal["max", "min"]


class ArchiveScalarReading(_Notice):
    """The official scalar metric when no complete objective row exists."""

    value: float
    unit: str | None


class ArchiveLatestRound(_Notice):
    """The most recent completed round and its trusted official evidence.

    ``official_metrics`` is non-empty only for a trusted official evaluation
    with a complete objective row; otherwise ``official_scalar`` holds a
    trusted official scalar if one exists. Both empty means no trusted
    official measurement.
    """

    round_number: int
    commit: str | None
    official_metrics: tuple[ArchiveMetric, ...] = ()
    official_scalar: ArchiveScalarReading | None = None
    retained: bool | None


class ArchiveTrustedParent(_Notice):
    """A trusted point on the noise-aware Pareto frontier."""

    round_number: int
    commit: str
    official: bool
    metrics: tuple[ArchiveMetric, ...]
    operating_point: str
    artifact: str | None


class ArchivePendingClaim(_Notice):
    """A retained frontier claim that is not yet a trusted parent."""

    round_number: int
    commit: str
    metrics: tuple[ArchiveMetric, ...]
    operating_point: str
    artifact: str | None
    reason: str


class OmittedPendingClaims(_Notice):
    """Older pending claims left out of the archive view."""

    count: int
    first_round: int
    last_round: int


class ParetoArchiveView(_Notice):
    """Trusted frontier parents and measured claims awaiting review.

    ``axes`` empty means multi-objective retention is off; only the latest
    round is then meaningful.
    """

    axes: tuple[ArchiveAxis, ...]
    relative_noise: float
    latest: ArchiveLatestRound | None
    trusted_parents: tuple[ArchiveTrustedParent, ...] = ()
    pending_claims: tuple[ArchivePendingClaim, ...] = ()
    omitted_claims: OmittedPendingClaims | None = None


class ArchiveDominator(_Notice):
    """A trusted archive point that dominates a claimed frontier row."""

    round_number: int
    metrics: tuple[ArchiveMetric, ...]


class ArchiveConflict(_Notice):
    """A `pareto_frontier` claim that the live archive dominates."""

    dominators: tuple[ArchiveDominator, ...] = Field(min_length=1)


class RetainedTerminalCheckpoint(_Notice):
    """A terminal hypothesis whose implementation kept a Pareto checkpoint."""

    kind: Literal["retained_checkpoint"] = "retained_checkpoint"
    hypothesis_id: str | None
    outcome: str
    round_number: int
    reviewed: bool
    candidate_metrics: dict[str, int | float]
    commit: str | None


class WorkspaceCheckpoint(_Notice):
    """An earlier nonterminal checkpoint distinct from the recorded parent."""

    round_number: int
    outcome: str | None
    reviewed: bool


class TerminalWorkspaceEdits(_Notice):
    """A terminal hypothesis whose workspace edits are still present."""

    kind: Literal["workspace_edits"] = "workspace_edits"
    hypothesis_id: str | None
    outcome: str
    round_number: int
    parent_round: int | None
    checkpoint: WorkspaceCheckpoint | None


class OfficialCandidateNotRetained(_Notice):
    """A passed round whose official candidate was not retained."""

    kind: Literal["official_not_retained"] = "official_not_retained"
    round_number: int
    perf_metric: int | float | None
    perf_unit: str | None


RegressionNotice = Annotated[
    RetainedTerminalCheckpoint | TerminalWorkspaceEdits | OfficialCandidateNotRetained,
    Field(discriminator="kind"),
]
"""Guidance about the parent to choose after a regression or terminal round."""


class ExhaustionNotice(_Notice):
    """A reviewed round that did not pass within its attempt budget."""

    round_number: int
    attempts: int
    feedback: str | None
