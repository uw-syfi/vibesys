"""Default host resource declarations for local coding-agent CLIs.

This is the policy layer: it contains the actual list of resources agents need,
expressed only through the public :mod:`vs_sandbox.host_resources` SDK. It does not
know whether a consumer uses bubblewrap, Seatbelt, bind mounts, or another
resource-import mechanism.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping  # noqa: TC003  # tracked: #288
from pathlib import Path
from typing import TYPE_CHECKING

import vibesys
from vibesys.agents import provider_profiles
from vs_sandbox import (
    HostResource,
    HostResourceAccess,
    HostResourceContext,
    HostResourceDeclarer,
    declare_resources,
)

if TYPE_CHECKING:
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


def resolve_active_rust_toolchain(
    ctx: HostResourceContext,
    *,
    workspace: Path | None = None,
) -> tuple[Path, Path] | None:
    """Return the active Rust sysroot and target library directory."""
    rustc = shutil.which("rustc", path=ctx.env.get("PATH"))
    if rustc is None:
        return None

    def rustc_print(name: str) -> str:
        result = subprocess.run(  # noqa: S603
            [rustc, "--print", name],
            check=True,
            capture_output=True,
            cwd=workspace,
            env={**ctx.env, "RUSTUP_AUTO_INSTALL": "0"},
            text=True,
            timeout=10,
        )
        return result.stdout.strip()

    try:
        sysroot_text = rustc_print("sysroot")
        target_libdir_text = rustc_print("target-libdir")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if not sysroot_text or not target_libdir_text:  # pragma: no cover - malformed compiler
        return None
    sysroot = Path(sysroot_text).expanduser().resolve()
    target_libdir = Path(target_libdir_text).expanduser().resolve()
    return sysroot, target_libdir


def declare_active_rust_toolchain_resources(
    ctx: HostResourceContext,
    *,
    workspace: Path | None = None,
) -> Iterable[HostResource]:
    """Declare a narrow view of the active Rust compiler and runtime.

    Omnigent may scan read grants for hidden paths. Granting all
    of ``~/.rustup`` is both expensive and likely to exceed its scan cap, so
    VibeSys bypasses the rustup proxy and exposes only the selected toolchain.
    """
    resolved = resolve_active_rust_toolchain(ctx, workspace=workspace)
    if resolved is None:
        return ()
    sysroot, target_libdir = resolved
    del target_libdir
    paths = [sysroot / "bin", sysroot / "lib"]
    if (sysroot / "libexec").is_dir():
        paths.append(sysroot / "libexec")
    return _resources(paths, purpose="active Rust toolchain")


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


def _agent_runtime(ctx: HostResourceContext) -> Iterable[HostResource]:
    paths: list[Path] = []
    if ctx.binary_path:
        real_binary = Path(ctx.binary_path).resolve()
        paths.extend((_install_root(real_binary), real_binary.parent))

    node = shutil.which("node", path=ctx.env.get("PATH"))
    if node:
        real_node = Path(node).resolve()
        paths.extend((real_node.parent, real_node.parent.parent))

    pkg_file = getattr(vibesys, "__file__", None)
    if pkg_file:
        paths.append(Path(pkg_file).resolve().parents[1])

    return _resources(paths, purpose="agent and VibeSys runtime")


#: State directories granted as named leaves rather than whole.
#:
#: This is VibeSys sandbox policy, not a provider fact: a Codex checkout may
#: itself live under the CLI's own state root (``$CODEX_HOME/worktrees`` by
#: default), so granting the directory would expose sibling tasks to the
#: agent, while dropping it makes the CLI appear logged out inside
#: confinement (#185). Bubblewrap creates the ephemeral parent directory
#: these leaf mounts need. Every other state directory is granted whole,
#: because a CLI writes session history and caches there and needs them back
#: on resume.
#:
#: Keyed on the profile's *default* directory name, ``.codex``, regardless of
#: where ``CODEX_HOME`` relocates it at runtime: :func:`_state_root` resolves
#: the actual root, and this table only says which leaves of Codex's state
#: directory are narrowed once that root is known.
#:
#: ``sessions`` is that session history for Codex. Without it the rollout a
#: turn writes lands in the sandbox's ephemeral view of ``$CODEX_HOME`` and is
#: gone by the next turn, so ``codex exec resume`` reports no rollout for the
#: thread, the driver restarts the conversation, and a confined run silently
#: loses continuity it was told it had.
_NARROWED_STATE_DIRS: dict[str, tuple[str, ...]] = {
    ".codex": ("auth.json", "config.toml", "sessions"),
}


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


def _provider_state(ctx: HostResourceContext) -> Iterable[HostResource]:
    """Declare the provider's own writable state, derived from its profile."""
    home = ctx.env.get("HOME")
    if not home or not ctx.provider:
        return ()
    # A name agentshim does not register raises, naming the alternatives, the
    # same way every other profile-derived table answers it. Silently declaring
    # no state instead would confine an agent with no access to its own
    # credentials and let it fail as if it were logged out.
    profile = provider_profiles.provider_profile(ctx.provider)

    state_dirs = list(profile.state_dirs)
    if sys.platform == "darwin":
        state_dirs.extend(profile.darwin_state_dirs)

    paths: list[Path] = []
    for state_dir in state_dirs:
        root = _state_root(state_dir, home=Path(home), ctx=ctx, profile=profile)
        leaves = _NARROWED_STATE_DIRS.get(state_dir)
        if leaves is None:
            paths.append(root)
        else:
            paths.extend(root / leaf for leaf in leaves)

    return _resources(
        paths,
        access=HostResourceAccess.READ_WRITE,
        purpose=f"{ctx.provider} agent state",
    )


