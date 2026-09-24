"""Shared environment setup/teardown interfaces for registered domains."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class RunEnvironmentCapabilities(Protocol):
    """Runtime properties a domain needs from the run environment."""

    isolated: bool
    materialize_local_model_weights: bool


@dataclass(frozen=True)
class EnvironmentContext:
    """Inputs and run-scoped paths supplied to domain environment hooks."""

    reference_path: Path
    workspace: Path
    run_environment: RunEnvironmentCapabilities
    project_root: Path
    model_cache_dir: Path
    runtime_artifact_dir: Path
    log: Callable[[str], None]


@dataclass(frozen=True)
class EnvironmentBindMount:
    """Read-only or writable host-to-container path mapping."""

    host_path: Path
    container_path: str
    read_only: bool = True


@dataclass(frozen=True)
class EnvironmentPatch:
    """Additional copy exclusions and mounts requested by a domain."""

    copy_excludes: frozenset[str] = frozenset()
    bind_mounts: tuple[EnvironmentBindMount, ...] = ()


class EnvironmentHooks(Protocol):
    """Domain-owned setup and teardown around the sandbox lifecycle."""

    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch:
        """Return the extra filesystem policy needed by this domain."""
        ...

    def teardown(self, ctx: EnvironmentContext) -> None:
        """Release domain resources after the sandbox has finished."""
        ...


class NoopEnvironmentHooks:
    """Environment hooks implementation for domains with no setup needs."""

    # lint-waiver: LW-007063 [ARG002]; the protocol requires context for domain implementations
    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch:  # noqa: ARG002
        """Return an empty patch."""
        return EnvironmentPatch()

    # lint-waiver: LW-007064 [ARG002]; the protocol requires context for domain implementations
    def teardown(self, ctx: EnvironmentContext) -> None:  # noqa: ARG002
        """Complete teardown without any domain-owned resources."""
        return
