"""Image builds: tags by tree digest, the `Builder` interface, and `BuildctlBuilder`.

An image is tagged with a digest of its build context, so a context that did not change
names an image the registry already has, and BuildKit's layer cache (kept by the
builder between jobs, with the Dockerfiles' cache mounts) makes its rebuild a no-op.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys_evaluator.deploy._k8s._clock import Clock

# Files tools generate are not part of a context's identity (vibesys-cli leaves them out of
# the task version for the same reason).
IGNORED_DIRS = frozenset({"__pycache__", ".git"})
IGNORED_SUFFIXES = (".pyc",)


class BuildError(Exception):
    """An image did not build; the message names it and ends with the build log's tail."""


@dataclass(frozen=True)
class Build:
    context: Path  # holds the Dockerfile
    image: str  # registry/repository:tag, pushed when built


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, dirs, names in os.walk(root):
        base = Path(directory)
        links = [d for d in dirs if (base / d).is_symlink()]
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and d not in links]
        found.extend(base / n for n in names if not n.endswith(IGNORED_SUFFIXES))
        found.extend(base / d for d in links)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def tree_digest(root: Path) -> str:
    """16 hex digits over every file's relative path, mode (executable or not) and content."""
    digest = hashlib.sha256()
    for path in _files(root):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        if path.is_symlink():
            digest.update(b"L" + str(path.readlink()).encode())
        else:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            digest.update(b"x" if os.access(path, os.X_OK) else b"-")
    return digest.hexdigest()[:16]


def stage(source: Path, into: Path) -> Path:
    """Copy a context without the generated files, so the build sees what the digest covers."""
    for path in _files(source):
        target = into / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            target.symlink_to(path.readlink())
        else:
            shutil.copy2(path, target)
    return into


_REPO = re.compile(r"[^a-z0-9._-]+")


def image_name(registry: str, app_slug: str, component: str, digest: str) -> str:
    repository = _REPO.sub("-", component.lower()).strip("-._") or "image"
    return f"{registry}/vse/{app_slug}/{repository}:{digest}"


class Builder(Protocol):
    def build(
        self, builds: Sequence[Build], *, timeout_secs: float, log_dir: Path
    ) -> dict[str, float]:
        """Build and push every image at once; return each image's build seconds.

        Raises BuildError naming the first image that failed.
        """
        ...


class BuildctlBuilder:
    """buildctl against the BuildKit daemon at `address`, pushing to an insecure registry."""

    def __init__(self, address: str, clock: Clock) -> None:
        self._address = address
        self._clock = clock

    def argv(self, build: Build) -> list[str]:
        return [
            "buildctl",
            "--addr",
            self._address,
            "build",
            "--frontend",
            "dockerfile.v0",
            "--local",
            f"context={build.context}",
            "--local",
            f"dockerfile={build.context}",
            "--output",
            f"type=image,name={build.image},push=true,registry.insecure=true",
        ]

    def build(
        self, builds: Sequence[Build], *, timeout_secs: float, log_dir: Path
    ) -> dict[str, float]:
        log_dir.mkdir(parents=True, exist_ok=True)
        started = self._clock.monotonic()
        running: list[tuple[Build, subprocess.Popen[bytes], IO[bytes], Path]] = []
        try:
            for index, build in enumerate(builds):
                log_path = log_dir / f"build-{index}.log"
                log = log_path.open("wb")
                # lint-waiver: LW-561003 [S603]; buildctl is run with an argv list, never a shell.
                # > The builds run at once as child processes; BuildKit's Go client is not
                # > available from Python, and a shell would only weaken the argv boundary.
                proc = subprocess.Popen(self.argv(build), stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
                running.append((build, proc, log, log_path))
            seconds: dict[str, float] = {}
            for build, proc, _, log_path in running:
                remaining = max(timeout_secs - (self._clock.monotonic() - started), 1.0)
                try:
                    code = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise BuildError(
                        f"build of {build.image} did not finish in {timeout_secs:.0f}s; "
                        f"log tail:\n{_tail(log_path)}"
                    ) from None
                if code != 0:
                    raise BuildError(
                        f"build of {build.image} from {build.context.name} failed "
                        f"(buildctl exit {code}); log tail:\n{_tail(log_path)}"
                    )
                seconds[build.image] = round(self._clock.monotonic() - started, 1)
            return seconds
        finally:
            for _, proc, log, _ in running:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                log.close()


def _tail(path: Path, size: int = 3000) -> str:
    return path.read_bytes()[-size:].decode(errors="replace")
