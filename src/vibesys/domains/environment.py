"""Shared environment setup/teardown interfaces for registered domains."""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import Protocol


class RunEnvironmentCapabilities(Protocol):  # noqa: D101  # tracked: #288
    isolated: bool
    materialize_local_model_weights: bool
    # True when the environment's own remote execution surface already has
    # access to model weights (e.g. persistent cluster storage) independent
    # of anything the framework stages or downloads locally. Domain hooks
    # must not require local weights, or a meta.json to derive them, when
    # this is set: the candidate transfer deliberately excludes weights for
    # these environments, so a local materialization would be unused.
    provides_remote_model_weights: bool


@dataclass(frozen=True)
class EnvironmentContext:  # noqa: D101  # tracked: #288
    reference_path: Path
    workspace: Path
    run_environment: RunEnvironmentCapabilities
    project_root: Path
    model_cache_dir: Path
    runtime_artifact_dir: Path
    log: Callable[[str], None]


@dataclass(frozen=True)
class EnvironmentBindMount:  # noqa: D101  # tracked: #288
    host_path: Path
    container_path: str
    read_only: bool = True


@dataclass(frozen=True)
class EnvironmentPatch:  # noqa: D101  # tracked: #288
    copy_excludes: frozenset[str] = frozenset()
    bind_mounts: tuple[EnvironmentBindMount, ...] = ()


class EnvironmentHooks(Protocol):  # noqa: D101  # tracked: #288
    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch: ...  # noqa: D102  # tracked: #288

    def teardown(self, ctx: EnvironmentContext) -> None: ...  # noqa: D102  # tracked: #288


class NoopEnvironmentHooks:  # noqa: D101  # tracked: #288
    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch:  # noqa: ARG002, D102  # tracked: #288
        return EnvironmentPatch()

    def teardown(self, ctx: EnvironmentContext) -> None:  # noqa: ARG002, D102  # tracked: #288
        return None
