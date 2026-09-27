"""SDK for declaring and normalizing host resources needed inside a sandbox.

This module intentionally contains no agent-specific resource list or import
effects. Callers use these types to describe resource intent; policy modules
provide declarations and execution backends decide how to import them. The
mount helper is the compatibility boundary for callers still assembling
``(host, container, read-only)`` declarations.
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
class EnvironmentBindMount:
    """Host-to-container path requested by product environment composition."""

    host_path: Path
    container_path: str
    read_only: bool = True


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


def host_resource_for_mount(
    host_path: str | Path,
    container_path: str,
    *,
    read_only: bool,
    purpose: str = "container mount",
) -> HostResource:
    """Lower one container mount declaration to a host resource.

    An identical host and container path uses the resource's identity mapping;
    otherwise the container destination is recorded explicitly for sandbox
    import and agent-path translation.
    """
    path = Path(host_path)
    access = HostResourceAccess.READ_ONLY if read_only else HostResourceAccess.READ_WRITE
    agent_path = container_path if container_path != str(path) else None
    return HostResource(path, access, purpose, agent_path)


def deduplicate_host_resources(
    resources: Iterable[HostResource],
) -> tuple[HostResource, ...]:
    """Keep the last resource declared for each sandbox-visible path.

    Replacing a declaration does not move its position. This preserves the
    historical mount ordering while allowing a later, more specific access
    declaration to override an earlier one.
    """
    by_agent_path: dict[str, HostResource] = {}
    for resource in resources:
        key = resource.agent_path if resource.agent_path is not None else str(resource.path)
        by_agent_path[key] = resource
    return tuple(by_agent_path.values())
