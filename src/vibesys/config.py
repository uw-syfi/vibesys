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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from vibesys.constants import DEFAULT_COMPUTE_BACKEND, PROJECT_ROOT, ComputeBackend
from vibesys.repository import REPOSITORY_COMPONENT, RepositoryVisibility
from vs_agent.api import SHIPPED_PROVIDERS, validate_env_names
from vs_core.api import MAX_REQUEST_RETRIES, MAX_SUBMISSIONS_PER_MEASUREMENT, RequestRetries
from vs_runtime.api import AgentRoleId, EvaluationRetries
from vs_runtime.api.infrastructure import (
    BundledResources,
    FallbackTarget,
    QuotaAction,
    QuotaPolicy,
)

BUNDLED_RESOURCES = BundledResources(PROJECT_ROOT / "resources", package="vibesys")
# Skills VibeSys installs for optimization agents: the domain skills only, never the rest of
# the published ``skills/`` tree. The wheel stages them as ``vibesys/_skills``.
BUNDLED_SKILLS = BundledResources(
    PROJECT_ROOT / "skills" / "domains", package="vibesys", packaged_subdir="_skills"
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class _Strict(BaseModel):
    """Base for every config section: reject unknown keys."""

    model_config = ConfigDict(extra="forbid")


class ModelCfg(_Strict):
    """Model identifier from agent.toml."""

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description=(
            "Model identifier, as the selected CLI provider's --model flag takes it. The "
            "--model flag overrides. Must be a vetted model of that provider (agentshim's "
            "known models for it); None uses that provider's default."
        ),
    )


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
    """Optional provider and model controls for one conceptual agent-loop role."""

    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description=(
            "CLI provider for this role. None uses [agent].cli_provider. A role that names "
            "a provider other than the run's and no model runs that provider's default model."
        ),
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description=(
            "CLI model override for this role. None uses [model].name, or the role's provider's "
            "default model when the role names another provider."
        ),
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
            "Agent runner backend: 'cli' (drive an external coding-agent CLI) or 'scripted' "
            "(agent-less: the real backend with scripted agents and no model). "
            "The --agent-backend flag overrides; defaults to 'cli'."
        ),
    )
    scripted_seed: int = Field(
        default=0,
        ge=0,
        description=(
            "Seed of the 'scripted' backend's choices: with the same seed and the same project, "
            "the scripted agents plan, edit and judge the same way. Read only with that backend."
        ),
    )
    cli_provider: str | None = Field(
        default=None,
        description=(
            "Which CLI coding-agent to drive: codex | claude | gemini | opencode. "
            "The --cli-provider flag overrides; defaults to 'codex'. [model].name "
            "must be one of its vetted models."
        ),
    )
    cli_timeout: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Per-invocation timeout for the CLI agent, in seconds. None → the agent "
            "runner's hang guard, vs_agent's DEFAULT_TURN_TIMEOUT_S (six hours)."
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
    queue_wait_budget_seconds: int = Field(
        default=43_200,
        strict=True,
        gt=0,
        description=(
            "How long one measurement's job may wait in the executor's queue (Slurm "
            "PENDING) before the run gives the measurement up as queue-budget-spent. "
            "Queue time does not count against the measurement's own time limit."
        ),
    )
    queue_notice_interval_seconds: int = Field(
        default=600,
        strict=True,
        gt=0,
        description=(
            "How often the run reports a measurement job that is still queued, with the "
            "executor's pending reason, in seconds of queue time."
        ),
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

    infrastructure_attempts: int = Field(
        default=3,
        strict=True,
        ge=1,
        le=MAX_SUBMISSIONS_PER_MEASUREMENT,
        description=(
            "How many times the backend measures a candidate in all when a try fails for a "
            "reason in the machinery (a node fault, a lost job). The search sees only the last."
        ),
    )
    ambiguous_attempts: int = Field(
        default=2,
        strict=True,
        ge=1,
        le=MAX_SUBMISSIONS_PER_MEASUREMENT,
        description=(
            "How many times the backend measures a candidate in all when a try fails and the "
            "evidence cannot say whose fault it is. At most infrastructure_attempts."
        ),
    )
    request_retry_limit: int = Field(
        default=3,
        strict=True,
        ge=0,
        le=MAX_REQUEST_RETRIES,
        description="How often a request the executor did not run is sent again.",
    )
    request_backoff_seconds: float = Field(
        default=1.0,
        gt=0,
        description="Wait before the first re-send of a request; each further wait doubles.",
    )
    request_backoff_cap_seconds: float = Field(
        default=30.0, gt=0, description="The longest wait between two sends of one request."
    )

    @model_validator(mode="after")
    def _cap_covers_interval(self) -> Self:
        if self.observe_backoff_cap_seconds < self.observe_interval_seconds:
            message = "observe_backoff_cap_seconds must be at least observe_interval_seconds"
            raise ValueError(message)
        if self.ambiguous_attempts > self.infrastructure_attempts:
            message = "ambiguous_attempts must be at most infrastructure_attempts"
            raise ValueError(message)
        if self.request_backoff_cap_seconds < self.request_backoff_seconds:
            message = "request_backoff_cap_seconds must be at least request_backoff_seconds"
            raise ValueError(message)
        return self

    def request_retries(self) -> RequestRetries:
        """The backend's declaration for core's re-sends of requests the executor did not run."""
        return RequestRetries(
            limit=self.request_retry_limit,
            backoff=self.request_backoff_seconds,
            backoff_cap=self.request_backoff_cap_seconds,
        )

    def evaluation_retries(self) -> EvaluationRetries:
        """The backend's declaration for measuring again after a failure that is not the candidate's."""
        return EvaluationRetries(
            infrastructure_attempts=self.infrastructure_attempts,
            ambiguous_attempts=self.ambiguous_attempts,
        )


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
    cancellation_bound_seconds: int = Field(
        default=60,
        strict=True,
        gt=0,
        description="The longest core waits for a cancelled job to be confirmed gone.",
    )
    cleanup_bound_seconds: int = Field(
        default=60,
        strict=True,
        gt=0,
        description=(
            "The least time core gives its own cleanup and inspection requests, even once "
            "the run deadline has passed."
        ),
    )
    reconciliation_bound_seconds: int = Field(
        default=60,
        strict=True,
        gt=0,
        description="The longest core waits to learn what became of a request it could not read.",
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

    model: ModelCfg = Field(
        default_factory=ModelCfg,
        description="[model] — the agent model; omitted uses the CLI provider's default.",
    )
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


class _HostSettings(_Strict):
    """The host file that names which models live runs on this machine may use.

    ``allowed_models`` is the only key. Missing, or an empty list, means no
    restriction. Unknown keys are rejected and the key is named.
    """

    allowed_models: tuple[str, ...] = Field(default=(), description="Model ids live runs may use.")

    @field_validator("allowed_models")
    @classmethod
    def _named_models(cls, models: tuple[str, ...]) -> tuple[str, ...]:
        blank = [index for index, model in enumerate(models) if not model.strip()]
        if blank:
            message = "allowed_models entries must be non-empty model ids"
            raise ValueError(message)
        return models


def host_settings_path() -> Path:
    """``~/.config/vibesys/host.toml``, the machine-wide file of allowed models.

    The directory is the user's VibeSys configuration directory, the same one
    that holds the machine's other settings. A missing file means no restriction.
    """
    return Path.home() / ".config" / "vibesys" / "host.toml"


def load_host_allowed_models(path: Path | None = None) -> frozenset[str]:
    """The models ``path`` allows, or none when the file is missing.

    ``path`` defaults to :func:`host_settings_path`. An empty list is no
    restriction. A file that cannot be parsed, or that names an unknown key,
    raises ``ValueError`` naming that key or the file.
    """
    location = host_settings_path() if path is None else path
    if not location.exists():
        return frozenset()
    if not location.is_file():
        message = f"{location.name}: expected a file"
        raise ValueError(message)
    try:
        document = tomllib.loads(location.read_text(encoding="utf-8"))
    except OSError as error:
        message = f"{location.name}: {error.strerror or error}"
        raise ValueError(message) from error
    except tomllib.TOMLDecodeError as error:
        message = f"{location.name}: {error}"
        raise ValueError(message) from error
    try:
        settings = _HostSettings.model_validate(document)
    except ValidationError as error:
        keys = _host_setting_keys(error)
        message = f"{location.name}: {keys}"
        raise ValueError(message) from error
    return frozenset(settings.allowed_models)


def _host_setting_keys(error: ValidationError) -> str:
    """The keys ``error`` names, in the order reported, each once."""
    named: list[str] = []
    for item in error.errors():
        loc = item.get("loc") or ()
        key = "allowed_models" if not loc else str(loc[0])
        if key not in named:
            named.append(key)
    return ", ".join(named)


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
