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

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentshim import ProviderProfile


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
    import agentshim  # noqa: PLC0415  # lint-waiver: LW-010181 [PLC0415]; Keep agentshim lazy in provider_profile so unused providers and import cycles stay unloaded.

    return agentshim.get_provider(provider).profile


def state_dir_path(
    profile: ProviderProfile, state_dir: str, *, home: Path, env: Mapping[str, str]
) -> Path:
    """Return where *state_dir* lives, honoring the CLI's own relocation variable.

    ``profile.state_root_env`` (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``) relocates
    only ``state_dirs[0]``, and only when *env* sets it; everything else stays
    under *home*.
    """
    if (
        profile.state_root_env
        and state_dir == profile.state_dirs[0]
        and profile.state_root_env in env
    ):
        return Path(env[profile.state_root_env]).expanduser()
    return home / state_dir


def credential_path(profile: ProviderProfile, *, home: Path, env: Mapping[str, str]) -> Path | None:
    """Return the primary credential file, ``auth_files[0]``, or ``None`` when there is none."""
    if not profile.auth_files:
        return None
    credential = Path(profile.auth_files[0])
    for state_dir in profile.state_dirs:
        if credential.is_relative_to(state_dir):
            root = state_dir_path(profile, state_dir, home=home, env=env)
            return root / credential.relative_to(state_dir)
    return home / credential
