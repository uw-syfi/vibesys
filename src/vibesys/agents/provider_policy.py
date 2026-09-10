"""VibeSys's own provider decisions.

``agentshim`` (read through :mod:`vibesys.agents.provider_profiles`) owns what
a provider *is*: its binary, state directories, auth environment variables,
skill-discovery paths, and container install recipe. This module owns what
VibeSys *decides* to do with that: which providers it ships, what a shipped
provider's container environment needs, which CLI version its container image
pins, and which VibeSys behaviors are scoped to a single provider.

Before this module existed, several of these decisions were copied by hand
into the modules that needed them (a shipped-provider tuple in the AgentShim
driver, a ``--cli-provider`` choices list in the headless entrypoint, a
container-env table in ``cli_docker``). Every module that would otherwise
repeat one of those decisions as a literal imports it from here instead, so a
new decision (or a change to an existing one) has one place to make it.
"""

from __future__ import annotations

from vibesys.agents import provider_profiles

SHIPPED_PROVIDERS: tuple[str, ...] = ("claude", "codex", "gemini", "opencode")
"""The CLI providers VibeSys ships.

agentshim also registers ``copilot``, which VibeSys has neither host-resource
declarations nor a container install recipe for, so it is not offered here.
"""

DEFAULT_CLI_PROVIDER = "codex"
"""The CLI provider selected when neither a flag nor config names one."""


def is_codex(provider: str | None) -> bool:
    """Whether *provider* is Codex (``None``, an unset provider, is not).

    A few VibeSys behaviors are deliberately scoped to Codex alone: its
    session turn/token/duration budget (``AgentShimSession`` retires the
    thread rather than letting it run unbounded) and the containerized
    rollout watchdog that compensates for a resumed ``codex exec --json``
    that finishes its work but never exits. Naming the check here means a
    driver branches on a documented VibeSys decision instead of repeating the
    provider's literal name at each call site.
    """
    return provider == "codex"


# --- Docker container environment -------------------------------------------

_COMMON_DOCKER_ENV: dict[str, str] = {"PYTHONPATH": "/opt/vibesys"}
"""Every shipped provider's container CLI needs this so it can spawn
``python -m vs_issue_board.mcp`` against the bind-mounted project root (added
in ``DockerSandbox.start`` for all four CLI providers). Without it the MCP
server module would not be importable inside the container."""

# Claude Code refuses ``--dangerously-skip-permissions`` when running as root
# unless ``IS_SANDBOX=1`` is set, and VibeSys runs every container provider as
# root (the default) to avoid uv/pip permission errors when the agent installs
# packages. agentshim 0.6.1 declares this on ``ProviderProfile.container_env``
# for every provider that needs container-only environment beyond auth
# (Claude Code today), so VibeSys folds each shipped profile's answer into the
# common table rather than pinning Claude's requirement by hand.
DOCKER_PROVIDER_ENV: dict[str, dict[str, str]] = {
    provider: {
        **_COMMON_DOCKER_ENV,
        **provider_profiles.provider_profile(provider).container_env,
    }
    for provider in SHIPPED_PROVIDERS
}
"""Per-provider environment variables to set inside the container.

Used as the canonical "supported with --docker" registry: providers absent
from this dict are rejected up front in ``build_agent_client``.
"""

CODEX_DOCKER_CLI_VERSION = "0.144.4"
"""Keep the editor container aligned with the verified host CLI feature set.

Luna and its Max reasoning level require a newer CLI than the old 0.125 pin.
"""


# --- Skill discovery ---------------------------------------------------------

_CURSOR_SKILL_DIR = ".cursor/skills"
"""VibeSys-only addition: agentshim registers no Cursor provider (Cursor's
agent mode is not a CLI VibeSys drives), but Cursor discovers skills from the
same flat ``<name>/SKILL.md`` convention the shipped CLIs use, so VibeSys
mirrors skills there too."""


def cli_skill_dirs() -> tuple[str, ...]:
    """Return every CLI skill-discovery path VibeSys mirrors skills into.

    The union of each shipped provider's ``ProviderProfile.skill_dirs``, in
    :data:`SHIPPED_PROVIDERS` order, plus :data:`_CURSOR_SKILL_DIR`. Resolved
    at call time (through :mod:`vibesys.agents.provider_profiles`) so a
    library upgrade that adds or moves a provider's skill directory changes
    VibeSys behavior without an edit here.
    """
    seen: dict[str, None] = {}
    for provider in SHIPPED_PROVIDERS:
        for skill_dir in provider_profiles.provider_profile(provider).skill_dirs:
            seen.setdefault(skill_dir, None)
    seen.setdefault(_CURSOR_SKILL_DIR, None)
    return tuple(seen)
