"""Portable project and run manifest schemas, including legacy version 3 records."""

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


class _OpenEvolveSettingsError(ValueError):
    def __init__(self) -> None:
        super().__init__("OpenEvolve settings require search_policy='openevolve'")


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


class _BaseRunConfiguration(BaseModel):
    """Strict settings shared by every supported outer loop."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_environment: RunEnvironmentRecord
    model: PortableText | None = None
    agent_backend: PortableText
    agent_driver: PortableText | None = None
    cli_provider: PortableText | None = None
    cli_timeout: Annotated[int, Field(gt=0)] | None = None
    compute_backend: PortableText
    profiler: PortableText | None = None
    modality: PortableText | None = None
    default_reasoning_effort: PortableText | None = None
    outer_model: PortableText | None = None
    outer_reasoning_effort: PortableText | None = None
    inner_model: PortableText | None = None
    inner_reasoning_effort: PortableText | None = None


class AgentRunConfiguration(_BaseRunConfiguration):
    """Deprecated for new code: v3 settings for an agent-loop run."""

    outer_loop: Literal["agent", "profile-guided"]
    inner_loop: PortableText
    interface: PortableText
    max_rounds: Annotated[int, Field(gt=0)]
    max_retries_per_round: Annotated[int, Field(gt=0)]
    judge_every: Annotated[int, Field(gt=0)]
    official_eval_every: Annotated[int, Field(gt=0)]
    memory_layout: PortableText
    operator_constraints: tuple[str, ...] = ()
    objectives: tuple[PortableText, ...] = ()


class PlainRunConfiguration(_BaseRunConfiguration):
    """Deprecated for new code: v3 settings for a plain-loop run."""

    outer_loop: Literal["plain"]
    max_rounds: Annotated[int, Field(gt=0)]
    max_attempts_per_issue: Annotated[int, Field(gt=0)]
    max_issues_per_perf_eval: Annotated[int, Field(gt=0)]


class EvolveRunConfiguration(_BaseRunConfiguration):
    """Deprecated for new code: v3 settings for an evolutionary-search run."""

    outer_loop: Literal["evolve"]
    max_generations: Annotated[int, Field(gt=0)]
    children_per_generation: Annotated[int, Field(gt=0)]
    k_top_inspirations: Annotated[int, Field(ge=0)]
    k_random_inspirations: Annotated[int, Field(ge=0)]
    selection_temperature: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    seed: int | None = None
    search_policy: Literal["vibesys", "openevolve"] | None = None
    openevolve_population_size: Annotated[int, Field(gt=0)] | None = None
    openevolve_archive_size: Annotated[int, Field(gt=0)] | None = None
    openevolve_num_islands: Annotated[int, Field(gt=0)] | None = None
    openevolve_migration_interval: Annotated[int, Field(gt=0)] | None = None
    openevolve_migration_rate: Annotated[float, Field(ge=0, le=1)] | None = None
    frontier_bias: Annotated[float, Field(ge=0, le=1)]
    bootstrap_max_attempts: Annotated[int, Field(gt=0)]
    keep_deployments: bool
    max_parallelism: Annotated[int, Field(gt=0)]
    objectives: tuple[PortableText, ...] = ()

    @model_validator(mode="after")
    def _validate_search_policy_settings(self) -> Self:
        openevolve_values = (
            self.openevolve_population_size,
            self.openevolve_archive_size,
            self.openevolve_num_islands,
            self.openevolve_migration_interval,
            self.openevolve_migration_rate,
        )
        if self.search_policy == "vibesys" and any(
            value is not None for value in openevolve_values
        ):
            raise _OpenEvolveSettingsError
        return self


RunConfiguration = Annotated[
    AgentRunConfiguration | PlainRunConfiguration | EvolveRunConfiguration,
    Field(discriminator="outer_loop"),
]
# Deprecated for new code: this closed union is retained for v3 compatibility.


class _BaseRunManifest(_CommittedManifest):
    """Identity and provenance shared by supported run-manifest versions."""

    run_id: Identifier
    project_id: Identifier
    task_name: Identifier | None = None
    display_name: PortableText
    created_at: AwareDatetime
    input_fingerprint: Sha256Digest
    trusted_input_baseline: GitObjectId
    branch: PortableText
    vibesys_version: PortableText


class RunManifest(_BaseRunManifest):
    """Deprecated for new code: version 3 run manifest with loop-specific settings."""

    schema_version: Literal[3]
    configuration: RunConfiguration


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


class OrchestrationRunManifest(_BaseRunManifest):
    """Version 4 run manifest independent of orchestration implementation."""

    schema_version: Literal[4]
    run_environment: RunEnvironmentRecord
    orchestration: OrchestrationDescriptor


RunManifestRecord = RunManifest | OrchestrationRunManifest
