"""VibeSys's own provider decisions.

``agentshim`` (read through :mod:`vs_agent.provider_profiles`) owns what
a provider *is*: its binary, state directories, auth environment variables,
skill-discovery paths, and container install recipe. This module owns what
VibeSys *decides* to do with that: which providers it ships, what a shipped
provider's container environment needs, which CLI version its container image
pins, and which VibeSys behaviors are scoped to a single provider. Which
models a provider runs, and its default model, are agentshim's facts
(``ProviderProfile.models``); this module only restricts them to the shipped
providers.

Before this module existed, several of these decisions were copied by hand
into the modules that needed them (a shipped-provider tuple in the session
launcher, a ``--cli-provider`` choices list in the headless entrypoint, a
container-env table in ``cli_docker``). Every module that would otherwise
repeat one of those decisions as a literal imports it from here instead, so a
new decision (or a change to an existing one) has one place to make it.
"""

from __future__ import annotations

from dataclasses import dataclass

from vs_agent import provider_profiles

SHIPPED_PROVIDERS: tuple[str, ...] = ("claude", "codex", "gemini", "opencode")
"""The CLI providers VibeSys ships.

agentshim also registers ``copilot``, which VibeSys has neither host-resource
declarations nor a container install recipe for, so it is not offered here.
"""

DEFAULT_CLI_PROVIDER = "codex"
"""The CLI provider selected when neither a flag nor config names one."""


# --- Vetted harnesses --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Harness:
    """One CLI provider and one model it runs."""

    provider: str
    model: str


def harnesses() -> tuple[Harness, ...]:
    """The provider and model pairs VibeSys runs agents with, the one list of them.

    Derived at call time from agentshim, which owns which models each provider's
    CLI runs and which is its default (``ProviderProfile.models``, default
    first). VibeSys's policy is only which providers it ships, and which it
    offers first: :data:`DEFAULT_CLI_PROVIDER`, then :data:`SHIPPED_PROVIDERS`
    in order. A run uses only these pairs unless its operator opts out at the
    terminal (``--allow-unvetted-model``). A shipped provider for which
    agentshim names no model (Gemini today) runs only with that opt-out.
    """
    order = (DEFAULT_CLI_PROVIDER, *(p for p in SHIPPED_PROVIDERS if p != DEFAULT_CLI_PROVIDER))
    return tuple(
        Harness(provider, model) for provider in order for model in vetted_models(provider)
    )


ALLOWED_MODELS_SETTING = "allowed_models"
"""The host-settings key that names the models a live run on this host may use.

An empty value, or a missing file, means the host sets no restriction.
"""


class ModelNotAllowedError(ValueError):
    """A live run's model is outside the host's allowed set.

    ``setting`` is the host-settings key, ``role`` the role whose model it is
    (``None`` for the run's own model), ``model`` the refused id, and
    ``allowed`` the set the host named.
    """

    def __init__(self, *, role: str | None, model: str, allowed: frozenset[str]) -> None:
        """Name the setting, the role, the model, and the allowed set."""
        self.setting = ALLOWED_MODELS_SETTING
        self.role = role
        self.model = model
        self.allowed = tuple(sorted(allowed))
        whose = "the run" if role is None else f"role {role}"
        listed = ", ".join(self.allowed) if self.allowed else "none"
        super().__init__(
            f"{self.setting}: model {model!r} for {whose} is outside the allowed set; "
            f"allowed models: {listed}"
        )

    def at_role(self, role: str | None) -> ModelNotAllowedError:
        """This refusal, naming ``role`` when it does not already name one."""
        if role is None or self.role is not None:
            return self
        return ModelNotAllowedError(role=role, model=self.model, allowed=frozenset(self.allowed))


def check_allowed_model(model: str, *, role: str | None, allowed: frozenset[str]) -> None:
    """Raise :class:`ModelNotAllowedError` when ``allowed`` is set and omits ``model``.

    An empty ``allowed`` is no restriction. The check compares model ids only.
    """
    if allowed and model not in allowed:
        raise ModelNotAllowedError(role=role, model=model, allowed=allowed)


class UnvettedHarnessError(ValueError):
    """A provider and model pair outside :func:`harnesses`."""

    def __init__(self, provider: str, model: str | None) -> None:
        """Name the pair and the models vetted for ``provider``; ``model`` None names none."""
        self.provider = provider
        self.model = model
        self.allowed = vetted_models(provider)
        allowed = ", ".join(self.allowed) if self.allowed else "none"
        chosen = "no model is named" if model is None else f"model {model!r} is not vetted"
        super().__init__(f"{chosen} for provider {provider!r}; vetted models: {allowed}")


def vetted_models(provider: str) -> tuple[str, ...]:
    """The models vetted for ``provider``, its default first; none for a provider VibeSys does not ship.

    agentshim's ``ProviderProfile.models`` for a shipped provider, read at call time.
    """
    if provider not in SHIPPED_PROVIDERS:
        return ()
    return provider_profiles.provider_profile(provider).models


def default_model(provider: str) -> str | None:
    """The model a run on ``provider`` uses when nothing names one; ``None`` if none is vetted."""
    models = vetted_models(provider)
    return models[0] if models else None


def check_harness(provider: str, model: str) -> None:
    """Raise :class:`UnvettedHarnessError` unless ``(provider, model)`` is vetted."""
    if model not in vetted_models(provider):
        raise UnvettedHarnessError(provider, model)


