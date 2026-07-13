"""Profiler kinds and resolution policy."""

from __future__ import annotations

from enum import StrEnum

from vibe_serve.domains.base import DomainName


class ProfilerKind(StrEnum):
    """Known profiler modes."""

    AUTO = "auto"
    NONE = "none"
    NSYS = "nsys"
    TORCH = "torch"
    NEURON = "neuron"


ACTIVE_PROFILER_KINDS: frozenset[ProfilerKind] = frozenset(
    {ProfilerKind.NSYS, ProfilerKind.TORCH, ProfilerKind.NEURON}
)

CLI_PROFILER_CHOICES: tuple[ProfilerKind, ...] = tuple(ProfilerKind)

_DOMAIN_ALLOWED_PROFILERS: dict[DomainName, frozenset[ProfilerKind]] = {
    DomainName.GENERIC: frozenset({ProfilerKind.NONE}),
    DomainName.LLM_SERVING: frozenset(
        {
            ProfilerKind.NONE,
            ProfilerKind.NSYS,
            ProfilerKind.TORCH,
            ProfilerKind.NEURON,
        }
    ),
}


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
    return _DOMAIN_ALLOWED_PROFILERS[domain_name]


def resolve_profiler_kind(
    requested: ProfilerKind,
    *,
    domain: DomainName,
    backend_profiler_kind: ProfilerKind | None,
    environment_default_profiler_kind: ProfilerKind,
) -> ProfilerKind:
    """Resolve ``--profiler`` into the effective profiler kind.

    ``auto`` is intentionally domain-aware. Generic workloads default to no
    profiler; LLM-serving workloads pick the backend profiler unless the run
    environment dictates a remote-safe default such as Modal's torch profiler.
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
