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
from collections.abc import Iterable, Mapping
from pathlib import Path

from vs_sandbox import (
    HostResource,
    HostResourceAccess,
    HostResourceContext,
    HostResourceDeclarer,
    declare_resources,
)

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


def _python_runtime(ctx: HostResourceContext) -> Iterable[HostResource]:
    del ctx
    return _resources((Path(sys.base_prefix), Path(sys.prefix)), purpose="Python runtime")


def _path_toolchain(ctx: HostResourceContext) -> Iterable[HostResource]:
    paths = (
        Path(entry).expanduser() for entry in ctx.env.get("PATH", "").split(os.pathsep) if entry
    )
    return _resources(paths, purpose="launcher PATH toolchain")


def _rust_toolchain(ctx: HostResourceContext) -> Iterable[HostResource]:
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


def _agent_runtime(ctx: HostResourceContext) -> Iterable[HostResource]:
    paths: list[Path] = []
    if ctx.binary_path:
        real_binary = Path(ctx.binary_path).resolve()
        paths.extend((_install_root(real_binary), real_binary.parent))

    node = shutil.which("node", path=ctx.env.get("PATH"))
    if node:
        real_node = Path(node).resolve()
        paths.extend((real_node.parent, real_node.parent.parent))

    try:
        import vibesys

        pkg_file = getattr(vibesys, "__file__", None)
        if pkg_file:
            paths.append(Path(pkg_file).resolve().parents[1])
    except Exception:  # pragma: no cover - defensive; import cannot normally fail here
        pass

    return _resources(paths, purpose="agent and VibeSys runtime")


def _provider_state(ctx: HostResourceContext) -> Iterable[HostResource]:
    home = ctx.env.get("HOME")
    if not home:
        return ()
    base = Path(home)
    paths: list[Path]
    if ctx.provider == "codex":
        codex_home = Path(ctx.env.get("CODEX_HOME", base / ".codex")).expanduser()
        paths = [
            codex_home / "auth.json",
            codex_home / "config.toml",
            base / ".config" / "codex",
        ]
    elif ctx.provider == "claude":
        paths = [base / ".claude", base / ".claude.json", base / ".config" / "claude"]
    elif ctx.provider == "gemini":
        paths = [base / ".gemini", base / ".config" / "gemini"]
    elif ctx.provider == "opencode":
        paths = [base / ".local" / "share" / "opencode", base / ".config" / "opencode"]
    else:
        paths = []

    if sys.platform == "darwin":
        support = base / "Library" / "Application Support"
        caches = base / "Library" / "Caches"
        if ctx.provider == "codex":
            paths.extend((support / "codex", support / "com.openai.codex", caches / "codex"))
        elif ctx.provider == "claude":
            paths.extend((support / "claude", caches / "claude"))

    return _resources(
        paths,
        access=HostResourceAccess.READ_WRITE,
        purpose=f"{ctx.provider or 'unknown'} agent state",
    )


def _operator_allowlist(ctx: HostResourceContext) -> Iterable[HostResource]:
    raw = ctx.env.get(ALLOW_ENV, "")
    paths = (Path(path).expanduser() for path in raw.split(os.pathsep) if path.strip())
    return _resources(paths, purpose=f"{ALLOW_ENV} entry")


DEFAULT_AGENT_HOST_RESOURCE_DECLARERS: tuple[HostResourceDeclarer, ...] = (
    _python_runtime,
    _path_toolchain,
    _rust_toolchain,
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
