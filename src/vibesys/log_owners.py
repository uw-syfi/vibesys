"""What each log stream of a run is for: the owner a stream is attached to.

A log stream (``vs_logs``) is opened under one owner, a typed reference to something the
run model already has: a startup step, a gate stage of a candidate's execution, a tool
build, or the run's own narration. A client attaches the
stream to the view that already shows that owner. The union is a wire contract: the
generated TypeScript type comes from it (``protocol.schema.json``), and an unknown
variant or key is rejected.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, TypeAdapter

from vibesys.events import GateKind, SpanId
from vs_runtime.api.infrastructure import EnvironmentStartupStage


class _Owner(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: SpanId | None = None
    """The run-trace span the stream belongs to, when its writer knows it: what a client opens
    a span's logs by (``logs.list`` filters on it)."""


class StartupStep(_Owner):
    """A step of opening the run's environment, such as building its image."""

    kind: Literal["startup_step"] = "startup_step"
    stage: EnvironmentStartupStage


class GateStage(_Owner):
    """One attempt of one gate of a candidate's evaluation."""

    kind: Literal["gate_stage"] = "gate_stage"
    execution_id: str = Field(min_length=1)
    gate: GateKind
    attempt: NonNegativeInt


class ToolBuild(_Owner):
    """Building or pulling a tool the run needs, such as a profiler."""

    kind: Literal["tool_build"] = "tool_build"
    name: str = Field(min_length=1)


class RunNarration(_Owner):
    """The run's own narration for humans: what it is doing, warnings, tracebacks."""

    kind: Literal["run_narration"] = "run_narration"


type LogOwner = Annotated[
    StartupStep | GateStage | ToolBuild | RunNarration,
    Field(discriminator="kind"),
]
"""What a run's log stream is for."""

LOG_OWNER: TypeAdapter[LogOwner] = TypeAdapter(LogOwner)
"""Validates and serializes owners; the run's log store and reader use it."""
