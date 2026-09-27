"""Composition-only factories for production runtime infrastructure.

Orchestration plugins use :mod:`vs_runtime.api`, not this module. VibeSys
composition imports this factory to bind lower-library effects to the private
runtime implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

from vs_agent.api import AgentCapabilities, AgentSessionKey, ToolServerDescriptor
from vs_runtime._agent_sessions import RuntimeAgentSessions
from vs_runtime._bundled_paths import resolve_bundled_tree, resolve_packaged_tree
from vs_runtime._checkpoint import (
    CompletedRound,
    MultiSlotRoundTransaction,
    MultiSlotRoundTransactionCoordinator,
    RoundRecoveryOutcome,
    RoundTransactionError,
)
from vs_runtime._input_project import InputDependency, materialize_input_project
from vs_runtime._linux_cpu_profiler import (
    Capability as LinuxProfilerCapability,
)
from vs_runtime._linux_cpu_profiler import (
    CollectionResult as LinuxProfileResult,
)
from vs_runtime._linux_cpu_profiler import (
    DiagnosticCode as LinuxProfilerDiagnostic,
)
from vs_runtime._linux_cpu_profiler import LinuxProfilerEffects, LinuxProfilerTool
from vs_runtime._linux_cpu_profiler import collect as collect_linux_profile
from vs_runtime._linux_cpu_profiler import detect_capability as detect_linux_profiler
from vs_runtime._linux_cpu_profiler import parse_command as parse_profile_command
from vs_runtime._linux_cpu_profiler import summarize as summarize_linux_profile
from vs_runtime._local_validation import (
    FrameworkValidationResult,
    LocalValidationEvents,
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    ValidationRecipe,
    run_local_validation,
)
from vs_runtime._macos_cpu_profiler import (
    Capability as MacOSProfilerCapability,
)
from vs_runtime._macos_cpu_profiler import (
    CollectionResult as MacOSProfileResult,
)
from vs_runtime._macos_cpu_profiler import (
    DiagnosticCode as MacOSProfilerDiagnostic,
)
from vs_runtime._macos_cpu_profiler import MacOSProfilerEffects, MacOSProfilerTool
from vs_runtime._macos_cpu_profiler import collect as collect_macos_profile
from vs_runtime._macos_cpu_profiler import detect_capability as detect_macos_profiler
from vs_runtime._model_requests import ModelRequestError, _ModelRequestReconciler
from vs_runtime._run_state import RunState
from vs_runtime._sdk_paths import (
    InputProjectError,
    SDKRoots,
    relative_sdk_source,
    resolve_sdk_source,
)
from vs_runtime._skills import (
    SkillCatalogEntry,
    SkillMetadataError,
    build_skill_catalog,
    discover_skill_dirs,
    load_skill_frontmatter,
    resolve_skill_resources,
)
from vs_runtime.contracts import AgentSessions, Workspace

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from vs_runtime.api import AgentRole


ResponseT = TypeVar("ResponseT", bound=BaseModel)


class ManagedAgentWorkspace(Workspace, Protocol):
    """Composition view of workspace mechanics needed for access enforcement.

    This is not an orchestration-plugin contract. It is removed once workspace
    ownership also resides in ``vs_runtime``.
    """

    @property
    def path(self) -> Path:
        """Return the live host path used by the agent execution adapter."""
        ...

    async def snapshot(self, label: str) -> str:
        """Record the tree before or after one turn."""
        ...

    async def pending_changes(self) -> list[str]:
        """List workspace-relative paths changed since the latest snapshot."""
        ...

    async def restore_for_agent(
        self,
        revision: str,
        *,
        preserve_paths: tuple[str, ...],
    ) -> None:
        """Restore a turn snapshot while preserving only declared grants."""
        ...

    def is_directory(self, path: str) -> bool:
        """Return whether one validated workspace-relative path is a directory."""
        ...


class AgentExecution(Protocol):
    """Temporary composition port for one already-open agent execution.

    The runtime owns sessions through this port while the concrete client and
    sandbox opener still lives in VibeSys. The port is removed when those
    resources move into ``vs_runtime``.
    """

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return execution-system capabilities."""
        ...

    @property
    def backend_name(self) -> str:
        """Return the selected backend name."""
        ...

    @property
    def driver_name(self) -> str | None:
        """Return the selected driver name."""
        ...

    @property
    def provider(self) -> str | None:
        """Return the selected provider."""
        ...

    @property
    def model(self) -> str | None:
        """Return the selected model."""
        ...

    @property
    def reasoning_effort(self) -> str | None:
        """Return the selected reasoning effort."""
        ...

    async def execute(  # noqa: PLR0913  # lint-waiver: LW-837205 [PLR0913]; temporary port mirrors the independently fixed inputs of the existing execution handle and is deleted when that handle moves into vs_runtime.
        self,
        message: str,
        *,
        system_prompt: str,
        response: type[ResponseT] | None,
        label: str,
        session_key: AgentSessionKey,
        tool_servers: tuple[ToolServerDescriptor, ...] | None,
    ) -> str | ResponseT:
        """Execute one turn with fixed session configuration."""
        ...

    async def close(self) -> None:
        """Release the execution resource idempotently."""
        ...


