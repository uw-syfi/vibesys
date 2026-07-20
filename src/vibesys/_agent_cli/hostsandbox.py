"""Host-level filesystem confinement for agent CLI subprocesses.

VibeSys launches coding-agent CLIs (codex, claude, ...) with the provider's own
approval/sandbox bypass flags (``--dangerously-bypass-approvals-and-sandbox``,
``--dangerously-skip-permissions``) so they can run autonomously. On the host
execution path that leaves the spawned agent process able to read and write
anywhere the VibeSys user can, so a misbehaving agent can step outside its
``exp_env/<run>/workspace`` and reach sibling runs, unrelated repositories, or
host secrets. Prompt-only containment is not a security boundary (issue #149).

This module wraps the agent command in an OS confinement layer that exposes only:

* a read-only view of system/toolchain directories and the Python/agent
  runtimes needed to launch,
* the agent's own config/auth directories (so it can authenticate),
* any explicitly allowed extra paths (model/weight caches, MCP server code),
* read-write access to the run workspace and a private ``/tmp``.

Everything else — including the workspace's *parent* and sibling runs — is
denied, so absolute-path traversal outside the workspace fails. Network is left
open so the agent can still reach its model provider.

Two host backends implement the same ``wrap(argv) -> argv`` contract, selected
by platform:

* **Linux** — :class:`HostSandbox`, a `bubblewrap <https://github.com/
  containers/bubblewrap>`_ (``bwrap``) mount namespace. Denied paths are simply
  absent from the namespace, so traversal fails with ``ENOENT``.
* **macOS** — :class:`SeatbeltSandbox`, a Seatbelt (``sandbox-exec``) profile
  with ``(deny default)`` and an explicit read/write allowlist.

The confinement is enforced by default on supported hosts. It is a *host*-path
concern only: the Docker and Modal executors already run the agent inside an
externally managed sandbox, so those paths never build a sandbox here.

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
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DISABLE_ENV = "VIBESYS_AGENT_SANDBOX"
ALLOW_ENV = "VIBESYS_AGENT_SANDBOX_ALLOW"

_DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

# Read-only system/toolchain roots exposed inside the Linux namespace. Bound
# with ``--ro-bind-try`` so a root that does not exist on a given host is
# skipped rather than aborting the launch.
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

# Read-only system roots the macOS dynamic linker and command-line tools need to
# launch anything at all. Kept deliberately broad on the *system* side (dyld,
# frameworks, config) while the workspace's parent and sibling runs stay denied
# by ``(deny default)``.
_MACOS_SYSTEM_READ_ROOTS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    # The dyld shared cache lives under the Preboot Cryptexes tree on
    # Apple Silicon; without read access here every dynamically linked binary
    # (including /bin/sh) aborts during dyld startup under (deny default).
    "/System/Volumes/Preboot/Cryptexes",
    "/Library",
    "/private/var/db",  # dyld cache, timezone, and other launch-time state
    "/private/etc",
    "/etc",
    "/dev",
    "/Applications",  # some agent CLIs ship here
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


def _install_root(real_path: Path) -> Path:
    """Directory to expose so *real_path* can find its siblings/dependencies.

    Node CLIs (codex, claude, ...) are shipped as npm packages whose launcher
    (``.../node_modules/<pkg>/bin/foo.js``) loads a platform binary from a
    *sibling* ``node_modules`` (e.g. ``@openai/codex-linux-x64``). Binding only
    the launcher's ``bin/`` dir hides that, so codex fails with "Missing optional
    dependency". Bind the directory that holds the top-level ``node_modules`` so
    the whole package tree resolves; otherwise bind the containing directory.
    """
    parts = real_path.parts
    if "node_modules" in parts:
        idx = parts.index("node_modules")  # top-level node_modules
        if idx > 0:
            return Path(*parts[:idx])
    return real_path.parent


def _default_read_paths(env: dict[str, str], binary_path: str | None) -> list[Path]:
    """Toolchain paths the agent needs to *launch* (never the workspace parent).

    These are specific subtrees — the Python runtime, the Node/agent install
    tree, and the VibeSys package source — rather than broad roots like
    ``$HOME``, precisely so sibling repositories under the same parent stay
    invisible.
    """
    candidates: list[Path] = [
        Path(sys.base_prefix),
        Path(sys.prefix),
    ]

    # The agent binary and its full install tree (e.g. the npm package plus its
    # platform-binary sibling), resolved through symlinks.
    if binary_path:
        real_binary = Path(binary_path).resolve()
        candidates.append(_install_root(real_binary))
        candidates.append(real_binary.parent)
    # Node and its install prefix (``<prefix>/bin/node`` -> ``<prefix>``), which
    # holds the global ``node_modules`` where the agent CLIs live.
    node = shutil.which("node", path=env.get("PATH"))
    if node:
        real_node = Path(node).resolve()
        candidates.append(real_node.parent)
        candidates.append(real_node.parent.parent)

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
    codex_home = Path(env.get("CODEX_HOME", base / ".codex")).expanduser()
    paths = [
        # Mount the files Codex needs rather than the whole directory. A Codex
        # checkout may itself live under ``$CODEX_HOME/worktrees``; mounting the
        # directory would expose sibling tasks, while dropping it makes the CLI
        # appear logged out inside the sandbox. Bubblewrap creates an ephemeral
        # parent directory for these leaf mounts in ``HostSandbox.wrap``.
        codex_home / "auth.json",
        codex_home / "config.toml",
        base / ".claude",
        base / ".claude.json",
        base / ".config" / "codex",
        base / ".config" / "claude",
    ]
    if sys.platform == "darwin":
        # macOS agent CLIs also keep state under Application Support / Caches.
        support = base / "Library" / "Application Support"
        caches = base / "Library" / "Caches"
        paths += [
            support / "codex",
            support / "claude",
            support / "com.openai.codex",
            caches / "codex",
            caches / "claude",
        ]
    return paths


def _parse_allow(env: dict[str, str]) -> list[Path]:
    raw = env.get(ALLOW_ENV, "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


@dataclass(frozen=True)
class WorkspaceSandbox(ABC):
    """A host confinement policy for a single run workspace.

    Both OS backends are built by :func:`build` from the same inputs — the
    workspace plus the read/write allowlists computed by :func:`_collect_paths`
    — and expose the same ``wrap(argv) -> argv`` contract consumed at the agent
    launch chokepoint (:meth:`CLICodingAgent.generate`). Subclasses differ only
    in the OS mechanism they emit: :class:`HostSandbox` a bubblewrap namespace,
    :class:`SeatbeltSandbox` a ``sandbox-exec`` profile.
    """

    workspace: Path
    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()

    @abstractmethod
    def wrap(self, argv: list[str]) -> list[str]:
        """Return *argv* rewritten to run confined to :attr:`workspace`."""


@dataclass(frozen=True)
class HostSandbox(WorkspaceSandbox):
    """A bubblewrap confinement policy for a single run workspace."""

    bwrap_path: str = field(kw_only=True)
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
        # Leaf config mounts such as ~/.codex/auth.json need their destination
        # parents to exist in bubblewrap's otherwise-empty /home tree. These
        # directories are ephemeral inside the namespace; only the explicitly
        # bound files below are sourced from the host.
        parent_dirs = {
            parent for path in self.write_paths for parent in path.parents if parent != Path("/")
        }
        for parent in sorted(parent_dirs, key=lambda path: len(path.parts)):
            cmd += ["--dir", str(parent)]
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


def _sbpl_string(value: str) -> str:
    """Quote *value* as a Seatbelt Profile Language (SBPL) string literal.

    SBPL string literals are double-quoted with backslash escaping, so a path
    containing a quote, backslash, or other special character is embedded safely
    rather than breaking the profile (or letting an attacker-controlled path
    inject profile syntax).
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(frozen=True)
class SeatbeltSandbox(WorkspaceSandbox):
    """A macOS Seatbelt (``sandbox-exec``) confinement policy for a workspace.

    macOS confinement follows the model that Codex's own Seatbelt sandbox uses,
    because it is the one that reliably launches Apple-Silicon toolchains:
    **reads are allowed broadly** (a ``(deny default)`` read policy makes dyld
    and code-signing abort every dynamically linked binary), while **writes are
    denied by default** and permitted only on the workspace, the agent's config
    dirs, and ``/tmp``. On top of that, the workspace's run-container tree — the
    directory that holds sibling runs — is explicitly denied for both read and
    write, with the workspace itself carved back out. That blocks the concrete
    escape from issue #149 (discovering/using a sibling run) and all writes
    outside the workspace.

    This is a weaker guarantee than the Linux bubblewrap backend, which hides the
    entire host outside the workspace: on macOS, reads of unrelated host files
    outside the run-container tree are still permitted. Use ``--docker`` on macOS
    if full read-confinement is required.
    """

    sandbox_exec_path: str = field(kw_only=True)
    system_read_roots: tuple[str, ...] = _MACOS_SYSTEM_READ_ROOTS

    def blind_roots(self) -> list[Path]:
        """Ancestor dirs of the workspace to deny (read+write) to hide siblings.

        Returns the workspace's parent and grandparent (the run and run-container
        dirs in the ``exp_env/<run>/workspace`` layout), skipping the filesystem
        root and any system location so the deny can never blind the toolchain.
        """
        roots: list[Path] = []
        for anc in list(self.workspace.parents)[:2]:
            if anc == anc.parent:  # filesystem root
                continue
            s = str(anc)
            if any(s == r or s.startswith(r + "/") for r in self.system_read_roots):
                continue
            roots.append(anc)
        return roots

    def profile(self) -> str:
        """Render the SBPL profile text for this policy."""
        ws = _sbpl_string(str(self.workspace))
        lines = [
            "(version 1)",
            "(deny default)",
            # Launching, threading, and the basic services a CLI needs.
            "(allow process-exec*)",
            "(allow process-fork)",
            # Map code-signed executable pages — without this, Apple-Silicon code
            # signing aborts every dynamically linked binary (dyld) at startup.
            "(allow file-map-executable)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow mach-per-user-lookup)",
            "(allow ipc-posix-shm)",
            "(allow system-socket)",
            "(allow iokit-open)",  # Metal / GPU access for benchmarks
            # Network stays open so the agent can reach its model provider.
            "(allow network*)",
            # Reads are allowed broadly so dyld/code-signing/toolchain work.
            "(allow file-read*)",
        ]

        # Writes are denied by default; permit them only on the workspace, the
        # agent's own config/auth dirs, scratch tmp, and device nodes (a process
        # must be able to write /dev/null, /dev/stdout, /dev/tty, ...).
        write_roots = [str(self.workspace)] + [str(p) for p in self.write_paths]
        write_roots += ["/private/tmp", "/private/var/tmp", "/dev"]
        lines.append("(allow file-write*")
        lines += [f"    (subpath {_sbpl_string(w)})" for w in write_roots]
        lines.append(")")

        # Blind the sibling-run area: deny read+write on the run-container tree,
        # then carve the workspace back out. The most-specific (last) matching
        # rule wins in SBPL, so workspace access survives the deny.
        blind = self.blind_roots()
        if blind:
            lines.append("(deny file-read* file-write*")
            lines += [f"    (subpath {_sbpl_string(str(r))})" for r in blind]
            lines.append(")")
            lines.append(f"(allow file-read* file-write* (subpath {ws}))")

        return "\n".join(lines) + "\n"

    def wrap(self, argv: list[str]) -> list[str]:
        """Return *argv* wrapped so it runs inside the Seatbelt profile."""
        # ``-p`` takes the profile inline, keeping ``wrap`` pure (no temp files).
        # ``sandbox-exec`` runs the command from the caller's cwd, which the CLI
        # runner already sets to the workspace.
        return [self.sandbox_exec_path, "-p", self.profile(), *argv]


