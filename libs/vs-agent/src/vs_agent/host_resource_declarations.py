"""Default host resource declarations for local coding-agent CLIs.

This is the policy layer: it contains the actual list of resources agents need,
expressed only through the public :mod:`vs_sandbox.host_resources` SDK. It does not
know whether a consumer uses bubblewrap, Seatbelt, bind mounts, or another
resource-import mechanism.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vs_agent import provider_profiles
from vs_sandbox.api import (
    HostResource,
    HostResourceAccess,
    HostResourceContext,
    HostResourceDeclarer,
    declare_resources,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agentshim import ProviderProfile

ALLOW_ENV = "VIBESYS_AGENT_SANDBOX_ALLOW"


def _install_root(real_path: Path) -> Path:
    """Return the subtree needed by an installed agent executable."""
    parts = real_path.parts
    if "node_modules" in parts:
        idx = parts.index("node_modules")
        if idx > 0:
            return Path(*parts[:idx])
    return real_path.parent


def _resources(
    paths: Iterable[Path],
    *,
    access: HostResourceAccess = HostResourceAccess.READ_ONLY,
    purpose: str,
) -> tuple[HostResource, ...]:
    return tuple(HostResource(path, access, purpose) for path in paths)


def _interpreter_alias_roots() -> set[Path]:
    """Symlinked directories the interpreter is reached through.

    A virtualenv's ``bin/python`` often reaches its base install through an
    alias directory (uv keeps ``cpython-3.14`` pointing at ``cpython-3.14.7``).
    ``sys.base_prefix`` is already symlink-resolved, so importing it alone
    leaves the alias dangling inside the sandbox and every ``sys.executable``
    exec fails with ENOENT: the agent then loses its stdio MCP servers.
    Importing the alias directory itself binds the real install under the name
    the interpreter actually walks.
    """
    roots: set[Path] = set()
    current = Path(sys.executable)
    seen: set[Path] = set()
    while current.is_symlink() and current not in seen:
        seen.add(current)
        target = current.readlink()
        current = target if target.is_absolute() else current.parent / target
        roots.update(parent for parent in current.parents if parent.is_symlink())
    return roots


def _python_runtime(ctx: HostResourceContext) -> Iterable[HostResource]:
    del ctx
    return _resources(
        (Path(sys.base_prefix), Path(sys.prefix), *sorted(_interpreter_alias_roots())),
        purpose="Python runtime",
    )


def _path_toolchain(ctx: HostResourceContext) -> Iterable[HostResource]:
    paths = (
        Path(entry).expanduser() for entry in ctx.env.get("PATH", "").split(os.pathsep) if entry
    )
    return _resources(paths, purpose="launcher PATH toolchain")


def declare_rust_toolchain_resources(
    ctx: HostResourceContext,
) -> Iterable[HostResource]:
    """Declare the host paths needed to run an installed Rust toolchain."""
    home = ctx.env.get("HOME")
    if not home:
        return ()
    home_path = Path(home)
    cargo_home = Path(ctx.env.get("CARGO_HOME", home_path / ".cargo")).expanduser()
    rustup_home = Path(ctx.env.get("RUSTUP_HOME", home_path / ".rustup")).expanduser()
    return _resources(
        (cargo_home / "bin", cargo_home / "env", rustup_home),
        purpose="Rust toolchain",
    )


def _shell_setup(ctx: HostResourceContext) -> Iterable[HostResource]:
    home = ctx.env.get("HOME")
    if not home:
        return ()
    base = Path(home)
    return _resources(
        (
            base / ".bash_profile",
            base / ".bash_login",
            base / ".profile",
            base / ".bashrc",
        ),
        purpose="shell setup",
    )


def _agent_executable_runtime(ctx: HostResourceContext) -> Iterable[HostResource]:
    paths: list[Path] = []
    if ctx.binary_path:
        real_binary = Path(ctx.binary_path).resolve()
        paths.extend((_install_root(real_binary), real_binary.parent))

    node = shutil.which("node", path=ctx.env.get("PATH"))
    if node:
        real_node = Path(node).resolve()
        paths.extend((real_node.parent, real_node.parent.parent))

    return _resources(paths, purpose="agent runtime")


def _narrowed_writable_leaves(profile: ProviderProfile) -> dict[str, tuple[str, ...]]:
    """Return, per state directory, the leaves a provider is granted instead of the whole directory.

    A provider that declares ``ProviderProfile.resume_state_paths`` is known to
    run from its authentication files plus that conversation history, so its
    state directory is not granted whole: a Codex checkout may itself live under
    the CLI's own state root (``$CODEX_HOME/worktrees`` by default), and a whole
    grant would expose sibling tasks to the agent. A provider that declares none
    keeps the whole-directory grant. The outer key is the default state
    directory, regardless of where a state-root environment variable relocates
    it at runtime; :func:`_state_root` resolves the actual root.

    Authentication leaves come from ``ProviderProfile.auth_files`` and are
    mounted read-write: a CLI that refreshes its OAuth login writes the new
    tokens back, and when the provider rotates its refresh token on use (Codex
    does) a refresh that cannot be saved leaves the stored login holding a token
    the server already rejects, logging the operator out. The history leaves must
    persist too: without them the rollout a turn writes lands in the sandbox's
    ephemeral view of the state root and is gone by the next turn, so a resume
    reports no rollout for the thread, the session restarts the conversation, and
    a confined run silently loses continuity it was told it had.
    """
    narrowed: dict[str, tuple[str, ...]] = {}
    for state_dir in profile.state_dirs:
        leaves = tuple(
            path.removeprefix(f"{state_dir}/")
            for path in profile.resume_state_paths
            if path.startswith(f"{state_dir}/")
        )
        if leaves:
            narrowed[state_dir] = leaves
    return narrowed


def _state_root(
    state_dir: str, *, home: Path, ctx: HostResourceContext, profile: ProviderProfile
) -> Path:
    """Return where *state_dir* actually lives, honoring the CLI's own relocation variable.

    ``ProviderProfile.state_root_env`` (agentshim 0.6.1+) names the
    environment variable a CLI documents for relocating its primary state
    directory, ``state_dirs[0]`` (``CLAUDE_CONFIG_DIR`` for Claude,
    ``CODEX_HOME`` for Codex; ``None`` for a provider that documents none).
    Only that first directory can move this way, and only when the run
    environment actually sets the variable; every other state directory, and
    every provider with no such variable, stays under ``home``.
    """
    if (
        profile.state_root_env
        and state_dir == profile.state_dirs[0]
        and profile.state_root_env in ctx.env
    ):
        return Path(ctx.env[profile.state_root_env]).expanduser()
    return home / state_dir


def declare_provider_state_resources(
    env: Mapping[str, str], *, profile: ProviderProfile
) -> tuple[HostResource, ...]:
    """Declare isolated host state for one AgentShim provider profile.

    Profiles own provider facts such as authentication files and state roots.
    VibeSys applies access policy: authentication needed inside a narrowed root
    and explicitly persistent session state are writable, nothing else in that
    root is visible, and roots without a narrowing policy retain their
    existing writable grant.
    """
    home = env.get("HOME")
    if not home:
        return ()
    ctx = HostResourceContext(env=env, provider=profile.name)

    state_dirs = list(profile.state_dirs)
    if sys.platform == "darwin":
        state_dirs.extend(profile.darwin_state_dirs)

    resources: list[HostResource] = []
    narrowed = _narrowed_writable_leaves(profile)
    for state_dir in state_dirs:
        root = _state_root(state_dir, home=Path(home), ctx=ctx, profile=profile)
        writable_leaves = narrowed.get(state_dir)
        if writable_leaves is None:
            resources.append(
                HostResource(
                    root,
                    HostResourceAccess.READ_WRITE,
                    f"{profile.name} agent state",
                )
            )
            continue

        state_path = Path(state_dir)
        for auth_file in profile.auth_files:
            try:
                relative_auth = Path(auth_file).relative_to(state_path)
            except ValueError:
                continue
            resources.append(
                HostResource(
                    root / relative_auth,
                    HostResourceAccess.READ_WRITE,
                    f"{profile.name} agent authentication",
                )
            )
        resources.extend(
            HostResource(
                root / leaf,
                HostResourceAccess.READ_WRITE,
                f"{profile.name} agent state",
            )
            for leaf in writable_leaves
        )
    return tuple(resources)


def prepare_provider_state(env: Mapping[str, str], *, profile: ProviderProfile) -> None:
    """Create the persistent state leaves :func:`declare_provider_state_resources` grants.

    A sandbox mounts only paths that exist, so a leaf missing from a fresh
    state root (a run's dedicated Codex home has no ``sessions`` yet) would
    land in the sandbox's ephemeral view and vanish after the turn.
    """
    home = env.get("HOME")
    if not home:
        return
    ctx = HostResourceContext(env=env, provider=profile.name)
    for state_dir, leaves in _narrowed_writable_leaves(profile).items():
        root = _state_root(state_dir, home=Path(home), ctx=ctx, profile=profile)
        for leaf in leaves:
            (root / leaf).mkdir(parents=True, exist_ok=True)


def _provider_state(ctx: HostResourceContext) -> Iterable[HostResource]:
    """Look up and declare the selected provider's state."""
    if not ctx.provider:
        return ()
    # An unregistered name raises rather than producing an apparently logged
    # out sandbox. AgentShim's error names the registered alternatives.
    profile = provider_profiles.provider_profile(ctx.provider)
    return declare_provider_state_resources(ctx.env, profile=profile)


def task_agent_host_resources(
    *,
    cli_sandboxed: bool,
    evaluator_package_root: Path | None,
    evaluator_tool_roots: tuple[Path, ...] = (),
) -> tuple[HostResource, ...]:
    """Declare the extra host resources a repository-native task's agent needs.

    A packaged benchmark command may name ``${PACKAGE_ROOT}`` and preinstalled
    evaluator tools. Those resources live outside the workspace, so without
    importing them the command dies on a missing directory and the Profiler
    returns no evidence at all. Imports are read-only because evaluator
    packages and selected content-addressed tool installations are
    integrity-checked input no role may edit. The writable tool-cache parent
    remains operator-only.

    Container backends run the agent inside their own image and own resource
    exposure themselves, so a sandboxed run declares nothing here.
    """
    if cli_sandboxed:
        return ()
    resources: tuple[HostResource, ...] = ()
    if evaluator_package_root is not None:
        resources = (
            *resources,
            HostResource(evaluator_package_root, HostResourceAccess.READ_ONLY, "evaluator package"),
        )
    return (
        *resources,
        *(
            HostResource(root, HostResourceAccess.READ_ONLY, "evaluator tool")
            for root in evaluator_tool_roots
        ),
    )


def _operator_allowlist(ctx: HostResourceContext) -> Iterable[HostResource]:
    raw = ctx.env.get(ALLOW_ENV, "")
    paths = (Path(path).expanduser() for path in raw.split(os.pathsep) if path.strip())
    return _resources(paths, purpose=f"{ALLOW_ENV} entry")


DEFAULT_AGENT_HOST_RESOURCE_DECLARERS: tuple[HostResourceDeclarer, ...] = (
    _python_runtime,
    _path_toolchain,
    declare_rust_toolchain_resources,
    _shell_setup,
    _agent_executable_runtime,
    _provider_state,
    _operator_allowlist,
)


def declare_agent_host_resources(
    env: Mapping[str, str],
    *,
    binary_path: str | None,
    provider: str,
    additional: Iterable[HostResource] = (),
) -> tuple[HostResource, ...]:
    """Declare the complete local resource set for one CLI provider."""
    return declare_resources(
        HostResourceContext(env=env, binary_path=binary_path, provider=provider),
        DEFAULT_AGENT_HOST_RESOURCE_DECLARERS,
        additional=additional,
    )


#: The declarers that describe the host's toolchains rather than one agent
#: CLI: no provider binary, state, or credentials.
COMMAND_HOST_RESOURCE_DECLARERS: tuple[HostResourceDeclarer, ...] = (
    _python_runtime,
    _path_toolchain,
    declare_rust_toolchain_resources,
    _shell_setup,
    _operator_allowlist,
)


def declare_command_host_resources(
    env: Mapping[str, str],
    *,
    additional: Iterable[HostResource] = (),
) -> tuple[HostResource, ...]:
    """Declare what a confined agent command needs, without any agent CLI's own state.

    This is the agent's resource set minus its CLI binary, provider state, and
    credentials, for confining a command the agent started somewhere else,
    such as in a Slurm job.
    """
    return declare_resources(
        HostResourceContext(env=env),
        COMMAND_HOST_RESOURCE_DECLARERS,
        additional=additional,
    )
