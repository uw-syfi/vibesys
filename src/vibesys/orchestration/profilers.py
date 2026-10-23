"""Profiler kinds, resolution policy, and structured replies."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vibesys.config import BUNDLED_RESOURCES
from vibesys.constants import ComputeBackend, DomainName
from vibesys.run.contracts import ProfilerKind
from vs_core.api.search import markdown_prose

if TYPE_CHECKING:
    from collections.abc import Callable


class UnsupportedProfilerError(ValueError):
    """The selected domain cannot use the configured profiler."""

    def __init__(self) -> None:
        """Report missing Torch profiler support."""
        super().__init__("selected domain does not provide Torch profiler support")


_BACKEND_PROFILERS: dict[ComputeBackend, ProfilerKind] = {
    ComputeBackend.CUDA: ProfilerKind.NSYS,
    ComputeBackend.METAL: ProfilerKind.TORCH,
    ComputeBackend.TRAINIUM: ProfilerKind.NEURON,
    ComputeBackend.ROCM: ProfilerKind.ROCPROF,
    ComputeBackend.CPU: ProfilerKind.LINUX_CPU,
}


@dataclass(frozen=True)
class ProfilerChoice:
    """The resolved profiler, and why ``auto`` chose ``none`` when it did."""

    kind: ProfilerKind
    reason: str | None = None


def default_profiler_for_backend(backend: ComputeBackend) -> ProfilerKind:
    """Return the application policy default for one compute stack."""
    return _BACKEND_PROFILERS[backend]


# Kernel-writing ``auto`` picks a kernel-level profiler; backends without one
# resolve to none.
_KERNEL_WRITING_PROFILERS: dict[ComputeBackend, ProfilerKind] = {
    ComputeBackend.CUDA: ProfilerKind.NCU,
    ComputeBackend.ROCM: ProfilerKind.ROCPROF,
    ComputeBackend.TRAINIUM: ProfilerKind.NEURON,
}


class ProfilerSite(StrEnum):
    """The place in a run where a profiler's tool executes.

    The values are the run environment's own site names (``vs_runtime`` ``ToolSite``); a
    test keeps the two enums identical, because policy cannot import the runtime.
    """

    AGENT_SHELL = "agent_shell"
    GPU_WORKLOAD = "gpu_workload"


@dataclass(frozen=True)
class ProfilerTool:
    """The executable a profiler drives and the site of the run where it executes.

    Run start asks the selected run environment whether it can run ``executable`` at
    ``site``; a profiler whose tool cannot run there is refused (or, for ``auto``, not
    chosen) with an error naming both, instead of being offered and failing in every turn.
    """

    executable: str
    site: ProfilerSite


@dataclass(frozen=True)
class ProfilerDefinition:
    """Behavioral declaration for a runnable profiler.

    Packaging follows ``kind.value`` by convention so adding a profiler does
    not require path, prompt, or MCP dispatch changes.
    """

    kind: ProfilerKind
    domains: frozenset[DomainName]
    backends: frozenset[ComputeBackend] | None = None
    requires_domain_torch_support: bool = False
    # Other profiler kinds whose support directories are staged alongside
    # this one's, e.g. rocprof also exposes the torch analyzer's tools, so
    # rocprof stages torch_profiler/ too. Each entry must be a runnable
    # ProfilerKind with its own ProfilerDefinition; the staged sibling
    # directory name is that definition's own ``support_name``.
    extra_support_kinds: frozenset[ProfilerKind] = frozenset()
    # The executable the profiler needs and where it runs; ``None`` for a profiler that
    # is pure Python on the agent's interpreter and needs no external program.
    tool: ProfilerTool | None = None
    # Tools of the profiler's MCP server that need a GPU driver in the process serving
    # them (the editor). They are offered only when the run environment says the editor
    # has a GPU, or when capture is routed elsewhere (remote); report-reading tools do not
    # belong here.
    driver_tools: frozenset[str] = frozenset()

    @property
    def support_name(self) -> str:
        """Return the packaged support directory name for this profiler."""
        return f"{self.kind.value}_profiler"

    @property
    def prompt_template(self) -> str:
        """Return the prompt template path for this profiler."""
        return f"profilers/{self.kind.value}.j2"

    @property
    def mcp_name(self) -> str:
        """Return the MCP server name registered for this profiler."""
        return f"vibesys-{self.kind.value.replace('_', '-')}-profiler"


def profiler_support_extra(definition: ProfilerDefinition) -> tuple[tuple[str, str], ...]:
    """Return shared and profiler-specific sibling support directories."""
    extra: list[tuple[str, str]] = []
    common_dir = BUNDLED_RESOURCES.directory("profilers", "_common")
    if common_dir is not None:
        extra.append((str(common_dir), PROFILERS_COMMON_STAGED_NAME))
    for extra_kind in sorted(definition.extra_support_kinds):
        extra_definition = profiler_definition(extra_kind)
        extra_dir = BUNDLED_RESOURCES.directory("profilers", extra_kind.value)
        if extra_dir is not None:
            extra.append((str(extra_dir), extra_definition.support_name))
    return tuple(extra)


@dataclass(frozen=True)
class ProfilerPreflightResult:
    """Result of cheap host checks for a resolved profiler."""

    kind: ProfilerKind
    usable: bool
    diagnostics: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    def error_message(self) -> str:
        """Format the preflight diagnostics for an unusable profiler."""
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
        ProfilerDefinition(
            ProfilerKind.NSYS,
            frozenset({DomainName.LLM_SERVING}),
            tool=ProfilerTool("nsys", ProfilerSite.GPU_WORKLOAD),
        ),
        ProfilerDefinition(
            ProfilerKind.NCU,
            frozenset({DomainName.KERNEL_WRITING}),
            backends=frozenset({ComputeBackend.CUDA}),
            tool=ProfilerTool("ncu", ProfilerSite.GPU_WORKLOAD),
            driver_tools=frozenset({"query_metrics"}),
        ),
        # The rocprof MCP server also exposes the torch analyzer's tools
        # (torch.profiler traces are a useful cross-check alongside rocprofv3
        # captures), so torch_profiler/ is staged alongside rocprof_profiler/.
        ProfilerDefinition(
            ProfilerKind.ROCPROF,
            frozenset({DomainName.LLM_SERVING, DomainName.KERNEL_WRITING}),
            extra_support_kinds=frozenset({ProfilerKind.TORCH}),
            tool=ProfilerTool("rocprofv3", ProfilerSite.GPU_WORKLOAD),
            driver_tools=frozenset(
                {
                    "profiling_capabilities",
                    "profile_configured_timeline",
                    "submit_configured_timeline",
                    "capture_status",
                    "await_capture",
                    "cancel_capture",
                    "profile_timeline",
                    "profile_counters",
                    "profile_kernel_deep",
                    "profile_instructions",
                    "profile_ops",
                    "start_target",
                    "stop_target",
                    "targets",
                }
            ),
        ),
        ProfilerDefinition(ProfilerKind.OTEL, frozenset({DomainName.MICROSERVICES})),
        ProfilerDefinition(
            ProfilerKind.TORCH,
            frozenset({DomainName.LLM_SERVING}),
            requires_domain_torch_support=True,
        ),
        # NKI kernels run as NEFFs on NeuronCores, so neuron-explorer captures
        # kernel-writing candidates the same way it captures serving workloads.
        ProfilerDefinition(
            ProfilerKind.NEURON, frozenset({DomainName.LLM_SERVING, DomainName.KERNEL_WRITING})
        ),
        # The native CPU profilers drive a program in the agent's own shell, so they run
        # only where that shell has the program (the host, or an image that ships it).
        ProfilerDefinition(
            ProfilerKind.MACOS_CPU,
            frozenset({DomainName.GENERIC}),
            tool=ProfilerTool("sample", ProfilerSite.AGENT_SHELL),
        ),
        ProfilerDefinition(
            ProfilerKind.LINUX_CPU,
            frozenset({DomainName.GENERIC}),
            tool=ProfilerTool("perf", ProfilerSite.AGENT_SHELL),
        ),
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

# The shared profiler support package is staged with an agent-facing name.
PROFILERS_COMMON_STAGED_NAME = "profilers_common"


def profiler_definition(kind: ProfilerKind) -> ProfilerDefinition:
    """Return the declaration for a runnable profiler kind."""
    kind = require_profiler_kind(kind)
    try:
        return PROFILER_DEFINITIONS[kind]
    except KeyError as exc:
        message = f"Profiler {kind.value!r} is not runnable."
        raise ValueError(message) from exc


def coerce_profiler_kind(value: str, *, label: str = "profiler") -> ProfilerKind:
    """Parse a profiler kind and raise a useful error for unknown values."""
    try:
        return ProfilerKind(value)
    except ValueError as exc:
        choices = ", ".join(kind.value for kind in ProfilerKind)
        message = f"Unknown {label} kind {value!r}; choose from: {choices}."
        raise ValueError(message) from exc


def require_profiler_kind(value: object, *, label: str = "profiler") -> ProfilerKind:
    """Require an already-parsed profiler enum at internal API boundaries."""
    if not isinstance(value, ProfilerKind):
        message = f"{label} must be a ProfilerKind, got {type(value).__name__}."
        raise TypeError(message)
    return value


def require_domain_name(value: object, *, label: str = "domain") -> DomainName:
    """Require an already-parsed domain enum at internal API boundaries."""
    if not isinstance(value, DomainName):
        message = f"{label} must be a DomainName, got {type(value).__name__}."
        raise TypeError(message)
    return value


def allowed_profiler_kinds(domain: DomainName, backend: ComputeBackend) -> frozenset[ProfilerKind]:
    """Profiler kinds allowed by a domain and compute backend."""
    domain_name = require_domain_name(domain)
    if not isinstance(backend, ComputeBackend):
        message = f"backend must be a ComputeBackend, got {type(backend).__name__}."
        raise TypeError(message)
    return frozenset(
        {ProfilerKind.NONE}
        | {
            kind
            for kind, definition in PROFILER_DEFINITIONS.items()
            if domain_name in definition.domains
            and (definition.backends is None or backend in definition.backends)
        }
    )


def _resolve_generic_profiler(
    allowed: frozenset[ProfilerKind],
    environment_default: ProfilerKind,
    environment_supported: frozenset[ProfilerKind] | None,
    backend: ComputeBackend,
) -> ProfilerChoice:
    """Pick a host-native profiler when the backend and the environment fit it.

    The native profilers (``perf``, Instruments) see CPU work only, so they fit the CPU
    backend alone: on an accelerator backend the workload that matters runs on the device,
    and a CPU profile of it is empty.
    """
    if backend is not ComputeBackend.CPU:
        return ProfilerChoice(
            ProfilerKind.NONE,
            f"the generic domain's native CPU profilers do not fit the {backend.value} backend",
        )
    system = platform.system()
    if system == "Darwin":
        candidate = ProfilerKind.MACOS_CPU
    elif system == "Linux":
        candidate = ProfilerKind.LINUX_CPU
    else:
        return ProfilerChoice(ProfilerKind.NONE, f"no native CPU profiler exists for {system}")
    if environment_supported is None or candidate in environment_supported:
        return ProfilerChoice(candidate)
    # Remote or constrained environments may not expose a host profiler.
    if environment_default in allowed and environment_default in environment_supported:
        return ProfilerChoice(environment_default)
    if ProfilerKind.NONE in environment_supported:
        return ProfilerChoice(
            ProfilerKind.NONE,
            f"the selected run environment does not support {candidate.value!r}",
        )
    supported_values = ", ".join(sorted(kind.value for kind in environment_supported))
    message = (
        "No profiler supported by both the generic domain and selected run environment; "
        f"environment allows: {supported_values}."
    )
    raise ValueError(message)


def resolve_profiler_kind(
    requested: ProfilerKind,
    *,
    domain: DomainName,
    backend: ComputeBackend,
    environment_default_profiler_kind: ProfilerKind,
    environment_supported_profiler_kinds: frozenset[ProfilerKind] | None = None,
) -> ProfilerKind:
    """Resolve ``--profiler`` into the effective profiler kind (see ``resolve_profiler_choice``)."""
    return resolve_profiler_choice(
        requested,
        domain=domain,
        backend=backend,
        environment_default_profiler_kind=environment_default_profiler_kind,
        environment_supported_profiler_kinds=environment_supported_profiler_kinds,
    ).kind


def resolve_profiler_choice(
    requested: ProfilerKind,
    *,
    domain: DomainName,
    backend: ComputeBackend,
    environment_default_profiler_kind: ProfilerKind,
    environment_supported_profiler_kinds: frozenset[ProfilerKind] | None = None,
) -> ProfilerChoice:
    """Resolve ``--profiler`` into the effective profiler kind and, for ``auto``, the reason for none.

    ``auto`` is domain-aware. Generic workloads pick a native CPU profiler when
    the host platform has one; kernel-writing runs pick NCU on CUDA, rocprof
    on ROCm and neuron on Trainium when the run environment supports it; LLM-serving runs use their
    backend profiler unless the run environment dictates another safe default.
    """
    requested_kind = require_profiler_kind(requested, label="requested profiler")
    domain_name = require_domain_name(domain)
    allowed = allowed_profiler_kinds(domain_name, backend)

    if requested_kind is not ProfilerKind.AUTO:
        if requested_kind not in allowed:
            allowed_values = ", ".join(sorted(kind.value for kind in allowed))
            _exception_message_3 = (
                f"Profiler {requested_kind.value!r} is not supported for domain "
                f"{domain_name.value!r} on backend {backend.value!r}; allowed: {allowed_values}."
            )
            raise ValueError(_exception_message_3)
        if (
            environment_supported_profiler_kinds is not None
            and requested_kind not in environment_supported_profiler_kinds
        ):
            supported_values = ", ".join(
                sorted(kind.value for kind in environment_supported_profiler_kinds)
            )
            _exception_message_4 = f"Profiler {requested_kind.value!r} is not supported by the selected run environment; allowed: {supported_values}."
            raise ValueError(_exception_message_4)
        return ProfilerChoice(requested_kind)

    choice = _resolve_auto_profiler(
        domain_name,
        backend,
        allowed,
        environment_default_profiler_kind,
        environment_supported_profiler_kinds,
    )
    if choice.kind is ProfilerKind.NONE and choice.reason is None:
        return ProfilerChoice(
            ProfilerKind.NONE, "the selected run environment's default profiler is none"
        )
    return choice


def _resolve_auto_profiler(
    domain: DomainName,
    backend: ComputeBackend,
    allowed: frozenset[ProfilerKind],
    environment_default: ProfilerKind,
    environment_supported: frozenset[ProfilerKind] | None,
) -> ProfilerChoice:
    if domain is DomainName.GENERIC:
        return _resolve_generic_profiler(
            allowed, environment_default, environment_supported, backend
        )

    # OTel requires an instrumented input bundle; bare microservices use none.
    if domain is DomainName.MICROSERVICES:
        return ProfilerChoice(
            ProfilerKind.NONE, "microservices profiling needs an instrumented input"
        )

    if domain is DomainName.KERNEL_WRITING:
        candidate = _KERNEL_WRITING_PROFILERS.get(backend, ProfilerKind.NONE)
        if candidate in allowed and (
            environment_supported is None or candidate in environment_supported
        ):
            return ProfilerChoice(candidate)
        return ProfilerChoice(
            ProfilerKind.NONE,
            f"no kernel-writing profiler fits the {backend.value} backend in the selected "
            "run environment",
        )

    if allowed == frozenset({ProfilerKind.NONE}):
        return ProfilerChoice(
            ProfilerKind.NONE,
            f"the {domain.value} domain has no profiler for the {backend.value} backend",
        )

    return ProfilerChoice(
        _resolve_environment_profiler(
            domain, backend, allowed, environment_default, environment_supported
        )
    )


def _resolve_environment_profiler(
    domain: DomainName,
    backend: ComputeBackend,
    allowed: frozenset[ProfilerKind],
    environment_default: ProfilerKind,
    environment_supported: frozenset[ProfilerKind] | None,
) -> ProfilerKind:
    environment_default = require_profiler_kind(
        environment_default, label="environment default profiler"
    )
    backend_profiler = default_profiler_for_backend(backend)
    # Prefer native capture when the environment supports it; otherwise use
    # the environment's declared capture path (for example, Modal uses Torch).
    if environment_supported is None or backend_profiler in environment_supported:
        candidate = backend_profiler
    else:
        candidate = environment_default

    if candidate not in allowed:
        allowed_values = ", ".join(sorted(kind.value for kind in allowed))
        message = f"Resolved profiler {candidate.value!r} is not supported for domain {domain.value!r}; allowed: {allowed_values}."
        raise ValueError(message)
    if environment_supported is not None and candidate not in environment_supported:
        supported_values = ", ".join(sorted(kind.value for kind in environment_supported))
        message = f"Resolved profiler {candidate.value!r} is not supported by the selected run environment; allowed: {supported_values}."
        raise ValueError(message)
    return candidate


def preflight_profiler_kind(
    kind: ProfilerKind,
    *,
    native_preflight: Callable[[ProfilerKind], ProfilerPreflightResult] | None = None,
) -> ProfilerPreflightResult:
    """Run cheap local checks for a resolved profiler.

    Most profiler kinds are validated by their backend/runtime setup. Native CPU
    profilers run on the local host, so check their command availability before
    the optimization loop starts.
    """
    resolved = require_profiler_kind(kind)
    if resolved is ProfilerKind.NONE:
        return ProfilerPreflightResult(kind=resolved, usable=True)
    if resolved in {ProfilerKind.LINUX_CPU, ProfilerKind.MACOS_CPU}:
        if native_preflight is None:
            message = f"native_preflight is required for profiler {resolved.value!r}."
            raise ValueError(message)
        return native_preflight(resolved)
    return ProfilerPreflightResult(kind=resolved, usable=True)


class ProfilerSummary(BaseModel):
    """Structured summary from the profiler agent, shared with the orchestrator."""

    model_config = ConfigDict(extra="forbid")

    analysis: str = Field(
        description=markdown_prose("Detailed interpretation of the profile data.")
    )
    bottlenecks: str = Field(
        description=markdown_prose("Ranked bottlenecks with concrete numbers.")
    )
    suggestions: str = Field(
        description=markdown_prose(
            "Advisory optimization or measurement suggestions tied to bottlenecks; "
            "they do not impose a planning prerequisite."
        )
    )
    perf_metric: FiniteFloat | None = Field(
        default=None,
        description=(
            "Uninverted primary performance metric from a fresh trusted uninstrumented "
            "benchmark, not profiler timings. "
            "The configured primary objective determines whether lower or higher is better. "
            "Without configured objectives, scalar selection assumes higher is better. "
            "None when unavailable."
        ),
    )
    perf_unit: str | None = Field(
        default=None,
        description=(
            "Unit or objective-declared benchmark field name of perf_metric, as required "
            "by the configured profiling workflow. None when perf_metric is None."
        ),
    )
    metrics: dict[str, FiniteFloat] = Field(
        default_factory=dict,
        description=(
            "Optional multi-metric dict keyed by metric name (e.g. "
            "{'median_tok_per_sec': 42.1, 'p99_latency_ms': 87.3}). Used by "
            "the evolve loop's Pareto-frontier selection. Single-objective "
            "consumers (agent-loop plateau detection) ignore this field; "
            "they read perf_metric instead."
        ),
    )


__all__ = [
    "ACTIVE_PROFILER_KINDS",
    "CLI_PROFILER_CHOICES",
    "PROFILERS_COMMON_STAGED_NAME",
    "PROFILER_DEFINITIONS",
    "ProfilerChoice",
    "ProfilerDefinition",
    "ProfilerKind",
    "ProfilerPreflightResult",
    "ProfilerSite",
    "ProfilerSummary",
    "ProfilerTool",
    "UnsupportedProfilerError",
    "allowed_profiler_kinds",
    "coerce_profiler_kind",
    "default_profiler_for_backend",
    "preflight_profiler_kind",
    "profiler_definition",
    "profiler_support_extra",
    "require_domain_name",
    "require_profiler_kind",
    "resolve_profiler_choice",
    "resolve_profiler_kind",
]
