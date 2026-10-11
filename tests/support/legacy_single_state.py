"""The state aggregate the pre-core single loop persisted, for tests that read old runs.

The loop is gone, but run directories it created remain readable by the inspector, the design
and experiment views; these tests write that shape to disk as a fixture.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.hypothesis.state import HypothesisState


class SingleState(BaseModel):
    """The single plugin's opaque durability aggregate before the core port."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    search: HypothesisState = Field(default_factory=HypothesisState)
    last_paid_attempt: dict[str, object] | None = None
    accuracy_receipt: dict[str, object] | None = None
    last_response: dict[str, object] | None = None
