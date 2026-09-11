"""Docker configuration for CLI agent providers.

Provider facts come from ``agentshim``'s ``ProviderProfile`` at call time:
state directories, auth environment variables, which files carry credentials,
and the CLI's own container install recipe. What stays here is VibeSys policy
that is not itself a provider *decision*: the toolchain a candidate
repository needs, and the overrides VibeSys applies to a library recipe. The
container environment table and the Codex CLI version pin are provider
decisions and live in :mod:`vibesys.agents.provider_policy`; this module
imports them.

The plain Docker path (``vibesys.sandbox.run_environment.DockerEnvironment``)
now starts from a prebuilt agent image with every shipped CLI, toolchain, and
the non-root ``agent`` user already installed, so it needs none of the
shell-recipe machinery below: it copies auth files itself at container start
(see :func:`auth_copy_paths`) instead of running :func:`auth_copy_commands`
through ``extra_init_commands``. ``docker_init_commands`` and its supporting
override tables stay here for Modal and SkyPilot, which still install
per-run through ``extra_init_commands`` until their own image work
(#676, #679) lands; delete them once those land.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from vibesys.agents import provider_profiles
from vibesys.agents.provider_policy import (
    CODEX_DOCKER_CLI_VERSION,
    DOCKER_PROVIDER_ENV,
)
from vs_sandbox import AGENT_HOME

# Re-exported for existing importers (``vibesys.agents.factory``,
# ``vibesys.sandbox.run_environment``, and this module's own tests reach them
# as ``cli_docker.DOCKER_PROVIDER_ENV`` / ``cli_docker.CODEX_DOCKER_CLI_VERSION``);
# the values themselves live in ``provider_policy`` now.
__all__ = ["CODEX_DOCKER_CLI_VERSION", "DOCKER_PROVIDER_ENV"]

# Native implementations are valid candidate designs across domains, so the
# editor container must be able to build and test them before paid target work.
# Pin the toolchain for reproducible local checks instead of letting each agent
# independently bootstrap an arbitrary Rust release.
RUST_DOCKER_TOOLCHAIN_VERSION = "1.92.0"


# Bash one-liners run inside the container at start() time, per provider.
# Each list runs sequentially; a non-zero exit at any step raises RuntimeError.
#
# Every provider gets the python3 + ``mcp`` install at the end so that the
# in-container CLI can spawn ``python -m vs_issue_board.mcp``
# as a stdio MCP child. agentshim installs the per-provider MCP config for the
# turn (a config file for claude, gemini and opencode; ``--config`` flags for
# codex) and removes it afterwards. The default base image
# ``nvcr.io/nvidia/pytorch:25.04-py3`` already ships python3 + pip + a
# compatible ``mcp`` install, so this is a defensive no-op for the default
# image but keeps the install resilient on alternative images.
# Retry apt-get up to 5x with backoff — Ubuntu archive mirrors regularly
# return transient "connection timed out" / "mirror sync in progress"
# errors that fail a single-shot `apt-get update`.
def _apt_install(pkgs: str, check_bin: str | None = None) -> str:
    bin_ = check_bin or pkgs.split(maxsplit=1)[0]
    return (
        f"command -v {bin_} >/dev/null || "
        "{ for i in 1 2 3 4 5; do "
        f"  apt-get update -qq && apt-get install -y -qq {pkgs} && break || "
        '  (echo "apt retry $i..." >&2; sleep $((i*5))); '
        "done; "
        f"command -v {bin_} >/dev/null; }}"
    )


_RUST_TOOLCHAIN_INSTALL = (
    "command -v cargo >/dev/null || { set -e; "
    "curl -fsSL --retry 5 --retry-delay 5 -o /tmp/rustup-init.sh "
    "https://sh.rustup.rs && "
    "sh /tmp/rustup-init.sh -y --profile minimal "
    f"--default-toolchain {RUST_DOCKER_TOOLCHAIN_VERSION} "
    "--component rustfmt --component clippy && "
    "ln -sf /root/.cargo/bin/* /usr/local/bin/ && "
    "rm -f /tmp/rustup-init.sh; }"
)


_COMMON_DOCKER_TOOLING_INSTALL = [
    _apt_install("curl ca-certificates", check_bin="curl"),
    _RUST_TOOLCHAIN_INSTALL,
    _apt_install("ripgrep", check_bin="rg"),
    _apt_install("python3 python3-pip", check_bin="pip3"),
    "PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --quiet 'mcp>=1.0,<2'",
]


# Steps a provider's own recipe may contain that VibeSys replaces before
# running them in an editor container.
#
# ``apt-get update``: a library recipe bootstraps curl with a single-shot
# ``apt-get``. Ubuntu archive mirrors reachable from our hosts return transient
# fetch failures often enough that a single-shot update fails container start
# outright, so the exact bootstrap step is swapped for the retrying, idempotent
# VibeSys equivalent. An exact-string key on purpose: when the library changes
# its recipe the override stops matching, and the test that pins this pairing
# fails rather than silently hardening nothing.
_PROFILE_STEP_OVERRIDES: dict[str, str] = {
    "apt-get update && apt-get install -y --no-install-recommends curl ca-certificates": (
        _apt_install("curl ca-certificates", check_bin="curl")
    ),
}

# Node needs no entry. VibeSys used to install it from the nodejs.org tarball
# because apt-get against archive.ubuntu.com is unreliable from several of our
# hosts, and the codex and gemini profiles now fetch the same tarball behind
# the same `command -v node` guard, so their recipes stand as written.
#
# One rough edge is left as the library ships it: the codex recipe fetches that
# tarball with curl but, unlike gemini's, carries no curl bootstrap of its own,
# so on an image without curl it would rely on the bootstrap in the common
# tooling below, which runs after the recipe. The default editor image
# (`nvcr.io/nvidia/pytorch:25.04-py3`) ships curl, and the pre-profile VibeSys
# table had the same ordering, so this is a known hazard on an alternative
# image rather than a regression.

# ``npm install -g [--flags] @openai/codex`` with no ``@<version>`` suffix.
_UNPINNED_CODEX_NPM_INSTALL = re.compile(r"(npm install -g\b[^&|;]*?)@openai/codex(?!@)(\s|$)")


def _pin_codex_cli(step: str) -> str:
    """Pin an unpinned ``@openai/codex`` install to the verified CLI version.

    VibeSys pins because the container CLI must match the host feature set the
    prompts were validated against; a floating ``@latest`` silently changes the
    editor mid-campaign. A recipe that already names a version is left alone:
    the library is then the one making the call, and disagreeing silently would
    be worse than either choice.

    ``--include=optional`` is added with the pin because newer codex packages
    ship the Linux-x64 native binary as an optional dependency that
    ``npm install -g`` skips on some npm configurations.
    """

    def pinned(match: re.Match[str]) -> str:
        prefix = match.group(1)
        flag = "" if "--include=optional" in prefix else "--include=optional "
        return f"{prefix}{flag}@openai/codex@{CODEX_DOCKER_CLI_VERSION}{match.group(2)}"

    return _UNPINNED_CODEX_NPM_INSTALL.sub(pinned, step, count=1)


def docker_init_commands(provider: str) -> list[str]:
    """Return the container init commands for *provider*.

    The provider's own install recipe comes from its agentshim profile, with
    the VibeSys overrides above applied, and is followed by the toolchain every
    VibeSys editor container needs regardless of provider.

    Raises:
        ValueError: if agentshim does not register *provider*.
    """
    recipe = [
        _pin_codex_cli(_PROFILE_STEP_OVERRIDES.get(step, step))
        for step in provider_profiles.provider_profile(provider).container_install
    ]
    # A recipe whose bootstrap was replaced by a VibeSys step already contains
    # that step; running it twice is harmless but hides what the recipe does.
    tail = [step for step in _COMMON_DOCKER_TOOLING_INSTALL if step not in recipe]
    return [*recipe, *tail]


@dataclass(frozen=True)
class DockerAuthPath:
    """Host provider state and its writable location inside the container."""

    host_path: Path
    container_path: str


def auth_paths(provider: str) -> list[DockerAuthPath]:
    """Return the provider state files to stage into a container, in order.

    Authentication and user configuration are mounted read-only under
    ``/opt/vibesys-auth`` and copied into the container's ephemeral writable
    layer before the CLI starts. Which home-relative paths those are comes
    straight from ``ProviderProfile.auth_files`` (agentshim 0.6.1+): each
    entry is either a state directory's leaf file or a state entry that is
    itself a single file, and is staged at the same relative path under
    the agent image's HOME. A state directory the profile does not list a
    leaf for contributes nothing.

    Raises:
        ValueError: if agentshim does not register *provider*.
    """
    home = Path.home()
    return [
        DockerAuthPath(home / auth_file, f"{AGENT_HOME}/{auth_file}")
        for auth_file in provider_profiles.provider_profile(provider).auth_files
    ]


def auth_env_vars(provider: str) -> tuple[str, ...]:
    """Return the host environment variables that carry *provider* credentials.

    Forwarded into the container alongside the staged files above. A host may
    authenticate a CLI entirely through the environment — an
    ``ANTHROPIC_AUTH_TOKEN`` plus ``ANTHROPIC_BASE_URL`` pointing at a proxy or
    enterprise gateway is a first-class Claude Code auth mechanism, and plain
    API keys are another — in which case no provider state file exists to stage
    and the container CLI would start unauthenticated. Host sandboxes never hit
    this because they inherit the host environment directly.

    The profile lists credential and endpoint variables only. VibeSys owns
    per-role model selection, so a model-selection variable such as
    ``ANTHROPIC_MODEL`` must never appear here: forwarding a host export would
    let it silently override the configured model inside the container.

    Raises:
        ValueError: if agentshim does not register *provider*.
    """
    return provider_profiles.provider_profile(provider).auth_env_vars


def auth_env_passthrough(provider: str) -> dict[str, str]:
    """Return the host auth environment variables *provider* can actually use.

    Unset and blank variables are dropped: an empty host export carries no
    credential and must not shadow staged file authentication or make the
    preflight check believe the container is authenticated.
    """
    values: dict[str, str] = {}
    for name in auth_env_vars(provider):
        value = os.environ.get(name)
        if value and value.strip():
            values[name] = value
    return values


def auth_bind_mounts(provider: str) -> list[tuple[str, str, bool]]:
    """Return read-only staging mounts for existing provider state."""
    out: list[tuple[str, str, bool]] = []
    for index, spec in enumerate(auth_paths(provider)):
        if spec.host_path.exists():
            out.append(
                (
                    str(spec.host_path),
                    f"/opt/vibesys-auth/{index}",
                    True,
                )
            )
    return out


def auth_copy_paths(provider: str) -> list[tuple[str, str]]:
    """Return ``(staged source, agent-home destination)`` pairs to copy at start.

    Used by the plain Docker path, which hands these straight to
    ``DockerSandbox(auth_files=...)`` to copy as a start-time step rather than
    running shell commands through ``extra_init_commands`` (see
    :func:`auth_copy_commands` for the Modal/SkyPilot form of the same list).
    Indexing matches :func:`auth_bind_mounts`: entry *i*'s staging mount is
    ``/opt/vibesys-auth/{i}``.
    """
    return [
        (f"/opt/vibesys-auth/{index}", spec.container_path)
        for index, spec in enumerate(auth_paths(provider))
        if spec.host_path.exists()
    ]


def auth_copy_commands(provider: str) -> list[str]:
    """Return commands that copy staged provider state into writable storage.

    Directories contain runtime state such as sessions and history, so mounting
    them read-only at their final locations can break otherwise valid CLI runs.
    Copying from read-only staging keeps those writes inside the disposable
    container layer.

    Used by Modal and SkyPilot's ``extra_init_commands`` path; the plain
    Docker path uses :func:`auth_copy_paths` instead (see module docstring).
    """
    commands: list[str] = []
    for index, spec in enumerate(auth_paths(provider)):
        if not spec.host_path.exists():
            continue
        source = shlex.quote(f"/opt/vibesys-auth/{index}")
        destination = shlex.quote(spec.container_path)
        parent = shlex.quote(str(Path(spec.container_path).parent))
        if spec.host_path.is_dir():
            commands.append(f"mkdir -p {destination} && cp -a {source}/. {destination}/")
        else:
            commands.append(f"mkdir -p {parent} && cp -a {source} {destination}")
    return commands
