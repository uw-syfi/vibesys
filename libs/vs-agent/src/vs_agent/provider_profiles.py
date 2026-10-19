"""Single seam for reading agentshim's provider facts.

agentshim owns what a provider *is*: its binary, state directories, auth
environment variables, and container install recipe. VibeSys owns policy on
top of that: which providers it offers, which files inside a state directory
carry credentials, and what else a container needs. Every VibeSys module that
needs a provider fact reads it through here, at call time, so a library
upgrade changes behaviour without a VibeSys edit and tests have one place to
substitute a profile.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from types import ModuleType

    from agentshim import CommandExecutor, ProviderProfile


def _agentshim() -> ModuleType:
    """Import ``agentshim`` on first use (see :func:`provider_profile` for why)."""
    import agentshim  # noqa: PLC0415  # lint-waiver: LW-010181 [PLC0415]; Keep agentshim lazy in provider_profile so unused providers and import cycles stay unloaded.

    return agentshim


def provider_profile(provider: str) -> ProviderProfile:
    """Return the agentshim profile for *provider*.

    Imports ``agentshim`` lazily: this module is reachable from
    :mod:`vs_agent.api`'s eager exports (through :mod:`vs_agent.provider_policy`,
    :mod:`vs_agent.spec`, and others), and importing :mod:`vs_agent.api` must
    not pull in ``agentshim``.

    Raises:
        ValueError: if agentshim does not register *provider*. The message
            names the provider and the registered alternatives.
    """
    return _agentshim().get_provider(provider).profile


def provider_binary(provider: str) -> str:
    """The program name of *provider*'s CLI, as agentshim's profile names it."""
    return provider_profile(provider).binary


def launch_shell_environment() -> Mapping[str, str]:
    """The environment a run finds provider CLIs in: the interactive login shell's.

    Provider CLIs are usually installed by a shell rc file that a non-interactive
    process (an SSH command, a service) never sources, so the run reads ``PATH`` and
    credentials from the shell a user would get. agentshim reads it once per process.
    Whoever asks whether a provider CLI is installed asks here, so the answer is the
    run's.
    """
    return _agentshim().interactive_env()


def model_context_window(provider: str, model: str | None) -> int | None:
    """*model*'s context window in tokens when *provider* runs it, or ``None`` when unknown.

    agentshim owns the fact (``agentshim.context_window``). An unknown model is
    ``None``, never a guess from a similar name, so a caller shows no limit
    rather than a wrong one.
    """
    return _agentshim().context_window(provider, model)


@dataclass(frozen=True, slots=True)
class ModelDisplay:
    """How one model reads, as agentshim names it (``agentshim.ModelSpec``)."""

    model_name: str
    """The model alone ("Opus 5.5")."""
    vendor: str
    """Who made the model, as agentshim names the vendor ("anthropic", "openai", "google")."""
    family: str
    """The model line ("Claude", "GPT", "Gemini")."""
    tier: int | None
    """The model's rank in its family, 1 the smallest and cheapest; ``None`` off the ladder."""
    tier_name: str | None
    """The tier as people call it ("Opus"); ``None`` exactly when ``tier`` is."""
    generation: ModelGenerationFacts | None
    """The generation of its vendor's the model belongs to; ``None`` when agentshim names none."""


@dataclass(frozen=True, slots=True)
class ModelGenerationFacts:
    """One generation of a vendor's models, as agentshim ranks it."""

    id: str
    """Names the generation within its vendor ("5.5"); models of one generation share it."""
    name: str
    """The generation as people call it ("Claude 5.5")."""
    rank: int
    """The generation's rank among its vendor's, 1 the newest."""


def provider_display_name(provider: str) -> str:
    """*provider*'s CLI as people call it ("Claude Code"), from agentshim's profile."""
    return provider_profile(provider).display_name


