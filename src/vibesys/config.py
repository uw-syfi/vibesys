"""Typed, schema-driven configuration for ``agent.toml``.

The whole config is described by the :class:`Config` pydantic model and its
nested sections. ``load_config`` parses the TOML, validates it against that
schema (fail-fast: missing required fields, unknown providers/backends, wrong
types, **and unknown keys** all raise), applies environment-variable overrides,
and returns a typed :class:`Config`. Consumers use attribute access
(``config.model.name``) — there is no dict-style access anywhere.

Every section is ``extra="forbid"``: a stray or misspelled key is an error
rather than being silently dropped, which is the failure mode the previous
allowlist loader suffered from.
"""

import tomllib
from collections.abc import Mapping  # noqa: TC003  # tracked: #288
from pathlib import Path
from typing import Any, Literal, Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vibesys.constants import DEFAULT_COMPUTE_BACKEND, PROJECT_ROOT, ComputeBackend
from vibesys.features import FeatureFlag
from vibesys.repository import REPOSITORY_COMPONENT, RepositoryVisibility
from vs_feature_flags import parse_feature_flag_overrides

Provider = Literal["vertex-ai", "anthropic", "google-genai", "openai", "openai-compatible"]


class _Strict(BaseModel):
    """Base for every config section: reject unknown keys."""

    model_config = ConfigDict(extra="forbid")


class ModelCfg(_Strict):  # noqa: D101  # tracked: #288
    name: str = Field(description="Model identifier, e.g. 'claude-sonnet-4-6'. Required.")
    # Deprecated: accepted so existing agent.toml files load, but ignored.
    provider: Provider | None = Field(
        default=None,
        description="Deprecated and ignored: the agent CLI provider is selected by [agent].",
    )


class ThinkingCfg(_Strict):  # noqa: D101  # tracked: #288
    level: str | None = Field(
        default=None,
        description=(
            "Reasoning effort level passed to the model (provider-specific, e.g. "
            "'low'/'medium'/'high'). Mutually exclusive with budget."
        ),
    )
    budget: int | None = Field(
        default=None,
        ge=-1,
        description=(
            "Thinking token budget (provider-specific). Alternative to level; -1 requests "
            "a dynamic budget and 0 disables thinking where supported."
        ),
    )

    @model_validator(mode="after")
    def _one_thinking_control(self) -> Self:
        if self.level is not None and self.budget is not None:
            raise ValueError("thinking.level and thinking.budget are mutually exclusive")  # noqa: TRY003  # tracked: #288
        return self


class VertexCfg(_Strict):  # noqa: D101  # tracked: #288
    # Deprecated: accepted so existing agent.toml files load, but ignored.
    # The attribute is ``json_path`` to avoid shadowing ``BaseModel.json``; the
    # TOML key stays ``json`` via the alias.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    json_path: str | None = Field(
        default=None,
        alias="json",
        description=("Path to the Vertex AI service-account JSON key file. Accepted but ignored."),
    )
    project: str | None = Field(
        default=None,
        description=("GCP project id. Accepted but ignored."),
    )
    region: str = Field(
        default="us-east5",
        description="Vertex AI region/location.",
    )


class OpenAICompatCfg(_Strict):  # noqa: D101  # tracked: #288
    # Deprecated: accepted so existing agent.toml files load, but ignored.
    base_url: str | None = Field(
        default=None,
        description=(
            "Base URL of the OpenAI-compatible endpoint "
            "(e.g. 'http://localhost:8000/v1'). Required for this provider."
        ),
    )
    api_key: str = Field(
        default="no-key",
        description="API key for the endpoint; 'no-key' for unauthenticated local servers.",
    )


class _CredEnvProviderCfg(_Strict):
    """A provider whose credentials come from the environment (``.env``).

    The ``[providers.<name>]`` table carries no keys; it exists only as a marker.
    Declared so the table validates under ``extra="forbid"`` while still
    rejecting stray keys placed under it.
    """


class ProvidersCfg(_Strict):  # noqa: D101  # tracked: #288
    # Deprecated: every [providers.*] table is accepted so existing agent.toml
    # files load, but none is read.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    vertex_ai: VertexCfg | None = Field(
        default=None,
        alias="vertex-ai",
        description="Vertex AI provider settings ([providers.vertex-ai]).",
    )
    openai_compatible: OpenAICompatCfg | None = Field(
        default=None,
        alias="openai-compatible",
        description=("OpenAI-compatible endpoint settings ([providers.openai-compatible])."),
    )
    anthropic: _CredEnvProviderCfg | None = Field(
        default=None,
        description="Anthropic provider marker. Accepted but ignored.",
    )
    google_genai: _CredEnvProviderCfg | None = Field(
        default=None,
        alias="google-genai",
        description="Google GenAI provider marker. Accepted but ignored.",
    )
    openai: _CredEnvProviderCfg | None = Field(
        default=None,
        description="OpenAI provider marker. Accepted but ignored.",
    )


class BackendCfg(_Strict):  # noqa: D101  # tracked: #288
    name: ComputeBackend = Field(
        default=DEFAULT_COMPUTE_BACKEND,
        description=(
            "Compute backend, coerced from the TOML string. One of: cuda, metal, "
            "trainium, rocm, cpu."
        ),
    )


class AgentRoleCfg(_Strict):
    """Optional model controls for one conceptual agent-loop role."""

    model: str | None = Field(
        default=None,
        description="CLI model override for this role. None uses [model].name.",
    )
    reasoning_effort: str | None = Field(
        default=None,
        description=(
            "CLI reasoning-effort override for this role (for Codex, for example "
            "'low'/'medium'/'high'/'xhigh'). None uses [thinking].level."
        ),
    )


