"""Finding a tool for the environment of an agent's GPU job.

A GPU job runs with the host's environment, whose ``PATH`` usually lacks the CUDA
toolkit: ``ncu`` and ``nsys`` sit in ``/usr/local/cuda/bin`` or an Nsight install
directory that no login shell adds, so a bare ``ncu`` in a job exits 127. This module
answers where such a tool is, as an absolute path, by searching the job's ``PATH`` and
then the toolkit directories, so the agent is told a command that works.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Directories (as globs) where a CUDA toolkit or an Nsight install puts its tools.
TOOLKIT_BIN_PATTERNS: tuple[str, ...] = (
    "/usr/local/cuda/bin",
    "/usr/local/cuda-*/bin",
    "/opt/nvidia/nsight-compute/*",
    "/opt/nvidia/nsight-systems/*/bin",
    "/opt/nvidia/nsight-systems/*/target-linux-x64",
)


def find_job_tool(
    executable: str,
    *,
    path_env: str,
    toolkit_patterns: tuple[str, ...] = TOOLKIT_BIN_PATTERNS,
) -> str | None:
    """Return a command that runs *executable* in a job whose ``PATH`` is *path_env*.

    The bare name when the job's ``PATH`` already finds it; otherwise the absolute path
    inside the first toolkit directory that has it (newest version first); else ``None``.
    """
    for directory in path_env.split(os.pathsep):
        if directory and _is_executable(Path(directory) / executable):
            return executable
    for pattern in toolkit_patterns:
        for directory in sorted(_expand(pattern), key=_version_key, reverse=True):
            candidate = directory / executable
            if _is_executable(candidate):
                return str(candidate)
    return None


def off_path_tools(
    executables: tuple[str, ...],
    *,
    path_env: str,
    toolkit_patterns: tuple[str, ...] = TOOLKIT_BIN_PATTERNS,
) -> tuple[tuple[str, str], ...]:
    """Return ``(name, absolute path)`` for each of *executables* only a toolkit directory has.

    A tool a job's ``PATH`` already finds is left out. Each of the rest is a tool the
    agent must name by absolute path, because a bare name exits 127 in the job.
    """
    found: list[tuple[str, str]] = []
    for name in executables:
        command = find_job_tool(name, path_env=path_env, toolkit_patterns=toolkit_patterns)
        if command is not None and command != name:
            found.append((name, command))
    return tuple(found)


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _expand(pattern: str) -> list[Path]:
    """The existing directories an absolute glob *pattern* matches."""
    anchor = Path(pattern).anchor
    return [path for path in Path(anchor).glob(pattern[len(anchor) :]) if path.is_dir()]


def _version_key(path: Path) -> list[tuple[int, int, str]]:
    """Order paths naturally, so ``cuda-12.10`` is newer than ``cuda-12.9``."""
    return [
        (1, int(part), "") if part.isdigit() else (0, 0, part)
        for part in re.split(r"(\d+)", str(path))
        if part
    ]
