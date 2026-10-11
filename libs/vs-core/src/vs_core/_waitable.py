"""The reply an agent turn may end with instead of its result: a wait for its evaluations.

A turn that submitted measurements through the evaluation tool may end by naming their handles
with ``waiting_for_evaluation``; the run resumes the turn once when they settle. Every role that
can submit a measurement answers with a *result or a wait*, so the union and its discriminator
are one shared contract, not one per strategy. The bridge validates the handles against the
scope's own admitted submissions (`AgentEvaluationBridge`); this module owns only the wire form.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator


class WaitingForDependency(BaseModel):
    """End this agent turn until every owned evaluation handle settles."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    handles: tuple[str, ...] = Field(min_length=1)

    @field_validator("handles")
    @classmethod
    def _unique_nonempty_handles(cls, handles: tuple[str, ...]) -> tuple[str, ...]:
        if any(not handle.strip() or handle != handle.strip() for handle in handles) or len(
            handles
        ) != len(set(handles)):
            message = "handles must be nonblank and unique"
            raise ValueError(message)
        return handles


class WaitingForEvaluation(WaitingForDependency):
    """End this agent turn until every owned evaluation handle settles."""

    kind: Literal["waiting_for_evaluation"]


def _reply_kind(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("kind", "result"))
    return str(getattr(value, "kind", "result"))


type Waitable[R] = Annotated[
    Annotated[R, Tag("result")] | Annotated[WaitingForEvaluation, Tag("waiting_for_evaluation")],
    Discriminator(_reply_kind),
]
"""A reply that is either the role's result ``R`` or a wait for evaluations.

A result carries no ``kind`` of its own (or any value other than ``waiting_for_evaluation``).
"""
