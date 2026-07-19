"""Host-level filesystem confinement for agent CLI subprocesses.

VibeSys launches coding-agent CLIs (codex, claude, ...) with the provider's own
approval/sandbox bypass flags (``--dangerously-bypass-approvals-and-sandbox``,
``--dangerously-skip-permissions``) so they can run autonomously. On the host
execution path that leaves the spawned agent process able to read and write
anywhere the VibeSys user can, so a misbehaving agent can step outside its
``exp_env/<run>/workspace`` and reach sibling runs, unrelated repositories, or
host secrets. Prompt-only containment is not a security boundary (issue #149).

This module wraps the agent command in a `bubblewrap <https://github.com/
containers/bubblewrap>`_ (``bwrap``) mount namespace that exposes only:

* a read-only view of system/toolchain directories and the Python/agent
  runtimes needed to launch,
* the agent's own config/auth directories (so it can authenticate),
* any explicitly allowed extra paths (model/weight caches, MCP server code),
* a writable bind of the run workspace and a private ``/tmp``.

Everything else — including the workspace's *parent* and sibling runs — is
simply absent from the namespace, so absolute-path traversal outside the
workspace fails with ``ENOENT``. Network is left shared so the agent can still
reach its model provider.

The confinement is enforced by default on Linux hosts. It is a *host*-path
concern only: the Docker and Modal executors already run the agent inside an
externally managed sandbox, so those paths never build a :class:`HostSandbox`.

Operator controls (read from the agent's environment):

``VIBESYS_AGENT_SANDBOX``
    Set to ``0``/``false``/``off``/``no`` to disable host confinement (e.g. for
    debugging, or on a host whose toolchain layout the default allowlist does
    not cover). Disabling is logged loudly.

``VIBESYS_AGENT_SANDBOX_ALLOW``
    ``os.pathsep``-separated list of extra host paths to expose read-only inside
    the sandbox (model weights, HF/torch caches, MCP server code, ...). Paths
    that are ancestors of the workspace are rejected, because binding them would
    re-expose sibling runs and defeat the confinement.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DISABLE_ENV = "VIBESYS_AGENT_SANDBOX"
ALLOW_ENV = "VIBESYS_AGENT_SANDBOX_ALLOW"

_DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

# Read-only system/toolchain roots exposed inside the namespace. Bound with
# ``--ro-bind-try`` so a root that does not exist on a given host is skipped
# rather than aborting the launch.
_SYSTEM_READ_ROOTS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/lib32",
    "/etc",
    "/opt",
    "/run/systemd/resolve",  # DNS via systemd-resolved
)


def _is_disabled(env: dict[str, str]) -> bool:
    return env.get(DISABLE_ENV, "").strip().lower() in _DISABLED_VALUES


def _is_ancestor(ancestor: Path, path: Path) -> bool:
    """Return ``True`` if *ancestor* is *path* itself or one of its parents."""
    try:
        path.relative_to(ancestor)
    except ValueError:
        return False
    return True


def _existing(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        out.append(p)
    return out


def _default_read_paths(env: dict[str, str], binary_path: str | None) -> list[Path]:
    """Toolchain paths the agent needs to *launch* (never the workspace parent).

    These are specific subtrees — the Python runtime, the Node/agent install
    directory, and the VibeSys package source — rather than broad roots like
    ``$HOME``, precisely so sibling repositories under the same parent stay
    invisible.
    """
    candidates: list[Path] = [
        Path(sys.base_prefix),
        Path(sys.prefix),
    ]

    # The agent binary and its interpreter (e.g. a Node install under ~/.nvm),
    # resolved through any symlinks so the real install tree is exposed.
    if binary_path:
        real_binary = Path(binary_path).resolve()
        candidates.append(real_binary.parent)
    node = shutil.which("node", path=env.get("PATH"))
    if node:
        candidates.append(Path(node).resolve().parent)

    # The installed VibeSys source tree, so codex/claude MCP servers launched as
    # ``python -m ...`` can import the package even under an editable install.
    try:
        import vibesys

        pkg_file = getattr(vibesys, "__file__", None)
        if pkg_file:
            candidates.append(Path(pkg_file).resolve().parents[1])
    except Exception:  # pragma: no cover - defensive; import cannot normally fail here
        pass

    return candidates


def _default_config_paths(env: dict[str, str]) -> list[Path]:
    """Read-write agent config/auth directories (the agent's own state only)."""
    home = env.get("HOME")
    if not home:
        return []
    base = Path(home)
    return [
        base / ".codex",
        base / ".claude",
        base / ".claude.json",
        base / ".config" / "codex",
        base / ".config" / "claude",
    ]


def _parse_allow(env: dict[str, str]) -> list[Path]:
    raw = env.get(ALLOW_ENV, "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


@dataclass(frozen=True)
class HostSandbox:
    """A bubblewrap confinement policy for a single run workspace."""

    bwrap_path: str
    workspace: Path
    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    system_read_roots: tuple[str, ...] = _SYSTEM_READ_ROOTS
    gpu_device_nodes: tuple[Path, ...] = field(default_factory=tuple)

    def wrap(self, argv: list[str]) -> list[str]:
        """Return *argv* wrapped so it runs inside the confinement namespace."""
        ws = str(self.workspace)
        cmd: list[str] = [
            self.bwrap_path,
            "--die-with-parent",
            "--unshare-user",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-cgroup-try",
            # Network is intentionally shared: the agent must reach its model
            # provider. Filesystem confinement is what blocks the escape.
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for root in self.system_read_roots:
            cmd += ["--ro-bind-try", root, root]
        for path in self.read_paths:
            cmd += ["--ro-bind-try", str(path), str(path)]
        for node in self.gpu_device_nodes:
            cmd += ["--dev-bind-try", str(node), str(node)]
        for path in self.write_paths:
            cmd += ["--bind-try", str(path), str(path)]
        # The workspace is the one project path the agent may modify. Bound last
        # so it wins over any read-only bind that happens to cover it.
        cmd += ["--bind", ws, ws]
        cmd += ["--chdir", ws, "--"]
        cmd += argv
        return cmd


def _gpu_device_nodes() -> list[Path]:
    """NVIDIA character devices to pass through (``--dev`` hides them otherwise)."""
    dev = Path("/dev")
    if not dev.exists():
        return []
    nodes: list[Path] = []
    for pattern in ("nvidia*", "nvidia-uvm*", "nvidia-caps"):
        nodes.extend(sorted(dev.glob(pattern)))
    # ``/dev/dri`` (render nodes) for non-NVIDIA / integrated GPUs.
    dri = dev / "dri"
    if dri.exists():
        nodes.append(dri)
    return nodes


def build(
    workspace: Path | str,
    *,
    env: dict[str, str],
    binary_path: str | None = None,
    log: Callable[[str], None] | None = None,
) -> HostSandbox | None:
    """Build a :class:`HostSandbox` for *workspace*, or ``None`` if not enforced.

    Returns ``None`` (and logs why) when confinement is disabled by the operator,
    when the host is not Linux, or when ``bwrap`` / user namespaces are
    unavailable. In those cases the caller runs the agent unconfined, exactly as
    before this change, so the sandbox can never *break* a run that used to work
    — it only ever adds a boundary.
    """

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    workspace = Path(workspace).resolve()

    if _is_disabled(env):
        _log(
            f"[hostsandbox] DISABLED via {DISABLE_ENV}; agent runs with full host "
            "filesystem access. Sibling runs and host files are reachable."
        )
        return None

    if not sys.platform.startswith("linux"):
        _log(
            f"[hostsandbox] host confinement unavailable on {sys.platform!r} "
            "(bubblewrap is Linux-only); agent runs unconfined. Use --docker for "
            "an externally sandboxed run."
        )
        return None

    bwrap = shutil.which("bwrap", path=env.get("PATH")) or shutil.which("bwrap")
    if not bwrap:
        _log(
            "[hostsandbox] 'bwrap' not found on PATH; agent runs unconfined. "
            "Install bubblewrap or use --docker for an externally sandboxed run."
        )
        return None

    read_paths = _existing(_default_read_paths(env, binary_path))
    write_paths = _existing(_default_config_paths(env))

    # Extra operator-allowed read paths, minus any that would re-expose the
    # workspace's parent (and therefore sibling runs).
    for extra in _parse_allow(env):
        resolved = extra.resolve()
        if _is_ancestor(resolved, workspace):
            _log(
                f"[hostsandbox] ignoring {ALLOW_ENV} entry {extra} because it is an "
                "ancestor of the workspace; binding it would expose sibling runs."
            )
            continue
        if resolved.exists():
            read_paths.append(resolved)

    # Never expose a default read/write path that is an ancestor of the
    # workspace — that would defeat sibling isolation. (Toolchain subtrees like
    # the venv are not ancestors, so this is a safety net, not a common case.)
    read_paths = [p for p in read_paths if not _is_ancestor(p, workspace)]
    write_paths = [p for p in write_paths if not _is_ancestor(p, workspace)]

    return HostSandbox(
        bwrap_path=bwrap,
        workspace=workspace,
        read_paths=tuple(read_paths),
        write_paths=tuple(write_paths),
        gpu_device_nodes=tuple(_gpu_device_nodes()),
    )