class AgentCfg(_Strict):  # noqa: D101  # tracked: #288
    driver: Literal["agentshim", "omnigent"] | None = Field(
        default=None,
        description=(
            "Optional agent execution driver override. When omitted, VibeSys "
            "uses its current default driver. This is independent of the "
            "agent provider selected below."
        ),
    )
    backend: str | None = Field(
        default=None,
        description=(
            "Agent runner backend: 'cli' (drive an external coding-agent CLI). "
            "The --agent-backend flag overrides; defaults to 'cli'."
        ),
    )
    cli_provider: str | None = Field(
        default=None,
        description=(
            "Which CLI coding-agent to drive: codex | claude | gemini | opencode. "
            "The --cli-provider flag overrides; defaults to 'codex'."
        ),
    )
    cli_timeout: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Per-invocation timeout for the CLI agent, in seconds. None → the runner default."
        ),
    )
    outer: AgentRoleCfg = Field(
        default_factory=AgentRoleCfg,
        description="[agent.outer] — model controls for orchestrator invocations.",
    )
    inner: AgentRoleCfg = Field(
        default_factory=AgentRoleCfg,
        description="[agent.inner] — model controls for implementer invocations.",
    )


class RepositoryCfg(_Strict):  # noqa: D101  # tracked: #288
    owner: str | None = Field(
        default=None,
        description=(
            "Optional default GitHub user or organization for experiment repositories. "
            "When omitted, use the authenticated account from `gh`."
        ),
    )
    visibility: RepositoryVisibility = Field(
        default=RepositoryVisibility.PRIVATE,
        description="Default visibility for interactively created experiment repositories.",
    )

    @field_validator("owner")
    @classmethod
    def _valid_owner(cls, value: str | None) -> str | None:
        if value is None:
            return None
        owner = value.strip()
        if not REPOSITORY_COMPONENT.fullmatch(owner):
            raise ValueError("repository owner must be one GitHub user or organization name")  # noqa: TRY003  # tracked: #288
        return owner


class LoadLevelCfg(_Strict):
    """One benchmark load level fed to the perf_eval prompt template.

    Distinct from the ``LoadLevelMetrics`` *output* schema in ``schemas.py``.
    """

    rate: int = Field(gt=0, description="Request rate (requests/sec) for this load level.")
    duration: int = Field(gt=0, description="Benchmark duration in seconds at this load level.")
    max_tokens: int = Field(gt=0, description="Max output tokens per request at this load level.")


class PerfEvalCfg(_Strict):  # noqa: D101  # tracked: #288
    load_levels: list[LoadLevelCfg] | None = Field(
        default=None,
        description=(
            "Benchmark load levels handed to the perf evaluator. None → the "
            "evaluator uses its built-in default ladder."
        ),
    )


class Config(_Strict):  # noqa: D101  # tracked: #288
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: ModelCfg = Field(description="[model] — model name and provider. Required.")
    thinking: ThinkingCfg = Field(
        default_factory=ThinkingCfg, description="[thinking] — reasoning/thinking controls."
    )
    providers: ProvidersCfg = Field(
        default_factory=ProvidersCfg,
        description="[providers.*] — per-provider credentials and endpoints.",
    )
    backend: BackendCfg = Field(
        default_factory=BackendCfg, description="[backend] — compute backend selection."
    )
    agent: AgentCfg = Field(
        default_factory=AgentCfg,
        description="[agent] — agent runner backend and CLI-agent settings.",
    )
    repository: RepositoryCfg = Field(
        default_factory=RepositoryCfg,
        description="[repository] — defaults for remote experiment repositories.",
    )
    perf_eval: PerfEvalCfg = Field(
        default_factory=PerfEvalCfg,
        description="[perf_eval] — performance-evaluation settings.",
    )
    feature_flags: dict[FeatureFlag, bool] = Field(
        default_factory=dict,
        description="[feature_flags] — typed feature-flag overrides.",
    )

    @field_validator("feature_flags", mode="before")
    @classmethod
    def _parse_feature_flags(cls, value: object) -> dict[FeatureFlag, bool]:
        return parse_feature_flag_overrides(value, FeatureFlag)


def as_config(config: "Config | Mapping[str, Any]") -> "Config":
    """Coerce a mapping to a validated :class:`Config`; pass through instances.

    The loop entrypoints accept either a parsed :class:`Config` (the normal CLI
    path) or a raw mapping (tests, programmatic callers) and normalize here.
    """
    return config if isinstance(config, Config) else Config.model_validate(config)


def _load_dotenv_file(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load environment variables from a ``.env`` file via ``python-dotenv``.

    Existing environment variables take precedence (``override=False``); a
    missing file is a no-op. Delegating to ``python-dotenv`` gets us correct
    handling of ``export`` prefixes, quoting, inline comments, and multiline
    values for free.
    """
    load_dotenv(path, override=False)


def load_config(path: Path, *, ignored_sections: frozenset[str] = frozenset()) -> Config:
    """Load and validate core configuration from a shared TOML file.

    Application entrypoints may name top-level sections they own in
    ``ignored_sections``. The core schema remains strict for every section it
    accepts without needing to know which applications embed it.
    """
    _load_dotenv_file()
    path = Path(path)
    with open(path, "rb") as f:  # noqa: PTH123  # tracked: #288
        raw = {key: value for key, value in tomllib.load(f).items() if key not in ignored_sections}

    return Config.model_validate(raw)
