"""The host helper's ``MachineProbe`` on a real host: hardware, Docker, harness CLIs, checkouts.

Every fact comes from a short command run through ``vs_sim``'s ``CommandProbe`` or from
the files a checkout holds, so tests drive it with ``ScriptedProbe`` and temporary
directories. Which vendor tools list GPUs, and how each names them, is data
(``HOST_GPU_QUERIES``) the helper's entrypoint passes in; the request layer knows only
the closed ``Accelerator`` set. Like the helper, the module is light
(``tests/architecture/test_light_entrypoints.py``).

Public surface: ``HostMachineProbe``, ``GpuQuery``, ``HOST_GPU_QUERIES``,
``DOCKER_VERSION_COMMAND``, ``PROBE_TIMEOUT_SECONDS``, ``git_state``, and the
version helpers ``release_of`` and ``vibesys_version``.
"""

from __future__ import annotations

import csv
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from server.host_api import (
    CheckoutFound,
    CheckoutInspection,
    CheckoutNotFound,
    CheckoutTooOld,
)
from server.host_machine_facts import (
    Accelerator,
    DockerReachable,
    DockerUnreachable,
    Gpu,
    Hardware,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_sim.api import CommandProbe

PROBE_TIMEOUT_SECONDS: Final = 5.0
"""How long one fact's command may take before the fact counts as unavailable."""
DOCKER_VERSION_COMMAND: Final = ("docker", "version", "--format", "{{.Server.Version}}")
"""Asks the Docker daemon (not only the client) for its version, so it proves reachability."""
_GIT_TIMEOUT_SECONDS = 2.0
"""Kept short: ``hello`` reports the helper's own checkout through ``git_state``."""
_PYPROJECT_LIMIT_BYTES = 1 << 20
_HOST_HELPER_ENTRYPOINT = ("src", "entrypoints", "host_helper.py")
_RELEASE = re.compile(r"^(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class GpuQuery:
    """One vendor tool that lists a machine's GPUs: what to run, and what its GPUs are."""

    argv: tuple[str, ...]
    vendor: str
    accelerator: Accelerator
    models: Callable[[str], tuple[str, ...]]
    """The GPU model names in the tool's output; none when it lists no GPU."""


def _lines(output: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _card_series(output: str) -> tuple[str, ...]:
    rows = list(csv.reader(_lines(output)))
    if not rows:
        return ()
    header = [cell.strip().casefold() for cell in rows[0]]
    if "card series" not in header:
        return ()
    column = header.index("card series")
    return tuple(row[column].strip() for row in rows[1:] if len(row) > column and row[column])


def _apple_silicon(output: str) -> tuple[str, ...]:
    return tuple(line for line in _lines(output) if line.startswith("Apple "))


HOST_GPU_QUERIES: Final = (
    GpuQuery(
        ("nvidia-smi", "--query-gpu=name", "--format=csv,noheader"),
        "nvidia",
        Accelerator.CUDA,
        _lines,
    ),
    GpuQuery(("rocm-smi", "--showproductname", "--csv"), "amd", Accelerator.ROCM, _card_series),
    GpuQuery(
        ("sysctl", "-n", "machdep.cpu.brand_string"), "apple", Accelerator.METAL, _apple_silicon
    ),
)
"""The vendor tools asked in order; the first that lists a GPU names the accelerator."""


def release_of(version: str) -> tuple[int, ...] | None:
    """The release segment of a version (``0.1.1`` of ``0.1.1.dev3``), or ``None`` without one."""
    match = _RELEASE.match(version)
    return None if match is None else tuple(int(part) for part in match.group(1).split("."))


class HostMachineProbe:
    """Reads this machine's facts through ``probe``; ``which`` finds an installed program."""

    def __init__(
        self,
        probe: CommandProbe,
        *,
        cpu_cores: Callable[[], int | None],
        which: Callable[[str, str | None], str | None],
        harness_binaries: Callable[[], tuple[tuple[str, str], ...]],
        launch_environment: Callable[[], Mapping[str, str]],
    ) -> None:
        """Bind the command probe and the host seams.

        ``which(program, path)`` finds ``program`` on the ``path`` search path.
        ``harness_binaries`` lists each vetted provider with its CLI's program name, in
        offer order; it is read on first use, since the list lives in the agent library.
        ``launch_environment`` is the environment a run finds provider CLIs in (the agent
        library's interactive login shell's): the helper itself may have been started by a
        non-login shell whose ``PATH`` lacks the directory a CLI was installed to.
        """
        self._probe = probe
        self._cpu_cores = cpu_cores
        self._which = which
        self._harness_binaries = harness_binaries
        self._launch_environment = launch_environment

    def hardware(self) -> Hardware:
        """The CPU cores and the GPUs the first vendor tool that lists any reports."""
        for query in HOST_GPU_QUERIES:
            models = self._gpu_models(query)
            if models:
                gpus = tuple(Gpu(vendor=query.vendor, model=model) for model in models)
                return Hardware(cpu_cores=self._cores(), gpus=gpus, accelerator=query.accelerator)
        return Hardware(cpu_cores=self._cores(), gpus=(), accelerator=Accelerator.CPU)

    def docker(self) -> DockerReachable | DockerUnreachable:
        """Whether the Docker daemon answers, with its version or why not, in one line."""
        result = self._probe.run(DOCKER_VERSION_COMMAND, timeout_seconds=PROBE_TIMEOUT_SECONDS)
        if result is None:
            return DockerUnreachable(
                reason=f"docker is not installed or did not answer within {PROBE_TIMEOUT_SECONDS:g} s"
            )
        version = result.stdout.strip()
        if result.returncode == 0 and version:
            return DockerReachable(version=version)
        detail = next(iter(_lines(result.stderr)), "")
        return DockerUnreachable(
            reason=detail or f"docker version exited with status {result.returncode}"
        )

    def harness_clis(self) -> tuple[str, ...]:
        """The vetted providers whose CLI program is on the search path a run launches with."""
        path = self._launch_environment().get("PATH")
        return tuple(
            provider
            for provider, binary in self._harness_binaries()
            if self._which(binary, path) is not None
        )

    def inspect_checkout(self, path: Path) -> CheckoutInspection:
        """``found`` for a VibeSys checkout at least ``MIN_CHECKOUT_VERSION``, else why not."""
        # lint-waiver: LW-271601 [PLC0415]; the helper answers hello without this module.
        # > Only checkout inspection compares versions; at module level every channel would
        # > load the generated constant before `hello`, one module over the light-entrypoint
        # > ceiling. Re-exporting it from `host_api` would put it back on the hello path, and a
        # > literal copy would be a second source of truth beside the generated one.
        from server.host_compatibility_generated import MIN_CHECKOUT_VERSION  # noqa: PLC0415

        version = vibesys_version(path / "pyproject.toml")
        if version is None:
            return CheckoutNotFound()
        release = release_of(version)
        minimum = release_of(MIN_CHECKOUT_VERSION)
        if release is None or minimum is None or release < minimum:
            return CheckoutTooOld(version=version, min=MIN_CHECKOUT_VERSION)
        root = str(path.resolve())
        commit, dirty = git_state(self._probe, root)
        return CheckoutFound(
            root=root,
            version=version,
            commit=commit,
            dirty=dirty,
            compatible=path.joinpath(*_HOST_HELPER_ENTRYPOINT).is_file(),
        )

    def repository_root(self, path: Path) -> Path | None:
        """The root of the Git repository ``path`` is in, or ``None`` when git names none."""
        result = self._probe.run(
            ("git", "-C", str(path), "rev-parse", "--show-toplevel"),
            timeout_seconds=_GIT_TIMEOUT_SECONDS,
        )
        if result is None or result.returncode != 0 or not result.stdout.strip():
            return None
        return Path(result.stdout.strip()).resolve()

    def _gpu_models(self, query: GpuQuery) -> tuple[str, ...]:
        result = self._probe.run(query.argv, timeout_seconds=PROBE_TIMEOUT_SECONDS)
        if result is None or result.returncode != 0:
            return ()
        return query.models(result.stdout)

    def _cores(self) -> int:
        return max(self._cpu_cores() or 1, 1)


def git_state(probe: CommandProbe, root: str) -> tuple[str | None, bool | None]:
    """A checkout's ``HEAD`` commit and whether tracked files differ from it; ``None`` when git cannot say."""
    head = probe.run(("git", "-C", root, "rev-parse", "HEAD"), timeout_seconds=_GIT_TIMEOUT_SECONDS)
    status = probe.run(
        ("git", "-C", root, "status", "--porcelain", "--untracked-files=no"),
        timeout_seconds=_GIT_TIMEOUT_SECONDS,
    )
    commit = head.stdout.strip() if head is not None and head.returncode == 0 else None
    dirty = bool(status.stdout.strip()) if status is not None and status.returncode == 0 else None
    return commit or None, dirty


def vibesys_version(pyproject: Path) -> str | None:
    """The version a VibeSys ``pyproject.toml`` declares (empty if none), or ``None`` if it is not one."""
    try:
        if pyproject.stat().st_size > _PYPROJECT_LIMIT_BYTES:
            return None
        with pyproject.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != "vibesys":
        return None
    version = project.get("version")
    return version if isinstance(version, str) else ""


__all__ = [
    "DOCKER_VERSION_COMMAND",
    "HOST_GPU_QUERIES",
    "PROBE_TIMEOUT_SECONDS",
    "GpuQuery",
    "HostMachineProbe",
    "git_state",
    "release_of",
    "vibesys_version",
]
