"""Product-owned wiring between VibeSys configuration and runtime adapters."""

from __future__ import annotations

import importlib.metadata
import importlib.util
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from vibesys.config import load_host_allowed_models
from vibesys.constants import DomainName
from vibesys.errors import ConfigInvalid, ModelNotVetted, RolesNotDeclared, configuration_error
from vs_agent.api import (
    DEFAULT_CLI_PROVIDER,
    SHIPPED_PROVIDERS,
    AgentBackend,
    AgentSpec,
    ModelNotAllowedError,
    UnvettedHarnessError,
    check_allowed_model,
    check_harness,
    default_model,
)
from vs_runtime.api.infrastructure import (
    ModelArtifactRequest,
    PreparedModelArtifacts,
    model_artifact_copy_excludes,
    prepare_model_artifacts,
)
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from vibesys.config import AgentRoleCfg, Config
    from vs_runtime.api import AgentRole

_DISTRIBUTION = "vibesys"
"""The distribution whose top-level packages confined tool servers import."""


@dataclass(slots=True)
class AgentToolContext:
    """Product facts available while binding one declared agent tool."""

    profiler_id: str
    profiler_env: tuple[tuple[str, str], ...] = ()
    queue_status_socket: str | None = None
    """The run's queue status socket, valid on the host and in every agent container."""


def prepare_domain_model_artifacts(
    domain: DomainName,
    request: ModelArtifactRequest,
    *,
    isolated: bool,
    materialize_local_weights: bool,
) -> PreparedModelArtifacts:
    """Bind the closed LLM-serving domain to runtime model preparation."""
    if domain is not DomainName.LLM_SERVING:
        return PreparedModelArtifacts()
    return prepare_model_artifacts(
        request,
        isolated=isolated,
        materialize_local_weights=materialize_local_weights,
    )


def domain_model_copy_excludes(domain: DomainName, *, isolated: bool) -> frozenset[str]:
    """Reference entries a project copy leaves out for ``domain`` because they are mounted."""
    if domain is not DomainName.LLM_SERVING:
        return frozenset()
    return model_artifact_copy_excludes(isolated=isolated)


def _vibesys_runtime_host_resources() -> tuple[HostResource, ...]:
    """Declare the installed product packages needed by host-confined agents.

    Stdio tool servers run ``python -m <first-party module>`` inside the
    sandbox. A wheel installs every first-party package under one
    site-packages directory, but an editable checkout keeps each library under
    its own ``libs/<name>/src`` root, so every distinct root must be readable.
    """
    roots = {Path(__file__).resolve().parents[1]}
    for name, distributions in importlib.metadata.packages_distributions().items():
        if _DISTRIBUTION not in distributions:
            continue
        spec = importlib.util.find_spec(name)
        if spec is not None and spec.origin is not None:
            roots.add(Path(spec.origin).resolve().parents[1])
    return tuple(
        HostResource(root, HostResourceAccess.READ_ONLY, "VibeSys runtime")
        for root in sorted(roots)
    )


def resolve_model(config: Config, provider: str, model: str | None = None) -> str:
    """The run's agent model: ``model``, else ``[model].name``, else ``provider``'s default.

    Raises :class:`UnvettedHarnessError` when none of them names one, which
    happens only for a provider with no vetted model.
    """
    resolved = model or config.model.name or default_model(provider)
    if resolved is None:
        raise UnvettedHarnessError(provider, None)
    return resolved


def resolve_provider(config: Config, provider: str | None = None) -> str:
    """The run's CLI provider: ``provider``, else ``[agent].cli_provider``, else the default."""
    return provider or config.agent.cli_provider or DEFAULT_CLI_PROVIDER


def agent_spec_from_config(  # noqa: PLR0913  # lint-waiver: LW-221001 [PLR0913]; the host allow-list is an input independent of project config and the unvetted opt-out. Folding it into Config would make a project file the host restriction, and a wrapper object would only forward the same two values.
    config: Config,
    *,
    backend: AgentBackend | str | None = None,
    provider: str | None = None,
    model: str | None = None,
    allow_unvetted_model: bool = False,
    allowed_models: frozenset[str] | None = None,
    host_settings: Path | None = None,
) -> AgentSpec:
    """Bind application config and explicit overrides into one agent spec.

    ``allowed_models`` overrides the host file. Otherwise the models are read
    from ``host_settings`` (the machine file when omitted). A CLI model outside
    that set is refused here, before a run starts.
    """
    agent_cfg = config.agent
    resolved_backend = AgentBackend(backend or agent_cfg.backend or AgentBackend.CLI)
    resolved_provider = resolve_provider(config, provider)
    return AgentSpec(
        backend=resolved_backend,
        provider=resolved_provider,
        model=resolve_model(config, resolved_provider, model),
        reasoning_effort=config.thinking.level,
        cli_timeout=agent_cfg.cli_timeout,
        env_passthrough=agent_cfg.env_passthrough,
        allow_unvetted_model=allow_unvetted_model,
        scripted_seed=agent_cfg.scripted_seed,
        allowed_models=_host_allowed_models(allowed_models, host_settings),
    )