class AgentSessionRuntime(AgentSessions, Protocol):
    """Composition controls for the run-owned production session manager."""

    def invalidate_workspace(self, workspace: Workspace) -> None:
        """Reject new turns before a workspace begins teardown."""
        ...

    def begin_close(self) -> None:
        """Reject new turns before run teardown begins."""
        ...


type AgentExecutionFactory = Callable[[AgentRole, Workspace, str], Awaitable[AgentExecution]]
type ManagedAgentWorkspaceResolver = Callable[[Workspace], ManagedAgentWorkspace]
type AgentToolResolver = Callable[[Workspace], tuple[ToolServerDescriptor, ...]]


def create_agent_session_runtime(
    roles: tuple[AgentRole, ...],
    *,
    open_execution: AgentExecutionFactory,
    resolve_workspace: ManagedAgentWorkspaceResolver,
    tool_bindings: Mapping[str, AgentToolResolver] | None = None,
    log: Callable[[str], None] = print,
) -> AgentSessionRuntime:
    """Create the production owner for explicit orchestration sessions."""
    return RuntimeAgentSessions(
        roles,
        open_execution=open_execution,
        resolve_workspace=resolve_workspace,
        tool_bindings=tool_bindings,
        log=log,
    )


class ModelVolumeProvisioner(Protocol):
    """Ensure one requested model volume and return its stable name."""

    def __call__(
        self,
        model_id: str,
        *,
        revision: str | None = None,
        log: Callable[[str], object] = print,
    ) -> str:
        """Ensure one requested model volume and return its stable name."""
        ...


class ModelRequestReconciler(Protocol):
    """Reconcile candidate model requests without exposing manifest mechanics."""

    def reconcile(
        self,
        workspace: Path,
        *,
        log: Callable[[str], object] = print,
    ) -> tuple[str, ...]:
        """Validate and provision candidate requests in manifest order."""
        ...


class NativeCpuProfilerKind(StrEnum):
    """Native CPU profiler mechanism requested by product composition."""

    LINUX = "linux"
    MACOS = "macos"


@dataclass(frozen=True)
class NativeCpuProfilerPreflight:
    """Host capability facts needed by product profiler policy."""

    selected_tool: str
    usable: bool
    diagnostics: tuple[str, ...]
    details: tuple[str, ...]


