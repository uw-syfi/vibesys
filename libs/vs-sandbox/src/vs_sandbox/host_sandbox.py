"""Host-level filesystem confinement for agent CLI subprocesses.

VibeSys launches coding-agent CLIs (codex, claude, ...) with the provider's own
approval/sandbox bypass flags (``--dangerously-bypass-approvals-and-sandbox``,
``--dangerously-skip-permissions``) so they can run autonomously. On the host
execution path that leaves the spawned agent process able to read and write
anywhere the VibeSys user can, so a misbehaving agent can step outside the
canonical project root and reach sibling projects, unrelated repositories, or
host secrets. Prompt-only containment is not a security boundary (issue #149).

This module wraps the agent command in an OS confinement layer that exposes only:

* a read-only view of system/toolchain directories and the Python/agent
  runtimes needed to launch,
* resources supplied as typed :class:`~vs_sandbox.host_resources.HostResource`
  declarations (agent state, toolchains, model caches, MCP server code),
* read-write access to the canonical project root and a private ``/tmp``.

Everything else, including the project's *parent* and sibling projects, is
denied, so absolute-path traversal outside the project fails. Network is left
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

Resource discovery and policy are deliberately outside this module. The caller
passes declarations through the public resource SDK; this consumer only
validates them and implements their requested access.
"""

from __future__ import annotations

import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable  # noqa: TC003  # tracked: #288
from dataclasses import dataclass, field
from pathlib import Path

from vs_sandbox.host_resource_importer import prepare_host_resource_imports
from vs_sandbox.host_resources import HostResource  # noqa: TC001  # tracked: #288
from vs_sandbox.project_paths import ProjectPathPolicy

DISABLE_ENV = "VIBESYS_AGENT_SANDBOX"

_DISABLED_VALUES = frozenset({"0", "false", "off", "no"})


class SandboxUnavailableError(RuntimeError):
    """Raised when a caller requires host confinement but none is available."""


@dataclass(frozen=True)
class _BuildOptions:
    """Validated shared inputs passed to an OS-specific sandbox builder."""

    env: dict[str, str]
    resources: Iterable[HostResource]
    log: Callable[[str], None]
    project_path_policy: ProjectPathPolicy
    require_enforcement: bool


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
    # Accelerator runtimes discover devices through sysfs before they ever
    # touch the device node: without it the AWS Neuron runtime aborts in
    # ``nrt_init`` and ``neuron-ls`` exits fatal, and CUDA's device query
    # fails the same way. Read-only, so it exposes topology, not control.
    "/sys",
)

# Read-only system roots the macOS dynamic linker and command-line tools need to
# launch anything at all. Kept deliberately broad on the *system* side (dyld,
# frameworks, config) while the project's parent and siblings stay denied
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


@dataclass(frozen=True)
class WorkspaceSandbox(ABC):
    """A host confinement policy for a single canonical project.

    Both OS backends are built by :func:`build` from the same inputs: the
    project plus the read/write allowlists computed from resource declarations.
    They expose the same ``wrap(argv) -> argv`` contract consumed at the process
    launch chokepoint. Subclasses differ only
    in the OS mechanism they emit: :class:`HostSandbox` a bubblewrap namespace,
    :class:`SeatbeltSandbox` a ``sandbox-exec`` profile.
    """

    workspace: Path
    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    project_path_policy: ProjectPathPolicy = field(default_factory=ProjectPathPolicy)

    @abstractmethod
    def wrap(self, argv: list[str]) -> list[str]:
        """Return *argv* rewritten to run confined to :attr:`workspace`."""


