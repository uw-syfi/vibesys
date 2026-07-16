"""Shared environment setup/teardown interfaces for registered domains."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class RunEnvironmentCapabilities(Protocol):
    isolated: bool
    materialize_local_model_weights: bool


@dataclass(frozen=True)
class EnvironmentContext:
    reference_path: Path
    workspace: Path
    run_environment: RunEnvironmentCapabilities
    project_root: Path
    log: Callable[[str], None]


@dataclass(frozen=True)
class EnvironmentBindMount:
    host_path: Path
    container_path: str
    read_only: bool = True


@dataclass(frozen=True)
class EnvironmentPatch:
    copy_excludes: frozenset[str] = frozenset()
    bind_mounts: tuple[EnvironmentBindMount, ...] = ()


class EnvironmentHooks(Protocol):
    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch: ...

    def teardown(self, ctx: EnvironmentContext) -> None: ...


class NoopEnvironmentHooks:
    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch:
        return EnvironmentPatch()

    def teardown(self, ctx: EnvironmentContext) -> None:
        return None
