"""Portable project and orchestration run manifest schemas."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"

Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
Sha256Digest = Annotated[str, Field(pattern=_DIGEST_PATTERN)]
GitObjectId = Annotated[str, Field(pattern=_GIT_OBJECT_ID_PATTERN)]
PortableText = Annotated[str, Field(min_length=1, max_length=256)]


class _InvalidOrchestrationOptionsError(ValueError):
    def __init__(self) -> None:
        super().__init__("orchestration options must contain finite JSON values")


class _CommittedManifest(BaseModel):
    """Strict base for versioned, portable metadata committed with source.

    Each concrete manifest declares its own ``schema_version`` literal so the
    project and run schemas can evolve independently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProjectManifest(_CommittedManifest):
    """Immutable identity and initial provenance of one project directory."""

    schema_version: Literal[1]
    project_id: Identifier
    created_at: AwareDatetime
    initial_input_fingerprint: Sha256Digest


class RunResourceRequest(BaseModel):
    """Portable compute resources required by one run environment.

    Operator-owned cluster profiles resolve this logical request to concrete
    infrastructure. Provider names, partitions, accounts, images, paths, and
    transient allocation identifiers deliberately do not belong here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    nodes: Annotated[int, Field(gt=0)] = 1
    accelerators_per_node: Annotated[int, Field(gt=0)]
    accelerator_backend: Literal["cuda", "rocm", "trainium"]
    cpus_per_node: Annotated[int, Field(gt=0)] | None = None


class RunEnvironmentRecord(BaseModel):
    """Runtime environment a run executes in, recorded for faithful resume.

    ``name`` selects the environment; the remaining fields carry that
    environment's operator-selected options and stay ``None`` when they do not
    apply. Values a run derives from its own input (rather than from the
    operator) are deliberately absent: they are re-derived on every launch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Literal["local", "docker", "modal", "skypilot"]
    image: PortableText | None = None
    gpu: PortableText | None = None
    model_volume: PortableText | None = None
    app: PortableText | None = None
    resources: RunResourceRequest | None = None


class OrchestrationDescriptor(_CommittedManifest):
    """Versioned, portable settings owned and validated by an orchestration."""

    id: Identifier
    config_version: Annotated[int, Field(gt=0)]
    options: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_portable_options(self) -> Self:
        try:
            json.dumps(self.options, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise _InvalidOrchestrationOptionsError from exc
        return self


class RunExecutionRecord(_CommittedManifest):
    """Resolved host settings needed to resume the same execution environment."""

    model: PortableText
    agent_backend: PortableText
    agent_driver: PortableText | None = None
    cli_provider: PortableText | None = None
    cli_timeout: Annotated[int, Field(gt=0)] | None = None
    compute_backend: PortableText
    requested_profiler: PortableText
    resolved_profiler: PortableText
    default_reasoning_effort: PortableText | None = None
    thinking_budget: Annotated[int, Field(ge=-1)] | None = None
    outer_model: PortableText | None = None
    outer_reasoning_effort: PortableText | None = None
    inner_model: PortableText | None = None
    inner_reasoning_effort: PortableText | None = None
    perf_eval_load_levels: list[dict[str, int]] | None = None
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    skills_dirs: list[str] = Field(default_factory=list)


class OrchestrationRunManifest(_CommittedManifest):
    """Version 4 run manifest independent of orchestration implementation."""

    schema_version: Literal[4]
    run_id: Identifier
    project_id: Identifier
    task_name: Identifier | None = None
    display_name: PortableText
    created_at: AwareDatetime
    input_fingerprint: Sha256Digest
    trusted_input_baseline: GitObjectId
    branch: PortableText
    vibesys_version: PortableText
    run_environment: RunEnvironmentRecord
    execution: RunExecutionRecord
    orchestration: OrchestrationDescriptor