# --- Docker container environment -------------------------------------------

_COMMON_DOCKER_ENV: dict[str, str] = {"PYTHONPATH": "/opt/vibesys"}
"""Every shipped provider's container CLI needs this so agent-launched product
tool modules can import VibeSys from the bind-mounted project root (added in
``DockerSandbox.start`` for all four CLI providers).

This is the whole of ``DOCKER_PROVIDER_ENV``'s per-provider contribution: the
rest of a containerized run's extra environment (``UV_CACHE_DIR``, and
``VIBESYS_GIT_HISTORY`` when the run carries git history) is set once, for
every provider alike, by ``run_environment`` rather than duplicated per
provider here."""

# Claude Code used to refuse ``--dangerously-skip-permissions`` when running
# as root unless ``IS_SANDBOX=1`` was set, back when VibeSys ran every
# container provider as root. The agent image now runs the CLI as the non-root
# ``agent`` user, so that requirement no longer applies and
# ``ProviderProfile.container_env`` (agentshim 0.6.1's home for exactly this
# kind of root-only escape hatch) is deliberately not merged in here.
DOCKER_PROVIDER_ENV: dict[str, dict[str, str]] = {
    provider: dict(_COMMON_DOCKER_ENV) for provider in SHIPPED_PROVIDERS
}
"""Per-provider environment variables to set inside the container.

Used as the canonical "supported in Docker" registry: providers absent
from this dict are rejected up front in ``build_agent_client``.
"""

CODEX_DOCKER_CLI_VERSION = "0.160.0"
"""Keep the editor container aligned with the verified host CLI feature set.

The models VibeSys runs through Codex, and their reasoning levels, need a newer
CLI than the old 0.125 pin.
It must also be at least the CLI version agentshim generated its ``codex
app-server`` protocol from: an older server omits fields that protocol requires,
and a container turn then fails to decode ``thread/start``.
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
    at call time (through :mod:`vs_agent.provider_profiles`) so a
    library upgrade that adds or moves a provider's skill directory changes
    VibeSys behavior without an edit here.
    """
    seen: dict[str, None] = {}
    for provider in SHIPPED_PROVIDERS:
        for skill_dir in provider_profiles.provider_profile(provider).skill_dirs:
            seen.setdefault(skill_dir, None)
    seen.setdefault(_CURSOR_SKILL_DIR, None)
    return tuple(seen)


def cli_mcp_config_files() -> tuple[str, ...]:
    """Return every workspace file a shipped provider writes its MCP servers into.

    Some CLIs read MCP servers only from a file in the working directory, so
    the session writes one there for each turn. It carries per-turn capability
    tokens and is never candidate content. Resolved at call time from each
    provider's ``ProviderProfile.mcp_config_file``.
    """
    seen: dict[str, None] = {}
    for provider in SHIPPED_PROVIDERS:
        config_file = provider_profiles.provider_profile(provider).mcp_config_file
        if config_file is not None:
            seen.setdefault(config_file, None)
    return tuple(seen)


# --- Agent image ------------------------------------------------------------
#
# Versions baked into the agent image's Dev Container Features as options
# (see ``vs_agent.api.images.devcontainer_config``). Pinned rather than left
# to float so the container CLI matches the feature set VibeSys prompts were
# validated against, and so a rebuild with unchanged pins resolves to the same
# image from Docker's layer cache.

NODE_VERSION = "24.21.0"
"""Current Node.js LTS ("Krypton"), matching what the shipped CLIs are
verified against upstream."""

CLI_VERSIONS: dict[str, str] = {
    "claude": "2.1.304",
    "codex": CODEX_DOCKER_CLI_VERSION,
    "gemini": "0.59.0",
    "opencode": "1.18.30",
}
"""Container CLI version per shipped provider (``npm view <pkg> version`` at
the time each was pinned, except Codex).

Codex draws from :data:`CODEX_DOCKER_CLI_VERSION` instead of repeating its own
literal: that constant is this module's own single source for the pin, used
both here and (formerly) by ``cli_docker``'s now-removed per-run container
install for Modal and SkyPilot.
"""

CLAUDE_CLI_MIN_FOR_GENERATION: dict[str, str] = {
    "5.5": "2.1.293",
}
"""The oldest Claude Code release that recognizes every model of a Claude
generation, by agentshim's ``generation_id``.

A Claude Code older than its model does not know the model's id: it prints
``unrecognized_model`` on every session and prices the model at a fallback rate
(2.1.268 billed a 5.5 model about 2.6 times too high, before agentshim priced
turns itself). The pin in :data:`CLI_VERSIONS` and the models agentshim lists
move separately, so ``test_claude_cli_pin`` fails when the newest generation
agentshim lists has no entry here, or the pin is older than an entry.
Read an entry off the binary: the first release whose ``claude`` executable
contains every model id of the generation (``npm pack
@anthropic-ai/claude-code-<platform>@<version>``, then ``grep -a`` each id).
For 5.5 the Opus model is in 2.1.281, Sonnet in 2.1.284 and Haiku, the last, in
2.1.293.
"""

# The toolchain pins (Rust, Go, and the container-runtime tools) live in
# ``vs_sandbox.toolchain_features``, the catalog that owns those Features;
# ``vs_agent.api`` re-exports them so callers keep one import path.