def _collect_paths(
    workspace: Path,
    env: dict[str, str],
    binary_path: str | None,
    log: Callable[[str], None],
) -> tuple[list[Path], list[Path]]:
    """Compute the (read, write) allowlists shared by both host backends.

    Applies the sibling-isolation invariant: no allowlisted path may be an
    ancestor of the workspace, since that would re-expose sibling runs.
    """
    read_paths = _existing(_default_read_paths(env, binary_path))
    write_paths = _existing(_default_config_paths(env))

    for extra in _parse_allow(env):
        resolved = extra.resolve()
        if _is_ancestor(resolved, workspace):
            log(
                f"[hostsandbox] ignoring {ALLOW_ENV} entry {extra} because it is an "
                "ancestor of the workspace; allowing it would expose sibling runs."
            )
            continue
        if resolved.exists():
            read_paths.append(resolved)

    # Safety net: drop any default read/write path that is an ancestor of the
    # workspace. (Toolchain subtrees like the venv are not ancestors, so this
    # normally changes nothing.)
    read_paths = [p for p in read_paths if not _is_ancestor(p, workspace)]
    write_paths = [p for p in write_paths if not _is_ancestor(p, workspace)]
    return read_paths, write_paths


def build(
    workspace: Path | str,
    *,
    env: dict[str, str],
    binary_path: str | None = None,
    log: Callable[[str], None] | None = None,
) -> WorkspaceSandbox | None:
    """Build a host confinement policy for *workspace*, or ``None`` if not enforced.

    Dispatches to the Linux (bubblewrap) or macOS (Seatbelt) backend by platform.
    Returns ``None`` (and logs why) when confinement is disabled by the operator,
    when the host OS has no supported backend, or when the backend's tool
    (``bwrap`` / ``sandbox-exec``) is unavailable. In those cases the caller runs
    the agent unconfined, exactly as before this change — the sandbox can never
    *break* a run that used to work, it only ever adds a boundary.
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

    if sys.platform.startswith("linux"):
        return _build_linux(workspace, env=env, binary_path=binary_path, log=_log)
    if sys.platform == "darwin":
        return _build_macos(workspace, env=env, binary_path=binary_path, log=_log)

    _log(
        f"[hostsandbox] no host confinement backend for {sys.platform!r}; agent "
        "runs unconfined. Use --docker for an externally sandboxed run."
    )
    return None


def _build_linux(
    workspace: Path,
    *,
    env: dict[str, str],
    binary_path: str | None,
    log: Callable[[str], None],
) -> HostSandbox | None:
    bwrap = shutil.which("bwrap", path=env.get("PATH")) or shutil.which("bwrap")
    if not bwrap:
        log(
            "[hostsandbox] 'bwrap' not found on PATH; agent runs unconfined. "
            "Install bubblewrap or use --docker for an externally sandboxed run."
        )
        return None

    read_paths, write_paths = _collect_paths(workspace, env, binary_path, log)
    return HostSandbox(
        bwrap_path=bwrap,
        workspace=workspace,
        read_paths=tuple(read_paths),
        write_paths=tuple(write_paths),
        gpu_device_nodes=tuple(_gpu_device_nodes()),
    )


def _build_macos(
    workspace: Path,
    *,
    env: dict[str, str],
    binary_path: str | None,
    log: Callable[[str], None],
) -> SeatbeltSandbox | None:
    sandbox_exec = shutil.which("sandbox-exec", path=env.get("PATH")) or shutil.which(
        "sandbox-exec"
    )
    if not sandbox_exec:
        log(
            "[hostsandbox] 'sandbox-exec' not found; agent runs unconfined. "
            "Use --docker for an externally sandboxed run."
        )
        return None

    read_paths, write_paths = _collect_paths(workspace, env, binary_path, log)
    return SeatbeltSandbox(
        sandbox_exec_path=sandbox_exec,
        workspace=workspace,
        read_paths=tuple(read_paths),
        write_paths=tuple(write_paths),
    )
