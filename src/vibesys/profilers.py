"""Profiler kinds and resolution policy."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from enum import StrEnum

from vibesys.domains.base import DomainName


class ProfilerKind(StrEnum):
    """Known profiler modes."""

    AUTO = "auto"
    NONE = "none"
    NSYS = "nsys"
    TORCH = "torch"
    NEURON = "neuron"
    MACOS_CPU = "macos_cpu"
    LINUX_CPU = "linux_cpu"


@dataclass(frozen=True)
class ProfilerDefinition:
    """Behavioral declaration for a runnable profiler.

    Packaging follows ``kind.value`` by convention so adding a profiler does
    not require path, prompt, or MCP dispatch changes.
    """

    kind: ProfilerKind
    domains: frozenset[DomainName]
    requires_inprocess: bool = False
    requires_domain_torch_support: bool = False

    @property
    def support_name(self) -> str:
        return f"{self.kind.value}_profiler"

    @property
    def server_path(self) -> str:
        return f"{self.support_name}/server.py"

    @property
    def prompt_template(self) -> str:
        return f"profilers/{self.kind.value}.j2"

    @property
    def mcp_name(self) -> str:
        return f"vibesys-{self.kind.value.replace('_', '-')}-profiler"


PROFILER_DEFINITIONS: dict[ProfilerKind, ProfilerDefinition] = {
    definition.kind: definition
    for definition in (
        ProfilerDefinition(ProfilerKind.NSYS, frozenset({DomainName.LLM_SERVING})),
        ProfilerDefinition(
            ProfilerKind.TORCH,
            frozenset({DomainName.LLM_SERVING}),
            requires_inprocess=True,
            requires_domain_torch_support=True,
        ),
        ProfilerDefinition(ProfilerKind.NEURON, frozenset({DomainName.LLM_SERVING})),
        ProfilerDefinition(ProfilerKind.MACOS_CPU, frozenset({DomainName.GENERIC})),
        ProfilerDefinition(ProfilerKind.LINUX_CPU, frozenset({DomainName.GENERIC})),
    )
}

ACTIVE_PROFILER_KINDS: frozenset[ProfilerKind] = frozenset(PROFILER_DEFINITIONS)

CLI_PROFILER_CHOICES: tuple[ProfilerKind, ...] = tuple(ProfilerKind)


def profiler_definition(kind: ProfilerKind) -> ProfilerDefinition:
    """Return the declaration for a runnable profiler kind."""

    kind = require_profiler_kind(kind)
    try:
        return PROFILER_DEFINITIONS[kind]
    except KeyError as exc:
        raise ValueError(f"Profiler {kind.value!r} is not runnable.") from exc


def coerce_profiler_kind(value: str, *, label: str = "profiler") -> ProfilerKind:
    """Parse a profiler kind and raise a useful error for unknown values."""

    try:
        return ProfilerKind(value)
    except ValueError as exc:
        choices = ", ".join(kind.value for kind in ProfilerKind)
        raise ValueError(f"Unknown {label} kind {value!r}; choose from: {choices}.") from exc


def require_profiler_kind(value: object, *, label: str = "profiler") -> ProfilerKind:
    """Require an already-parsed profiler enum at internal API boundaries."""

    if not isinstance(value, ProfilerKind):
        raise TypeError(f"{label} must be a ProfilerKind, got {type(value).__name__}.")
    return value


def require_domain_name(value: object, *, label: str = "domain") -> DomainName:
    """Require an already-parsed domain enum at internal API boundaries."""

    if not isinstance(value, DomainName):
        raise TypeError(f"{label} must be a DomainName, got {type(value).__name__}.")
    return value


def allowed_profiler_kinds(domain: DomainName) -> frozenset[ProfilerKind]:
    """Profiler kinds allowed by a domain."""

    domain_name = require_domain_name(domain)
    return frozenset(
        {ProfilerKind.NONE}
        | {
            kind
            for kind, definition in PROFILER_DEFINITIONS.items()
            if domain_name in definition.domains
        }
    )


def resolve_profiler_kind(
    requested: ProfilerKind,
    *,
    domain: DomainName,
    backend_profiler_kind: ProfilerKind | None,
    environment_default_profiler_kind: ProfilerKind,
) -> ProfilerKind:
    """Resolve ``--profiler`` into the effective profiler kind.

    ``auto`` is intentionally domain-aware. Generic workloads pick a native CPU
    profiler when the host platform has one; LLM-serving workloads pick the
    backend profiler unless the run environment dictates a remote-safe default
    such as Modal's torch profiler.
    """

    requested_kind = require_profiler_kind(requested, label="requested profiler")
    domain_name = require_domain_name(domain)
    allowed = allowed_profiler_kinds(domain_name)

    if requested_kind is not ProfilerKind.AUTO:
        if requested_kind not in allowed:
            allowed_values = ", ".join(sorted(kind.value for kind in allowed))
            raise ValueError(
                f"Profiler {requested_kind.value!r} is not supported for domain "
                f"{domain_name.value!r}; allowed: {allowed_values}."
            )
        return requested_kind

    if domain_name is DomainName.GENERIC:
        system = platform.system()
        if system == "Darwin":
            return ProfilerKind.MACOS_CPU
        if system == "Linux":
            return ProfilerKind.LINUX_CPU
        return ProfilerKind.NONE

    if allowed == frozenset({ProfilerKind.NONE}):
        return ProfilerKind.NONE

    environment_default = require_profiler_kind(
        environment_default_profiler_kind,
        label="environment default profiler",
    )
    backend_profiler = (
        require_profiler_kind(backend_profiler_kind, label="backend profiler")
        if backend_profiler_kind is not None
        else None
    )

    # Modal runs capture on remote GPUs through torch.profiler; prefer the run
    # environment's torch default over a local CUDA backend's nsys preference.
    if environment_default is ProfilerKind.TORCH:
        candidate = ProfilerKind.TORCH
    elif backend_profiler in ACTIVE_PROFILER_KINDS:
        candidate = backend_profiler
    else:
        candidate = environment_default

    if candidate not in allowed:
        allowed_values = ", ".join(sorted(kind.value for kind in allowed))
        raise ValueError(
            f"Resolved profiler {candidate.value!r} is not supported for domain "
            f"{domain_name.value!r}; allowed: {allowed_values}."
        )
    return candidate
