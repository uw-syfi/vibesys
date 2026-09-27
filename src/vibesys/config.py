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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vibesys.constants import DEFAULT_COMPUTE_BACKEND, PROJECT_ROOT, ComputeBackend
from vibesys.repository import REPOSITORY_COMPONENT, RepositoryVisibility
from vs_runtime.api import AgentRoleId
from vs_runtime.api.infrastructure import BundledResources

BUNDLED_RESOURCES = BundledResources(PROJECT_ROOT / "resources", package="vibesys")

if TYPE_CHECKING:
    from collections.abc import Mapping


class _Strict(BaseModel):
    """Base for every config section: reject unknown keys."""

    model_config = ConfigDict(extra="forbid")


class ModelCfg(_Strict):
    """Model identifier from agent.toml."""

    name: str = Field(description="Model identifier, e.g. 'claude-sonnet-4-6'. Required.")


class ThinkingCfg(_Strict):
    """Optional reasoning-effort level or token budget."""

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
            message = "thinking.level and thinking.budget are mutually exclusive"
            raise ValueError(message)
        return self


class BackendCfg(_Strict):
    """Compute backend selected for the run."""

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


class AgentCfg(_Strict):
    """Agent driver and role-specific model controls."""

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
    roles: dict[AgentRoleId, AgentRoleCfg] = Field(
        default_factory=dict,
        description=(
            "Sparse model overrides keyed by plugin-declared agent role ID. "
            "Omitted roles inherit [model] and [thinking]."
        ),
    )


class RepositoryCfg(_Strict):
    """Default GitHub owner and repository visibility."""

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
            message = "repository owner must be one GitHub user or organization name"
            raise ValueError(message)
        return owner


class Config(_Strict):
    """Validated project configuration for one VibeSys run."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: ModelCfg = Field(description="[model] — model name and provider. Required.")
    thinking: ThinkingCfg = Field(
        default_factory=ThinkingCfg, description="[thinking] — reasoning/thinking controls."
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
    with path.open("rb") as f:
        raw = {key: value for key, value in tomllib.load(f).items() if key not in ignored_sections}

    return Config.model_validate(raw)
