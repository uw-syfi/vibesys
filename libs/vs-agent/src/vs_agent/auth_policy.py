"""Which credential a CLI agent may log in with, and the plan that enforces it.

The operator chooses the credential kinds a run may bill (``[agent.auth]``)
and, optionally, the API endpoint. Which environment variables and files
carry each kind, and which one a CLI prefers when several are present, are
agentshim's facts: :func:`plan_cli_auth` asks agentshim for the one
credential to hand the CLI, and VibeSys delivers exactly that plan to the
agent's container (:func:`vs_agent.cli_docker.cli_auth_delivery`). The
readiness probe then reports which kind the CLI says it is using, and a kind
the policy does not allow stops the run before its first turn
(:attr:`vs_agent.contracts.ReadinessProblem.CREDENTIAL_NOT_ALLOWED`).

Importing this module does not import ``agentshim``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_agent import provider_profiles

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class CredentialKind(StrEnum):
    """What a CLI's credential bills.

    ``API_KEY`` is a static secret: a vendor API key, or a token a gateway
    issued. ``SUBSCRIPTION`` is a personal or team plan login (Claude.ai,
    ChatGPT, a Google or GitHub account).
    """

    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True)
class AgentAuthPolicy:
    """The credential kinds a run's CLI agents may use, and their endpoint.

    ``endpoint`` ``None`` means the vendor's own endpoint: a base URL left in
    the launching shell is withheld from the agent, not forwarded.
    """

    credentials: frozenset[CredentialKind]
    endpoint: str | None = None

    def __post_init__(self) -> None:
        """Reject a policy that allows no credential."""
        if not self.credentials:
            message = "agent.auth.credentials must allow at least one credential kind"
            raise ValueError(message)


@dataclass(frozen=True)
class PlannedCredentialFile:
    """A host credential file the CLI must find at ``home_path`` under its home.

    ``refreshable`` files hold a login the CLI rewrites on refresh, so they are
    shared writable.
    """

    host_path: Path
    home_path: str
    refreshable: bool


@dataclass(frozen=True)
class CliAuthPlan:
    """Exactly what a CLI gets so it logs in with one allowed credential.

    ``env`` and ``files`` are the whole delivery. ``source`` names the chosen
    credential (a variable name or a home-relative path); ``withheld`` names,
    never values, the credentials present on the host that the plan leaves out.
    """

    provider: str
    credential: CredentialKind
    source: str
    endpoint: str | None
    env: Mapping[str, str]
    files: tuple[PlannedCredentialFile, ...]
    withheld: tuple[str, ...]

    def describe(self) -> str:
        """One log line naming the credential, the endpoint and what was withheld."""
        endpoint = self.endpoint or "the vendor endpoint"
        withheld = ", ".join(self.withheld) or "nothing"
        return (
            f"{self.provider} logs in with {self.credential.value} from {self.source} "
            f"at {endpoint}; withheld from the agent: {withheld}"
        )


class AgentAuthError(ValueError):
    """The auth policy cannot be met on this host; the message says what is missing."""


def plan_cli_auth(
    provider: str, policy: AgentAuthPolicy, *, env: Mapping[str, str], home: Path
) -> CliAuthPlan:
    """Choose the one credential *provider*'s CLI logs in with under *policy*.

    Raises:
        AgentAuthError: no credential the policy allows is present in *env* or
            under *home*, or the CLI cannot meet the policy (it cannot take the
            endpoint, or its credentials cannot be classified).
    """
    # lint-waiver: LW-994811 [PLC0415]; load agentshim only when a run plans its auth.
    # > shim_translation imports agentshim at module scope, and vs_agent.api (which
    # > re-exports this module) must import without it. A module-scope import here
    # > would load agentshim for every vs_agent.api user; routing through
    # > provider_profiles' lazy import would need a second lazy import of the same
    # > library there instead of one here.
    from vs_agent import shim_translation  # noqa: PLC0415

    return shim_translation.plan_auth(
        provider_profiles.provider_profile(provider), policy, env=env, home=home
    )


@dataclass(frozen=True)
class HostAuthUnplannable:
    """agentshim cannot reproduce how *provider*'s CLI logs in on this host.

    ``detail`` is agentshim's reason; it names variables and files, never a
    value. The container then gets the host's auth state as it is.
    """

    provider: str
    detail: str

    def describe(self) -> str:
        """One log line saying the host's auth state is forwarded, and why."""
        return (
            f"{self.provider} gets the host's auth variables and files as they are, "
            f"because its login cannot be planned: {self.detail}"
        )


def plan_host_cli_auth(
    provider: str, *, env: Mapping[str, str], home: Path
) -> CliAuthPlan | HostAuthUnplannable:
    """Return the plan that logs *provider*'s CLI in the way it would on this host.

    A run without ``[agent.auth]`` lets the CLI log in the way the launching
    shell configured it. agentshim knows how the CLI chooses its credential
    and endpoint, so it plans exactly what a confined copy needs to do the
    same, and the caller delivers that plan and nothing else of the host's
    auth state. When agentshim cannot reproduce the host's login, the result
    says why.
    """
    # lint-waiver: LW-994812 [PLC0415]; load agentshim only when a run plans its auth.
    # > Same constraint as plan_cli_auth above: vs_agent.api must import without
    # > agentshim and shim_translation imports it at module scope. A shared loader
    # > returning the module would erase its types at both call sites.
    from vs_agent import shim_translation  # noqa: PLC0415

    return shim_translation.plan_host_auth(
        provider_profiles.provider_profile(provider), env=env, home=home
    )