def resolve_agent_specs(  # noqa: PLR0913  # lint-waiver: LW-221002 [PLR0913]; same inputs as agent_spec_from_config, passed through so a resume re-checks the host list. A wrapper would only rename those inputs.
    config: Config,
    roles: tuple[AgentRole, ...],
    *,
    backend: AgentBackend | str | None = None,
    provider: str | None = None,
    allow_unvetted_model: bool = False,
    allowed_models: frozenset[str] | None = None,
    host_settings: Path | None = None,
) -> Mapping[str, AgentSpec]:
    """Resolve one immutable execution spec per plugin-declared role.

    Authored role entries are sparse overrides. Every authored key must name a
    role in the selected plugin; every declared role is returned, inheriting
    the global model and reasoning defaults when no override is present. A
    model outside the vetted harnesses is rejected naming the key that chose
    it, unless ``allow_unvetted_model``. A model outside the host's allowed
    set is rejected even then, including on resume.
    """
    declared = {role.id for role in roles}
    unknown = sorted(config.agent.roles.keys() - declared)
    if unknown:
        raise configuration_error(RolesNotDeclared(tuple(unknown)))
    with _unvetted_harness_named(role=None):
        default_spec = agent_spec_from_config(
            config,
            backend=backend,
            provider=provider,
            allow_unvetted_model=allow_unvetted_model,
            allowed_models=allowed_models,
            host_settings=host_settings,
        )
    fallback = config.agent.quota
    if (
        fallback.fallback_provider is not None
        and fallback.fallback_model is not None
        and default_spec.backend is AgentBackend.CLI
    ):
        if not allow_unvetted_model:
            with _unvetted_harness_named(role=None, fallback=True):
                check_harness(fallback.fallback_provider, fallback.fallback_model)
        with _unvetted_harness_named(role=None, fallback=True):
            check_allowed_model(
                fallback.fallback_model, role=None, allowed=default_spec.allowed_models
            )
    resolved: dict[str, AgentSpec] = {}
    for role in roles:
        override = config.agent.roles.get(role.id)
        with _unvetted_harness_named(role=role.id):
            provider_name = (
                override.provider
                if override is not None and override.provider is not None
                else default_spec.provider
            )
            if provider_name not in SHIPPED_PROVIDERS:
                raise configuration_error(
                    ConfigInvalid(
                        problem=(
                            f"agent.roles.{role.id}.provider {provider_name!r} is not supported; "
                            f"supported providers: {', '.join(sorted(SHIPPED_PROVIDERS))}"
                        )
                    )
                )
            resolved[role.id] = replace(
                default_spec,
                provider=provider_name,
                model=_role_model(override, default_spec, provider_name),
                reasoning_effort=(
                    override.reasoning_effort
                    if override is not None and override.reasoning_effort is not None
                    else default_spec.reasoning_effort
                ),
            )
    return MappingProxyType(resolved)


def _host_allowed_models(
    allowed_models: frozenset[str] | None, host_settings: Path | None
) -> frozenset[str]:
    """``allowed_models``, else the host file. A bad file is a configuration error."""
    if allowed_models is not None:
        return allowed_models
    try:
        return load_host_allowed_models(host_settings)
    except ValueError as error:
        raise configuration_error(ConfigInvalid(problem=str(error))) from error


def _role_model(override: AgentRoleCfg | None, default: AgentSpec, provider: str) -> str | None:
    """A role's model: its own, else the run's, else the default of the provider it names.

    The run's model belongs to the run's provider, so a role on another provider never inherits
    it. Raises :class:`UnvettedHarnessError` for a provider with no vetted model.
    """
    if override is not None and override.model is not None:
        return override.model
    if provider == default.provider:
        return default.model
    model = default_model(provider)
    if model is None:
        raise UnvettedHarnessError(provider, None)
    return model


def require_one_login_per_container(specs: Mapping[str, AgentSpec], *, containerized: bool) -> None:
    """Refuse roles on more than one provider where a container carries one provider's login.

    A run environment that starts containers gives each container the login of the run's
    provider only, so a role on another provider could not authenticate in it. Agent-less
    runs carry no login, so only the CLI backend's roles count.
    """
    providers = sorted({s.provider for s in specs.values() if s.backend is AgentBackend.CLI})
    if containerized and len(providers) > 1:
        raise configuration_error(
            ConfigInvalid(
                problem=(
                    f"agent.roles name more than one provider ({', '.join(providers)}), which "
                    "a run environment that starts containers does not support: its containers "
                    "carry the login of the run's provider only"
                )
            )
        )


@contextmanager
def _unvetted_harness_named(*, role: str | None, fallback: bool = False) -> Iterator[None]:
    """Report an unvetted harness as a configuration error naming whose model it is."""
    try:
        yield
    except UnvettedHarnessError as error:
        raise configuration_error(
            ModelNotVetted(
                provider=error.provider,
                model=error.model,
                vetted=error.allowed,
                role=role,
                fallback=fallback,
            )
        ) from error
    except ModelNotAllowedError as error:
        named = error.at_role(role)
        raise configuration_error(ConfigInvalid(problem=str(named))) from named
