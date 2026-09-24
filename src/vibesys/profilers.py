"""Profiler kinds and resolution policy."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from enum import StrEnum

from vibesys.constants import DomainName


class ProfilerKind(StrEnum):
    """Known profiler modes."""

    AUTO = "auto"
    NONE = "none"
    NSYS = "nsys"
    ROCPROF = "rocprof"
    OTEL = "otel"
    TORCH = "torch"
    NEURON = "neuron"
    MACOS_CPU = "macos_cpu"
    LINUX_CPU = "linux_cpu"
    HEADROOM = "headroom"


@dataclass(frozen=True)
class ProfilerDefinition:
    """Behavioral declaration for a runnable profiler.

    Packaging follows ``kind.value`` by convention so adding a profiler does
    not require path, prompt, or MCP dispatch changes.
    """

    kind: ProfilerKind
    domains: frozenset[DomainName]
    requires_domain_torch_support: bool = False
    # Other profiler kinds whose support directories are staged alongside
    # this one's, e.g. rocprof also exposes the torch analyzer's tools, so
    # rocprof stages torch_profiler/ too. Each entry must be a runnable
    # ProfilerKind with its own ProfilerDefinition; the staged sibling
    # directory name is that definition's own ``support_name``.
    extra_support_kinds: frozenset[ProfilerKind] = frozenset()

    @property
    def support_name(self) -> str:  # noqa: D102  # tracked: #288
        return f"{self.kind.value}_profiler"

    @property
    def server_path(self) -> str:  # noqa: D102  # tracked: #288
        return f"{self.support_name}/server.py"

    @property
    def prompt_template(self) -> str:  # noqa: D102  # tracked: #288
        return f"profilers/{self.kind.value}.j2"

    @property
    def mcp_name(self) -> str:  # noqa: D102  # tracked: #288
        return f"vibesys-{self.kind.value.replace('_', '-')}-profiler"


@dataclass(frozen=True)
class ProfilerPreflightResult:
    """Result of cheap host checks for a resolved profiler."""

    kind: ProfilerKind
    usable: bool
    diagnostics: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    def error_message(self) -> str:  # noqa: D102  # tracked: #288
        diagnostic_text = ", ".join(self.diagnostics) or "unknown"
        detail_text = "; ".join(self.details)
        suffix = f" ({detail_text})" if detail_text else ""
        return (
            f"Resolved profiler {self.kind.value!r} is not usable on this host: "
            f"{diagnostic_text}{suffix}."
        )


PROFILER_DEFINITIONS: dict[ProfilerKind, ProfilerDefinition] = {
    definition.kind: definition
    for definition in (
        ProfilerDefinition(ProfilerKind.NSYS, frozenset({DomainName.LLM_SERVING})),
        # The rocprof MCP server also exposes the torch analyzer's tools
        # (torch.profiler traces are a useful cross-check alongside rocprofv3
        # captures), so torch_profiler/ is staged alongside rocprof_profiler/.
        ProfilerDefinition(
            ProfilerKind.ROCPROF,
            frozenset({DomainName.LLM_SERVING}),
            extra_support_kinds=frozenset({ProfilerKind.TORCH}),
        ),
        ProfilerDefinition(ProfilerKind.OTEL, frozenset({DomainName.MICROSERVICES})),
        ProfilerDefinition(
            ProfilerKind.TORCH,
            frozenset({DomainName.LLM_SERVING}),
            requires_domain_torch_support=True,
        ),
        ProfilerDefinition(ProfilerKind.NEURON, frozenset({DomainName.LLM_SERVING})),
        ProfilerDefinition(ProfilerKind.MACOS_CPU, frozenset({DomainName.GENERIC})),
        ProfilerDefinition(ProfilerKind.LINUX_CPU, frozenset({DomainName.GENERIC})),
        # Analyzes a target-captured kernel headroom report (observed vs
        # roofline speed-of-light per kernel). Scoped to LLM serving, where
        # kernel-level roofline analysis drives the optimization loop.
        ProfilerDefinition(
            ProfilerKind.HEADROOM,
            frozenset({DomainName.LLM_SERVING}),
        ),
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
        raise ValueError(f"Profiler {kind.value!r} is not runnable.") from exc  # noqa: TRY003  # tracked: #288


def coerce_profiler_kind(value: str, *, label: str = "profiler") -> ProfilerKind:
    """Parse a profiler kind and raise a useful error for unknown values."""
    try:
        return ProfilerKind(value)
    except ValueError as exc:
        choices = ", ".join(kind.value for kind in ProfilerKind)
        raise ValueError(f"Unknown {label} kind {value!r}; choose from: {choices}.") from exc  # noqa: TRY003  # tracked: #288


def require_profiler_kind(value: object, *, label: str = "profiler") -> ProfilerKind:
    """Require an already-parsed profiler enum at internal API boundaries."""
    if not isinstance(value, ProfilerKind):
        raise TypeError(f"{label} must be a ProfilerKind, got {type(value).__name__}.")  # noqa: TRY003  # tracked: #288
    return value


def require_domain_name(value: object, *, label: str = "domain") -> DomainName:
    """Require an already-parsed domain enum at internal API boundaries."""
    if not isinstance(value, DomainName):
        raise TypeError(f"{label} must be a DomainName, got {type(value).__name__}.")  # noqa: TRY003  # tracked: #288
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


def resolve_profiler_kind(  # noqa: C901, PLR0911, PLR0912  # tracked: #288
    requested: ProfilerKind,
    *,
    domain: DomainName,
    backend_profiler_kind: ProfilerKind | None,
    environment_default_profiler_kind: ProfilerKind,
    environment_supported_profiler_kinds: frozenset[ProfilerKind] | None = None,
) -> ProfilerKind:
    """Resolve ``--profiler`` into the effective profiler kind.

    ``auto`` is intentionally domain-aware. Generic workloads pick a native CPU
    profiler when the host platform has one; LLM-serving workloads pick the
    backend profiler unless the run environment dictates another safe default.
    """
    requested_kind = require_profiler_kind(requested, label="requested profiler")
    domain_name = require_domain_name(domain)
    allowed = allowed_profiler_kinds(domain_name)

    if requested_kind is not ProfilerKind.AUTO:
        if requested_kind not in allowed:
            allowed_values = ", ".join(sorted(kind.value for kind in allowed))
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"Profiler {requested_kind.value!r} is not supported for domain "
                f"{domain_name.value!r}; allowed: {allowed_values}."
            )
        if (
            environment_supported_profiler_kinds is not None
            and requested_kind not in environment_supported_profiler_kinds
        ):
            supported_values = ", ".join(
                sorted(kind.value for kind in environment_supported_profiler_kinds)
            )
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"Profiler {requested_kind.value!r} is not supported by the selected "
                f"run environment; allowed: {supported_values}."
            )
        return requested_kind

    if domain_name is DomainName.GENERIC:
        system = platform.system()
        if system == "Darwin":
            candidate = ProfilerKind.MACOS_CPU
        elif system == "Linux":
            candidate = ProfilerKind.LINUX_CPU
        else:
            candidate = ProfilerKind.NONE
        if (
            environment_supported_profiler_kinds is None
            or candidate in environment_supported_profiler_kinds
        ):
            return candidate
        # A remote or otherwise constrained environment may not expose the
        # host-native profiler. Prefer its declared default when the domain
        # supports it, then degrade cleanly to no profiler.
        if (
            environment_default_profiler_kind in allowed
            and environment_default_profiler_kind in environment_supported_profiler_kinds
        ):
            return environment_default_profiler_kind
        if ProfilerKind.NONE in environment_supported_profiler_kinds:
            return ProfilerKind.NONE
        supported_values = ", ".join(
            sorted(kind.value for kind in environment_supported_profiler_kinds)
        )
        raise ValueError(  # noqa: TRY003  # tracked: #288
            "No profiler supported by both the generic domain and selected run "
            f"environment; environment allows: {supported_values}."
        )

    # OTel requires an input bundle that provisions instrumentation and a
    # collector. Keep microservice defaults unchanged; users opt in explicitly.
    if domain_name is DomainName.MICROSERVICES:
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

    # Prefer the compute backend's native profiler when the selected execution
    # environment can expose it. Otherwise use the environment's declared
    # capture path. This is capability-based: the shared resolver must not know
    # which profiler a concrete remote provider happens to require.
    if (
        backend_profiler is not None
        and backend_profiler in ACTIVE_PROFILER_KINDS
        and (
            environment_supported_profiler_kinds is None
            or backend_profiler in environment_supported_profiler_kinds
        )
    ):
        candidate = backend_profiler
    else:
        candidate = environment_default

    if candidate not in allowed:
        allowed_values = ", ".join(sorted(kind.value for kind in allowed))
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"Resolved profiler {candidate.value!r} is not supported for domain "
            f"{domain_name.value!r}; allowed: {allowed_values}."
        )
    if (
        environment_supported_profiler_kinds is not None
        and candidate not in environment_supported_profiler_kinds
    ):
        supported_values = ", ".join(
            sorted(kind.value for kind in environment_supported_profiler_kinds)
        )
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"Resolved profiler {candidate.value!r} is not supported by the selected "
            f"run environment; allowed: {supported_values}."
        )
    return candidate


def preflight_profiler_kind(kind: ProfilerKind) -> ProfilerPreflightResult:
    """Run cheap local checks for a resolved profiler.

    Most profiler kinds are validated by their backend/runtime setup. Native CPU
    profilers run on the local host, so check their command availability before
    the optimization loop starts.
    """
    resolved = require_profiler_kind(kind)
    if resolved is ProfilerKind.NONE:
        return ProfilerPreflightResult(resolved, True)  # noqa: FBT003  # tracked: #288
    if resolved is ProfilerKind.LINUX_CPU:
        from vibesys.linux_cpu_profiler import (  # noqa: PLC0415
            DiagnosticCode,
            LinuxProfilerTool,
            detect_capability,
        )

        capability = detect_capability()
        blocking = {
            DiagnosticCode.NOT_LINUX,
            DiagnosticCode.PERF_UNAVAILABLE,
            DiagnosticCode.PERF_STAT_UNAVAILABLE,
        }
        diagnostics = tuple(item.value for item in capability.diagnostics)
        usable = capability.tool is LinuxProfilerTool.PERF and not any(
            item in blocking for item in capability.diagnostics
        )
        details = (
            f"perf_path={capability.perf_path or 'missing'}",
            f"perf_event_paranoid={capability.perf_event_paranoid}",
            f"kptr_restrict={capability.kptr_restrict}",
        )
        return ProfilerPreflightResult(resolved, usable, diagnostics, details)
    if resolved is ProfilerKind.MACOS_CPU:
        from vibesys.macos_cpu_profiler import (  # noqa: PLC0415
            MacOSProfilerTool,
            detect_capability,
        )

        capability = detect_capability()
        diagnostics = tuple(item.value for item in capability.diagnostics)
        usable = capability.tool is not MacOSProfilerTool.NONE
        details = (
            f"xcode_path={capability.xcode_path or 'missing'}",
            f"xctrace_path={capability.xctrace_path or 'missing'}",
            f"sample_path={capability.sample_path or 'missing'}",
        )
        return ProfilerPreflightResult(resolved, usable, diagnostics, details)
    return ProfilerPreflightResult(resolved, True)  # noqa: FBT003  # tracked: #288