def model_display(provider: str, model: str | None) -> ModelDisplay | None:
    """How *model* run through *provider* reads, or ``None`` when agentshim does not name it.

    agentshim owns the names (``agentshim.model_spec``). An unnamed model is ``None``,
    never a prettified id: a caller shows the raw id.
    """
    spec = _agentshim().model_spec(provider, model)
    if spec is None or spec.display_name is None or spec.family is None:
        return None
    return ModelDisplay(
        model_name=spec.display_name,
        vendor=spec.vendor,
        family=spec.family,
        tier=spec.tier,
        tier_name=spec.tier_name,
        generation=(
            None
            if spec.generation_id is None or spec.generation is None or spec.generation_rank is None
            else ModelGenerationFacts(
                id=spec.generation_id, name=spec.generation, rank=spec.generation_rank
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class LoginCommand:
    """The command that starts *provider*'s own sign-in on this machine."""

    argv: tuple[str, ...]
    """The program and its arguments."""
    needs_terminal: bool
    """The command prompts the person, so a process without a terminal cannot finish it."""

    @property
    def line(self) -> str:
        """The command as a person would type it."""
        return shlex.join(self.argv)


def provider_login_command(provider: str) -> LoginCommand | None:
    """The command that signs *provider*'s CLI in, or ``None`` when its CLI has none.

    agentshim owns the commands (``ProviderProfile.login``). The policy here is which one to
    run: the device-code variant when the CLI has one, because it finishes on any machine,
    including one with no browser to open; otherwise the CLI's usual sign-in.
    """
    profile = provider_profile(provider)
    login = profile.login
    if login is None:
        return None
    device = login.device_argv is not None
    return LoginCommand(
        argv=login.command(profile.binary, device=device),
        needs_terminal=login.needs_terminal and not device,
    )


class LoginState(StrEnum):
    """Whether a provider CLI is signed in, as far as its own tooling can say."""

    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"
    #: Not installed, or the CLI offers no cheap check, or the check could not run.
    UNKNOWN = "unknown"


class LoginKind(StrEnum):
    """What a signed-in CLI uses: a person's account, or a static API key."""

    ACCOUNT = "account"
    API_KEY = "api_key"


@dataclass(frozen=True, slots=True)
class LoginCredential:
    """How a CLI is signed in, never the secret: ``variable`` or ``file`` names where an API key is held.

    ``variable`` is an environment variable name and ``file`` a home-relative path; at most one is
    set, and neither for an account login or a key whose place the CLI did not say.
    """

    kind: LoginKind
    variable: str | None = None
    file: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderLogin:
    """One provider CLI's login on a machine: installed, signed in, how, and what could sign it in."""

    provider: str
    installed: bool
    state: LoginState
    credential: LoginCredential | None
    """How it is signed in; ``None`` unless ``state`` is signed in, and when the CLI did not say."""
    api_key_variables: tuple[str, ...]
    """The environment variables that sign the CLI in with an API key, in its precedence order."""
    detail: str
    """The CLI's own words about its login, one line, for a log or a tooltip."""


def provider_login(
    provider: str,
    *,
    env: Mapping[str, str],
    home: Path,
    executor: CommandExecutor | None = None,
    timeout: float = 15.0,
) -> ProviderLogin:
    """Probe *provider*'s CLI for whether it is installed and signed in, and how, without a turn.

    agentshim owns every fact: ``probe_provider`` runs the CLI's version and status commands in
    *env* (on *executor*, the host's by default), and the profile's credential sources say which
    variable or file the CLI signs in with. No secret is read into the result: a variable's name
    and a home-relative path only. A missing binary is a result. Raises ``ValueError`` for a
    provider agentshim does not register.
    """
    shim = _agentshim()
    profile = provider_profile(provider)
    status = shim.probe_provider(provider, executor=executor, env=env, timeout=timeout)
    state = {
        shim.AuthState.KNOWN_OK: LoginState.SIGNED_IN,
        shim.AuthState.FAILED: LoginState.SIGNED_OUT,
        shim.AuthState.UNKNOWN: LoginState.UNKNOWN,
    }[status.auth]
    variables = tuple(
        source.name
        for source in profile.credential_sources
        if isinstance(source, shim.EnvCredential) and source.kind is shim.CredentialKind.API_KEY
    )
    return ProviderLogin(
        provider=provider,
        installed=status.binary_found,
        state=state,
        credential=_credential(shim, profile, status, env, home)
        if state is LoginState.SIGNED_IN
        else None,
        api_key_variables=variables,
        detail=status.auth_detail.strip().splitlines()[0] if status.auth_detail.strip() else "",
    )


def _credential(
    shim: ModuleType,
    profile: ProviderProfile,
    status: object,
    env: Mapping[str, str],
    home: Path,
) -> LoginCredential | None:
    """The kind the CLI reports, with the place agentshim's plan names when it agrees."""
    reported = getattr(status, "credential", None)
    try:
        plan = shim.host_auth_plan(profile, env=env, home=home)
    except shim.HostAuthUnplannableError:
        plan = None
    kind = reported if reported is not None else (None if plan is None else plan.credential)
    if kind is None:
        return None
    login = LoginKind.API_KEY if kind is shim.CredentialKind.API_KEY else LoginKind.ACCOUNT
    if plan is None or plan.credential is not kind or login is LoginKind.ACCOUNT:
        return LoginCredential(login)
    if plan.files:
        return LoginCredential(login, file=plan.source)
    return LoginCredential(login, variable=plan.source)