#: Conventional host scratch root for a repository-native task. Container
#: workloads bind-mount capture directories from here, and Docker resolves a
#: bind source in the daemon's namespace rather than the agent's, so the path
#: only works when it names the same directory inside and outside confinement.
TASK_SCRATCH_ROOT = Path("/tmp")  # noqa: S108  # tracked: #288


def task_scratch_dir(task_name: str) -> Path:
    """Return the host scratch directory shared with a task's containers."""
    return TASK_SCRATCH_ROOT / f"vibesys-{task_name}"


def container_runtime_resources(env: Mapping[str, str] | None = None) -> tuple[HostResource, ...]:
    """Declare the Docker control socket for tasks that orchestrate containers.

    A microservice benchmark *is* a container topology: without the socket the
    agent cannot build, start, or profile the system it is optimizing, and the
    round-one routing check concludes the candidate does not run. The default
    Linux confinement exposes no ``/var/run``, so the socket has to be imported
    deliberately.

    This is a real widening. Access to the daemon is equivalent to root on the
    host, so it is declared only for the domain that needs it rather than for
    every local agent. Run with ``--docker`` when the workload should be
    confined to a container instead.
    """
    env = env if env is not None else os.environ
    paths = [Path("/var/run/docker.sock")]  # tracked: #288
    host = env.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        paths.append(Path(host.removeprefix("unix://")))
    return _resources(
        paths,
        access=HostResourceAccess.READ_WRITE,
        purpose="Docker control socket",
    )


def task_agent_host_resources(  # noqa: PLR0913
    *,
    container_topology: bool,
    cli_sandboxed: bool,
    task_name: str | None,
    evaluator_package_root: Path | None,
    evaluator_tool_roots: tuple[Path, ...] = (),
    env: Mapping[str, str] | None = None,
) -> tuple[HostResource, ...]:
    """Declare the extra host resources a repository-native task's agent needs.

    Two independent widenings, both host-only. A container topology needs the
    Docker socket and a scratch directory that names the same path inside and
    outside confinement, because Docker resolves a bind-mount source in the
    daemon's namespace rather than the agent's. Separately, a packaged
    benchmark command may name ``${PACKAGE_ROOT}`` and preinstalled evaluator
    tools. Those resources live outside the workspace, so without importing
    them the command dies on a missing directory and the Profiler returns no
    evidence at all. Imports are read-only because evaluator packages and
    selected content-addressed tool installations are integrity-checked input
    no role may edit. The writable tool-cache parent remains operator-only.

    Container backends run the agent inside their own image and own resource
    exposure themselves, so a sandboxed run declares nothing here.
    """
    if cli_sandboxed:
        return ()
    resources: tuple[HostResource, ...] = ()
    if container_topology:
        resources = container_runtime_resources(env)
        if task_name is not None:
            scratch = task_scratch_dir(task_name)
            scratch.mkdir(parents=True, exist_ok=True)
            resources = (
                *resources,
                HostResource(scratch, HostResourceAccess.READ_WRITE, "task container scratch"),
            )
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
    _agent_runtime,
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
