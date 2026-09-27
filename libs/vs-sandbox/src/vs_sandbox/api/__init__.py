"""Public sandbox contracts, implementations, and resource helpers.

Backend exports load on first access so importing this facade does not import
optional backend dependencies such as Modal. ``Sandbox`` and
``SandboxExecutionResult`` describe the execution contract; sandbox classes
select local, host, or Docker execution. Lifecycle and host-resource types
support composition by callers.
"""

from importlib import import_module
from typing import TYPE_CHECKING

from vs_sandbox.project_paths import ProjectPathPolicy, ProjectPathPolicyError

if TYPE_CHECKING:
    from vs_sandbox.accelerator_discovery import (
        AcceleratorDiscovery,
        AcceleratorInventory,
        SystemAcceleratorDiscovery,
    )
    from vs_sandbox.compute_backends import (
        ComputeBackend,
        ComputeBackendImpl,
        ContentionMonitor,
        Device,
        SandboxKind,
        create_compute_backend,
        register_compute_backend,
    )
    from vs_sandbox.cuda_backend import CudaBackend
    from vs_sandbox.device_lease import DeviceLease
    from vs_sandbox.docker_sandbox import AGENT_HOME, DockerSandbox
    from vs_sandbox.execution import Sandbox, SandboxExecutionResult
    from vs_sandbox.gpu_monitor import (
        GpuContentionMonitor,
        GpuInfo,
        parse_gpu_process_output,
        pick_gpu,
        query_gpu_info,
    )
    from vs_sandbox.host_resources import (
        HostResource,
        HostResourceAccess,
        HostResourceContext,
        HostResourceDeclarer,
        declare_resources,
    )
    from vs_sandbox.host_sandbox import (
        HostSandbox,
        LandlockSandbox,
        LinuxBackend,
        SandboxUnavailableError,
        SeatbeltSandbox,
        WorkspaceSandbox,
    )
    from vs_sandbox.host_sandbox import build as build_host_sandbox
    from vs_sandbox.lifecycle import (
        BeforeReadyContext,
        SandboxLifecycle,
        SandboxLifecycleError,
        SandboxLifecycleHooks,
    )
    from vs_sandbox.local_compute_backend import LocalBackend
    from vs_sandbox.local_shell import LocalShellSandbox
    from vs_sandbox.modal_model_setup import ensure_model_volume
    from vs_sandbox.rocm_backend import RocmBackend
    from vs_sandbox.trainium_backend import TrainiumBackend

__all__ = [
    "AGENT_HOME",
    "AcceleratorDiscovery",
    "AcceleratorInventory",
    "BeforeReadyContext",
    "ComputeBackend",
    "ComputeBackendImpl",
    "ContentionMonitor",
    "CudaBackend",
    "Device",
    "DeviceLease",
    "DockerSandbox",
    "GpuContentionMonitor",
    "GpuInfo",
    "HostResource",
    "HostResourceAccess",
    "HostResourceContext",
    "HostResourceDeclarer",
    "HostSandbox",
    "LandlockSandbox",
    "LinuxBackend",
    "LocalBackend",
    "LocalShellSandbox",
    "ProjectPathPolicy",
    "ProjectPathPolicyError",
    "RocmBackend",
    "Sandbox",
    "SandboxExecutionResult",
    "SandboxKind",
    "SandboxLifecycle",
    "SandboxLifecycleError",
    "SandboxLifecycleHooks",
    "SandboxUnavailableError",
    "SeatbeltSandbox",
    "SystemAcceleratorDiscovery",
    "TrainiumBackend",
    "WorkspaceSandbox",
    "build_host_sandbox",
    "create_compute_backend",
    "declare_resources",
    "ensure_model_volume",
    "parse_gpu_process_output",
    "pick_gpu",
    "query_gpu_info",
    "register_compute_backend",
]

_LAZY_EXPORTS = {
    "AcceleratorDiscovery": ("accelerator_discovery", "AcceleratorDiscovery"),
    "AcceleratorInventory": ("accelerator_discovery", "AcceleratorInventory"),
    "SystemAcceleratorDiscovery": (
        "accelerator_discovery",
        "SystemAcceleratorDiscovery",
    ),
    "ComputeBackend": ("compute_backends", "ComputeBackend"),
    "ComputeBackendImpl": ("compute_backends", "ComputeBackendImpl"),
    "ContentionMonitor": ("compute_backends", "ContentionMonitor"),
    "Device": ("compute_backends", "Device"),
    "SandboxKind": ("compute_backends", "SandboxKind"),
    "create_compute_backend": ("compute_backends", "create_compute_backend"),
    "register_compute_backend": ("compute_backends", "register_compute_backend"),
    "CudaBackend": ("cuda_backend", "CudaBackend"),
    "DeviceLease": ("device_lease", "DeviceLease"),
    "GpuContentionMonitor": ("gpu_monitor", "GpuContentionMonitor"),
    "GpuInfo": ("gpu_monitor", "GpuInfo"),
    "parse_gpu_process_output": ("gpu_monitor", "parse_gpu_process_output"),
    "pick_gpu": ("gpu_monitor", "pick_gpu"),
    "query_gpu_info": ("gpu_monitor", "query_gpu_info"),
    "LocalBackend": ("local_compute_backend", "LocalBackend"),
    "RocmBackend": ("rocm_backend", "RocmBackend"),
    "TrainiumBackend": ("trainium_backend", "TrainiumBackend"),
    "AGENT_HOME": ("docker_sandbox", "AGENT_HOME"),
    "DockerSandbox": ("docker_sandbox", "DockerSandbox"),
    "Sandbox": ("execution", "Sandbox"),
    "SandboxExecutionResult": ("execution", "SandboxExecutionResult"),
    "HostResource": ("host_resources", "HostResource"),
    "HostResourceAccess": ("host_resources", "HostResourceAccess"),
    "HostResourceContext": ("host_resources", "HostResourceContext"),
    "HostResourceDeclarer": ("host_resources", "HostResourceDeclarer"),
    "declare_resources": ("host_resources", "declare_resources"),
    "HostSandbox": ("host_sandbox", "HostSandbox"),
    "LandlockSandbox": ("host_sandbox", "LandlockSandbox"),
    "LinuxBackend": ("host_sandbox", "LinuxBackend"),
    "SandboxUnavailableError": ("host_sandbox", "SandboxUnavailableError"),
    "SeatbeltSandbox": ("host_sandbox", "SeatbeltSandbox"),
    "WorkspaceSandbox": ("host_sandbox", "WorkspaceSandbox"),
    "build_host_sandbox": ("host_sandbox", "build"),
    "BeforeReadyContext": ("lifecycle", "BeforeReadyContext"),
    "SandboxLifecycle": ("lifecycle", "SandboxLifecycle"),
    "SandboxLifecycleError": ("lifecycle", "SandboxLifecycleError"),
    "SandboxLifecycleHooks": ("lifecycle", "SandboxLifecycleHooks"),
    "LocalShellSandbox": ("local_shell", "LocalShellSandbox"),
    "ensure_model_volume": ("modal_model_setup", "ensure_model_volume"),
}


def __getattr__(name: str) -> object:
    """Resolve a public backend export only when it is requested."""
    if name not in _LAZY_EXPORTS:
        raise AttributeError(name)
    module_name, symbol_name = _LAZY_EXPORTS[name]
    module = import_module(f"vs_sandbox.{module_name}")
    value = getattr(module, symbol_name)
    globals()[name] = value
    return value