def preflight_native_cpu_profiler(
    kind: NativeCpuProfilerKind,
    *,
    detect_linux: Callable[[], LinuxProfilerCapability] = detect_linux_profiler,
    detect_macos: Callable[[], MacOSProfilerCapability] = detect_macos_profiler,
) -> NativeCpuProfilerPreflight:
    """Detect one native CPU profiler and return policy-neutral host facts."""
    if not isinstance(kind, NativeCpuProfilerKind):
        message = f"kind must be a NativeCpuProfilerKind, got {type(kind).__name__}."
        raise TypeError(message)
    if kind is NativeCpuProfilerKind.LINUX:
        capability = detect_linux()
        blocking = {
            LinuxProfilerDiagnostic.NOT_LINUX,
            LinuxProfilerDiagnostic.PERF_UNAVAILABLE,
            LinuxProfilerDiagnostic.PERF_STAT_UNAVAILABLE,
        }
        return NativeCpuProfilerPreflight(
            selected_tool=capability.tool.value,
            usable=capability.tool is LinuxProfilerTool.PERF
            and not any(item in blocking for item in capability.diagnostics),
            diagnostics=tuple(item.value for item in capability.diagnostics),
            details=(
                f"perf_path={capability.perf_path or 'missing'}",
                f"perf_event_paranoid={capability.perf_event_paranoid}",
                f"kptr_restrict={capability.kptr_restrict}",
            ),
        )

    capability = detect_macos()
    return NativeCpuProfilerPreflight(
        selected_tool=capability.tool.value,
        usable=capability.tool is not MacOSProfilerTool.NONE,
        diagnostics=tuple(item.value for item in capability.diagnostics),
        details=(
            f"xcode_path={capability.xcode_path or 'missing'}",
            f"xctrace_path={capability.xctrace_path or 'missing'}",
            f"sample_path={capability.sample_path or 'missing'}",
        ),
    )


def _ensure_model_volume(
    model_id: str,
    *,
    revision: str | None = None,
    log: Callable[[str], object] = print,
) -> str:
    """Load the optional Modal implementation only when reconciliation needs it."""
    ensure_model_volume = import_module("vs_sandbox.api").ensure_model_volume
    return ensure_model_volume(model_id, revision=revision, log=log)


def create_model_request_reconciler(
    *,
    provisioner: ModelVolumeProvisioner = _ensure_model_volume,
    environment: Mapping[str, str] | None = None,
) -> ModelRequestReconciler:
    """Bind model-volume and operator-environment effects once at composition."""
    return _ModelRequestReconciler(provisioner, environment)


__all__ = [
    "AgentExecution",
    "AgentExecutionFactory",
    "AgentSessionRuntime",
    "AgentToolResolver",
    "CompletedRound",
    "FrameworkValidationResult",
    "InputDependency",
    "InputProjectError",
    "LinuxProfileResult",
    "LinuxProfilerCapability",
    "LinuxProfilerDiagnostic",
    "LinuxProfilerEffects",
    "LinuxProfilerTool",
    "LocalValidationEvents",
    "LocalValidationRecipeError",
    "LocalValidationRecipeErrorKind",
    "MacOSProfileResult",
    "MacOSProfilerCapability",
    "MacOSProfilerDiagnostic",
    "MacOSProfilerEffects",
    "MacOSProfilerTool",
    "ManagedAgentWorkspace",
    "ManagedAgentWorkspaceResolver",
    "ModelRequestError",
    "ModelRequestReconciler",
    "ModelVolumeProvisioner",
    "MultiSlotRoundTransaction",
    "MultiSlotRoundTransactionCoordinator",
    "NativeCpuProfilerKind",
    "NativeCpuProfilerPreflight",
    "RoundRecoveryOutcome",
    "RoundTransactionError",
    "RunState",
    "SDKRoots",
    "SkillCatalogEntry",
    "SkillMetadataError",
    "ValidationRecipe",
    "build_skill_catalog",
    "collect_linux_profile",
    "collect_macos_profile",
    "create_agent_session_runtime",
    "create_model_request_reconciler",
    "detect_linux_profiler",
    "detect_macos_profiler",
    "discover_skill_dirs",
    "load_skill_frontmatter",
    "materialize_input_project",
    "parse_profile_command",
    "preflight_native_cpu_profiler",
    "relative_sdk_source",
    "resolve_bundled_tree",
    "resolve_packaged_tree",
    "resolve_sdk_source",
    "resolve_skill_resources",
    "run_local_validation",
    "summarize_linux_profile",
]