@dataclass(frozen=True)
class HostSandbox(WorkspaceSandbox):
    """A bubblewrap confinement policy for a single canonical project."""

    bwrap_path: str = field(kw_only=True)
    system_read_roots: tuple[str, ...] = _SYSTEM_READ_ROOTS
    gpu_device_nodes: tuple[Path, ...] = field(default_factory=tuple)

    def wrap(self, argv: list[str]) -> list[str]:
        """Return *argv* wrapped so it runs inside the confinement namespace."""
        ws = str(self.workspace.resolve())
        project_paths = self.project_path_policy.resolve(self.workspace)
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
            "/tmp",  # noqa: S108  # tracked: #288
        ]
        # Leaf config mounts such as ~/.codex/auth.json need their destination
        # parents to exist in bubblewrap's otherwise-empty /home tree. These
        # directories are ephemeral inside the namespace; only the explicitly
        # bound files below are sourced from the host.
        parent_dirs = {
            parent
            for path in (*self.read_paths, *self.write_paths)
            if path.is_file()
            for parent in path.parents
            if parent != Path("/")
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
        # The project bind wins over imported host resources. Nested project
        # restrictions are then overlaid in increasing strength: read-only
        # paths first, hidden masks last.
        cmd += ["--bind", ws, ws]
        for path in project_paths.read_only_paths:
            cmd += ["--ro-bind", str(path.path), str(path.path)]
        for path in project_paths.hidden_paths:
            if path.is_directory:
                cmd += ["--tmpfs", str(path.path)]
            else:
                cmd += ["--ro-bind", "/dev/null", str(path.path)]
        cmd += ["--chdir", ws, "--"]
        cmd += argv
        return cmd


def _gpu_device_nodes() -> list[Path]:
    """Accelerator character devices to pass through.

    ``--dev`` mounts a minimal devtmpfs that omits every accelerator node, so
    anything the agent must reach to exercise its own code has to be bound back
    explicitly. That covers NVIDIA GPUs, DRI render nodes, and AWS Neuron
    devices for the Trainium backend's local (non-Docker) path.
    """
    dev = Path("/dev")
    if not dev.exists():
        return []
    nodes: list[Path] = []
    for pattern in ("nvidia*", "nvidia-uvm*", "nvidia-caps", "neuron*"):
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
    """A macOS Seatbelt (``sandbox-exec``) policy for a canonical project.

    macOS confinement follows the model that Codex's own Seatbelt sandbox uses,
    because it is the one that reliably launches Apple-Silicon toolchains:
    **reads are allowed broadly** (a ``(deny default)`` read policy makes dyld
    and code-signing abort every dynamically linked binary), while **writes are
    denied by default** and permitted only on the project, the agent's config
    dirs, and ``/tmp``. On top of that, the project's nearest ancestor trees are
    explicitly denied for both read and write, with the project itself carved
    back out. That blocks the concrete escape from issue #149 (discovering or
    using a sibling project) and all writes outside the project.

    This is a weaker guarantee than the Linux bubblewrap backend, which hides the
    entire host outside the project: on macOS, reads of unrelated host files
    outside the denied ancestor trees are still permitted. Use ``--docker`` on macOS
    if full read-confinement is required.
    """

    sandbox_exec_path: str = field(kw_only=True)
    system_read_roots: tuple[str, ...] = _MACOS_SYSTEM_READ_ROOTS

    def blind_roots(self) -> list[Path]:
        """Project ancestor directories to deny for sibling isolation.

        Returns the project's parent and grandparent, skipping the filesystem
        root and system locations so the deny can never blind the toolchain.
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
        workspace = self.workspace.resolve()
        project_paths = self.project_path_policy.resolve(workspace)
        ws = _sbpl_string(str(workspace))
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

        # Writes are denied by default; permit them only on the project, the
        # agent's own config/auth dirs, scratch tmp, and device nodes (a process
        # must be able to write /dev/null, /dev/stdout, /dev/tty, ...).
        write_roots = [str(self.workspace)] + [str(p) for p in self.write_paths]
        write_roots += ["/private/tmp", "/private/var/tmp", "/dev"]
        lines.append("(allow file-write*")
        lines += [f"    (subpath {_sbpl_string(w)})" for w in write_roots]
        lines.append(")")

        # Blind nearby sibling projects by denying the project's ancestor
        # trees, then carve the project back out. The most-specific (last)
        # matching rule wins in SBPL, so project access survives the deny.
        blind = self.blind_roots()
        if blind:
            lines.append("(deny file-read* file-write*")
            lines += [f"    (subpath {_sbpl_string(str(r))})" for r in blind]
            lines.append(")")
            lines.append(f"(allow file-read* file-write* (subpath {ws}))")

        # Project restrictions come after every project allow. Hidden paths
        # deny both visibility and mutation; read-only paths deny mutation.
        for path in project_paths.read_only_paths:
            lines.append(
                _seatbelt_path_rule("deny file-write*", path.path, is_directory=path.is_directory)
            )
        for path in project_paths.hidden_paths:
            lines.append(
                _seatbelt_path_rule(
                    "deny file-read* file-write*",
                    path.path,
                    is_directory=path.is_directory,
                )
            )

        return "\n".join(lines) + "\n"

    def wrap(self, argv: list[str]) -> list[str]:
        """Return *argv* wrapped so it runs inside the Seatbelt profile."""
        # ``-p`` takes the profile inline, keeping ``wrap`` pure (no temp files).
        # ``sandbox-exec`` runs the command from the caller's cwd, which the CLI
        # runner already sets to the workspace.
        return [self.sandbox_exec_path, "-p", self.profile(), *argv]


def _seatbelt_path_rule(
    operation: str,
    path: Path,
    *,
    is_directory: bool,
) -> str:
    """Render a path-specific deny for a resolved project path."""
    predicate = "subpath" if is_directory else "literal"
    value = _sbpl_string(str(path))
    return f"({operation} ({predicate} {value}))"


def _resource_paths(
    workspace: Path,
    log: Callable[[str], None],
    resources: Iterable[HostResource],
) -> tuple[list[Path], list[Path]]:
    """Prepare SDK declarations for an OS-specific import backend."""
    imports = prepare_host_resource_imports(workspace, resources, log=log)
    return list(imports.read_paths), list(imports.write_paths)


def build(  # noqa: PLR0913
    workspace: Path | str,
    *,
    env: dict[str, str],
    resources: Iterable[HostResource] = (),
    log: Callable[[str], None] | None = None,
    project_path_policy: ProjectPathPolicy | None = None,
    require_enforcement: bool = False,
) -> WorkspaceSandbox | None:
    """Build a host confinement policy for *workspace*, or ``None`` if not enforced.

    Dispatches to the Linux (bubblewrap) or macOS (Seatbelt) backend by platform.
    Returns ``None`` (and logs why) when confinement is disabled by the operator,
    when the host OS has no supported backend, or when the backend's tool
    (``bwrap`` / ``sandbox-exec``) is unavailable. In those cases the caller runs
    the agent unconfined, exactly as before this change — the sandbox can never
    *break* a run that used to work, it only ever adds a boundary. Set
    ``require_enforcement`` to fail closed with :class:`SandboxUnavailableError`
    instead. Omitting both new policy arguments preserves the legacy behavior.
    """

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    workspace = Path(workspace).resolve()
    policy = project_path_policy or ProjectPathPolicy()
    policy.validate(workspace)
    options = _BuildOptions(
        env=env,
        resources=resources,
        log=_log,
        project_path_policy=policy,
        require_enforcement=require_enforcement,
    )

    if _is_disabled(env):
        message = (
            f"[hostsandbox] DISABLED via {DISABLE_ENV}; agent runs with full host "
            "filesystem access. Sibling runs and host files are reachable."
        )
        return _unavailable(message, require_enforcement=require_enforcement, log=_log)

    if sys.platform.startswith("linux"):
        return _build_linux(workspace, options)
    if sys.platform == "darwin":
        return _build_macos(workspace, options)

    message = (
        f"[hostsandbox] no host confinement backend for {sys.platform!r}; agent "
        "runs unconfined. Use --docker for an externally sandboxed run."
    )
    return _unavailable(message, require_enforcement=require_enforcement, log=_log)


def _unavailable(
    message: str,
    *,
    require_enforcement: bool,
    log: Callable[[str], None],
) -> None:
    log(message)
    if require_enforcement:
        raise SandboxUnavailableError(message)


def _build_linux(
    workspace: Path,
    options: _BuildOptions,
) -> HostSandbox | None:
    bwrap = shutil.which("bwrap", path=options.env.get("PATH")) or shutil.which("bwrap")
    if not bwrap:
        message = (
            "[hostsandbox] 'bwrap' not found on PATH; agent runs unconfined. "
            "Install bubblewrap or use --docker for an externally sandboxed run."
        )
        return _unavailable(
            message,
            require_enforcement=options.require_enforcement,
            log=options.log,
        )

    read_paths, write_paths = _resource_paths(workspace, options.log, options.resources)
    return HostSandbox(
        bwrap_path=bwrap,
        workspace=workspace,
        read_paths=tuple(read_paths),
        write_paths=tuple(write_paths),
        project_path_policy=options.project_path_policy,
        gpu_device_nodes=tuple(_gpu_device_nodes()),
    )


def _build_macos(
    workspace: Path,
    options: _BuildOptions,
) -> SeatbeltSandbox | None:
    sandbox_exec = shutil.which("sandbox-exec", path=options.env.get("PATH")) or shutil.which(
        "sandbox-exec"
    )
    if not sandbox_exec:
        message = (
            "[hostsandbox] 'sandbox-exec' not found; agent runs unconfined. "
            "Use --docker for an externally sandboxed run."
        )
        return _unavailable(
            message,
            require_enforcement=options.require_enforcement,
            log=options.log,
        )

    read_paths, write_paths = _resource_paths(workspace, options.log, options.resources)
    return SeatbeltSandbox(
        sandbox_exec_path=sandbox_exec,
        workspace=workspace,
        read_paths=tuple(read_paths),
        write_paths=tuple(write_paths),
        project_path_policy=options.project_path_policy,
    )
