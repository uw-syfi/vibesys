"""Composition-only factories for production runtime infrastructure.

Orchestration plugins use :mod:`vs_runtime.api`, not this module. VibeSys
composition imports this factory to bind lower-library effects to the private
runtime implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Protocol

from vs_runtime._accelerators import (
    AcceleratorDiscovery,
    AcceleratorInventory,
    SystemAcceleratorDiscovery,
)
from vs_runtime._bundled_paths import resolve_bundled_tree, resolve_packaged_tree
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

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


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
    "AcceleratorDiscovery",
    "AcceleratorInventory",
    "InputDependency",
    "InputProjectError",
    "LinuxProfileResult",
    "LinuxProfilerCapability",
    "LinuxProfilerDiagnostic",
    "LinuxProfilerEffects",
    "LinuxProfilerTool",
    "MacOSProfileResult",
    "MacOSProfilerCapability",
    "MacOSProfilerDiagnostic",
    "MacOSProfilerEffects",
    "MacOSProfilerTool",
    "ModelRequestError",
    "ModelRequestReconciler",
    "ModelVolumeProvisioner",
    "NativeCpuProfilerKind",
    "NativeCpuProfilerPreflight",
    "SDKRoots",
    "SkillCatalogEntry",
    "SkillMetadataError",
    "SystemAcceleratorDiscovery",
    "build_skill_catalog",
    "collect_linux_profile",
    "collect_macos_profile",
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
    "summarize_linux_profile",
]
