"""SDK for declaring host resources needed inside a sandbox.

This module intentionally contains no agent-specific resource list and no
sandbox or mount logic. Callers use these types to describe resource intent;
policy modules provide the declarations and execution backends decide how to
import them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path  # noqa: TC003  # tracked: #288


class HostResourceAccess(StrEnum):
    """Access requested for a host resource."""

    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


@dataclass(frozen=True)
class HostResource:
    """A host path an agent needs, independent of import implementation.

    ``agent_path`` is the path the confined process sees for this resource,
    when it differs from ``path`` on the host. Leave it ``None`` for anything
    imported at its own host path; only a resource a framework presents at a
    fixed container path (for example a container's ``/workspace`` or an
    ``/opt/vibesys-*`` toolchain mount) should set it. Host confinement
    backends run the agent directly against the host filesystem and cannot
    remap a resource: a mismatched ``agent_path`` on a host backend is a
    caller error, rejected where the resource list is applied.
    """

    path: Path
    access: HostResourceAccess = HostResourceAccess.READ_ONLY
    purpose: str = "caller-provided resource"
    agent_path: str | None = None


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
