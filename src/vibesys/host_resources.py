"""SDK for declaring host resources needed by an agent.

This module intentionally contains no agent-specific resource list and no
sandbox or mount logic. Callers use these types to describe resource intent;
policy modules provide the declarations and execution backends decide how to
import them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class HostResourceAccess(StrEnum):
    """Access requested for a host resource."""

    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


@dataclass(frozen=True)
class HostResource:
    """A host path an agent needs, independent of import implementation."""

    path: Path
    access: HostResourceAccess = HostResourceAccess.READ_ONLY
    purpose: str = "caller-provided resource"


@dataclass(frozen=True)
class HostResourceContext:
    """Host facts available to a resource declaration."""

    env: Mapping[str, str]
    binary_path: str | None = None
    provider: str | None = None


HostResourceDeclarer = Callable[[HostResourceContext], Iterable[HostResource]]


def declare_resources(
    context: HostResourceContext,
    declarers: Iterable[HostResourceDeclarer],
    *,
    additional: Iterable[HostResource] = (),
) -> tuple[HostResource, ...]:
    """Evaluate declarations through the SDK without importing any resources."""
    resources = [resource for declarer in declarers for resource in declarer(context)]
    resources.extend(additional)
    return tuple(resources)
