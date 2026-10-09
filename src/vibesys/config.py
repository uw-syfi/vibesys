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
from vs_agent.api import SHIPPED_PROVIDERS, validate_env_names
from vs_runtime.api import AgentRoleId
from vs_runtime.api.infrastructure import (
    BundledResources,
    FallbackTarget,
    QuotaAction,
    QuotaPolicy,
)

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
        min_length=1,
        max_length=256,
        description="CLI model override for this role. None uses [model].name.",
    )
    reasoning_effort: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description=(
            "CLI reasoning-effort override for this role (for Codex, for example "
            "'low'/'medium'/'high'/'xhigh'). None uses [thinking].level."
        ),
    )


class QuotaCfg(_Strict):
    """What the run does, unattended, when a provider has no capacity left.

    ``pause`` (the default) pauses the run and waits for the operator. ``wait``
    pauses it and resumes it by itself when capacity should have returned, using
    the provider's reported reset time or ``retry_seconds`` when it gave none,
    for at most ``wait_seconds`` per turn (no limit when omitted); once that
    budget is spent the turn fails with the quota error. ``fail`` does not pause:
    the turn fails with the quota error at once. ``fallback`` waits like ``wait``
    (not at all when ``wait_seconds`` is omitted) and then switches the run to
    ``fallback_provider`` and ``fallback_model``: the turn in flight ends with the
    quota error and sessions opened afterwards are fresh conversations on the
    fallback. Any policy may name a fallback, which an operator can then choose
    when resuming a paused run.
    """

    policy: Literal["pause", "wait", "fail", "fallback"] = Field(
        default="pause", description="pause | wait | fail | fallback."
    )
    wait_seconds: int | None = Field(
        default=None,
        strict=True,
        gt=0,
        description=(
            "Total seconds one turn may wait for capacity. Only for policy = 'wait' or 'fallback'."
        ),
    )
    retry_seconds: int = Field(
        default=300,
        strict=True,
        gt=0,
        description="Seconds between attempts when the provider gave no reset time.",
    )
    fallback_provider: str | None = Field(
        default=None,
        description=f"Provider to switch to ({' | '.join(SHIPPED_PROVIDERS)}). Needs fallback_model.",
    )
    fallback_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Model to use on the fallback provider. Needs fallback_provider.",
    )

    @field_validator("fallback_provider")
    @classmethod
    def _known_fallback_provider(cls, value: str | None) -> str | None:
        if value is not None and value not in SHIPPED_PROVIDERS:
            message = (
                f"fallback_provider {value!r} is not supported; "
                f"supported providers: {', '.join(SHIPPED_PROVIDERS)}"
            )
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.fallback_provider is None) != (self.fallback_model is None):
            message = "fallback_provider and fallback_model must be set together"
            raise ValueError(message)
        if self.wait_seconds is not None and self.policy not in ("wait", "fallback"):
            message = (
                f"wait_seconds applies only to policy = 'wait' or 'fallback', not {self.policy!r}"
            )
            raise ValueError(message)
        if self.policy == "fallback" and self.fallback_provider is None:
            message = "policy = 'fallback' needs fallback_provider and fallback_model"
            raise ValueError(message)
        return self

    def to_policy(self) -> QuotaPolicy:
        """The runtime policy this section declares."""
        fallback = (
            None
            if self.fallback_provider is None or self.fallback_model is None
            else FallbackTarget(self.fallback_provider, self.fallback_model)
        )
        return QuotaPolicy(
            action=QuotaAction(self.policy),
            wait_seconds=self.wait_seconds,
            retry_seconds=self.retry_seconds,
            fallback=fallback,
        )


class AgentCfg(_Strict):
    """Agent backend, provider, and role-specific model controls."""

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_driver(cls, data: object) -> object:
        """Name the removed ``driver`` key instead of a generic unknown-key error."""
        if isinstance(data, dict) and "driver" in data:
            message = (
                "agent.driver: this key was removed because AgentShim is the only agent "
                f"driver (got {data['driver']!r}); delete the key"
            )
            raise ValueError(message)
        return data

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
    env_passthrough: tuple[str, ...] = Field(
        default=(),
        description=(
            "Launcher environment variable names an agent session inherits beyond "
            "VibeSys's allowlist (PATH, HOME, locale, TERM, proxy and CA variables, "
            "and the selected provider's credential variables)."
        ),
    )

    quota: QuotaCfg = Field(
        default_factory=QuotaCfg,
        description="[agent.quota] — unattended policy for a provider quota or rate-limit stop.",
    )
    roles: dict[AgentRoleId, AgentRoleCfg] = Field(
        default_factory=dict,
        description=(
            "Sparse model overrides keyed by plugin-declared agent role ID. "
            "Omitted roles inherit [model] and [thinking]."
        ),
    )

    @field_validator("env_passthrough")
    @classmethod
    def _env_passthrough_names(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        try:
            return validate_env_names(names)
        except ValueError as exc:
            message = f"agent.env_passthrough: {exc}"
            raise ValueError(message) from exc


class EvaluationCfg(_Strict):
    """Bounds for evaluation suspension, independent of execution backend."""

    queue_allowance_seconds: int = Field(
        default=900,
        strict=True,
        gt=0,
        description="Queue allowance added to declared execution budgets for suspension deadlines.",
    )
    observe_interval_seconds: int = Field(
        default=10,
        strict=True,
        gt=0,
        description="How often the core polls a running evaluation job, in seconds.",
    )
    observe_backoff_cap_seconds: int = Field(
        default=120,
        strict=True,
        gt=0,
        description=(
            "Upper bound of the poll delay, in seconds, after the job's state could not be "
            "read. Must be at least the observe interval."
        ),
    )

    @model_validator(mode="after")
    def _cap_covers_interval(self) -> Self:
        if self.observe_backoff_cap_seconds < self.observe_interval_seconds:
            message = "observe_backoff_cap_seconds must be at least observe_interval_seconds"
            raise ValueError(message)
        return self


class RunCfg(_Strict):
    """Bounds on one whole run, independent of the orchestration."""

    max_run_seconds: int | None = Field(
        default=None,
        strict=True,
        gt=0,
        description=(
            "Optional wall-clock budget for one run, in seconds. When omitted the run is "
            "bounded only by its round budget."
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
    evaluation: EvaluationCfg = Field(
        default_factory=EvaluationCfg,
        description="[evaluation] — evaluation suspension bounds.",
    )
    run: RunCfg = Field(
        default_factory=RunCfg,
        description="[run] — whole-run bounds.",
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
