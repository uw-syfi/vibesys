"""Which launcher environment variables an agent session inherits.

A run's behavior must not depend on who launches it or from where. The
launcher's environment carries the operator's credentials for unrelated
services, the parent agent's own session variables when VibeSys is started
from inside another coding agent, terminal-multiplexer and D-Bus sockets, and
virtual-environment settings. None of that is the agent's business, so a
session inherits only an explicit allowlist: what any process needs to run
(``PATH``, ``HOME``, locale, terminal), network reachability (proxy and CA
bundle variables), toolchain locations the host sandbox grants, VibeSys's own
sandbox controls, and the selected provider's credential and state-root
variables as agentshim declares them. The run's own variables are layered on
top, and an operator adds names through ``[agent] env_passthrough``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vs_agent.host_resource_declarations import ALLOW_ENV
from vs_sandbox.api import SANDBOX_DISABLE_ENV

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agentshim import ProviderProfile

#: Variables every session inherits from the launcher when they are set.
BASE_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Process basics.
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TZ",
        "TERM",
        # Locale; every ``LC_*`` category is admitted by prefix as well.
        "LANG",
        "LANGUAGE",
        # Network reachability.
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        # Toolchain locations the host sandbox declares from this environment.
        "CARGO_HOME",
        "RUSTUP_HOME",
        # GPU visibility pins. An operator who launches with one set has chosen
        # the devices the run may use; when set, the device lease adds nothing
        # of its own, so dropping the pin would show agents excluded GPUs.
        "CUDA_VISIBLE_DEVICES",
        "HIP_VISIBLE_DEVICES",
        "ROCR_VISIBLE_DEVICES",
        # The container runtime an agent may drive (see container_runtime_resources).
        "DOCKER_HOST",
        # VibeSys's own sandbox controls, read from the session environment.
        ALLOW_ENV,
        SANDBOX_DISABLE_ENV,
    }
)

#: Prefixes admitted wholesale: the locale categories.
BASE_ENV_PREFIXES: tuple[str, ...] = ("LC_",)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate_env_names(names: Iterable[str]) -> tuple[str, ...]:
    """Return *names* if each is a POSIX environment variable name.

    Raises:
        ValueError: naming the first entry that is not one.
    """
    validated = tuple(names)
    for name in validated:
        if not _ENV_NAME.fullmatch(name):
            message = f"env_passthrough entry {name!r} is not an environment variable name"
            raise ValueError(message)
    return validated


def session_env_allowlist(
    profile: ProviderProfile, passthrough: Iterable[str] = ()
) -> frozenset[str]:
    """Names a session on *profile* may inherit, besides the ``LC_*`` prefix.

    The provider's credential variables and the variable that relocates its
    state root are agentshim's facts; the rest is VibeSys policy.
    """
    provider_names = set(profile.auth_env_vars)
    if profile.state_root_env:
        provider_names.add(profile.state_root_env)
    return BASE_ENV_ALLOWLIST | provider_names | set(validate_env_names(passthrough))


def dropped_launcher_names(
    launcher: Mapping[str, str],
    *,
    profile: ProviderProfile,
    passthrough: Iterable[str] = (),
) -> tuple[str, ...]:
    """Sorted names set in *launcher* that a session does not inherit.

    Only names, never values, so the result is safe to log.
    """
    allowed = session_env_allowlist(profile, passthrough)
    return tuple(
        sorted(
            name
            for name in launcher
            if name not in allowed and not name.startswith(BASE_ENV_PREFIXES)
        )
    )


def session_environment(
    launcher: Mapping[str, str],
    *,
    profile: ProviderProfile,
    passthrough: Iterable[str] = (),
    run: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment one session runs with.

    The allowlisted subset of *launcher*, overlaid with the run's own
    variables *run*, which are always kept.
    """
    allowed = session_env_allowlist(profile, passthrough)
    inherited = {
        name: value
        for name, value in launcher.items()
        if name in allowed or name.startswith(BASE_ENV_PREFIXES)
    }
    return {**inherited, **(run or {})}
