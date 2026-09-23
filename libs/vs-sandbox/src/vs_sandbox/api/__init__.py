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
    from vs_sandbox.docker_sandbox import AGENT_HOME, DockerSandbox
    from vs_sandbox.execution import Sandbox, SandboxExecutionResult
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
    from vs_sandbox.local_shell import LocalShellSandbox
    from vs_sandbox.modal_model_setup import ensure_model_volume

__all__ = [
    "AGENT_HOME",
    "BeforeReadyContext",
    "DockerSandbox",
    "HostResource",
    "HostResourceAccess",
    "HostResourceContext",
    "HostResourceDeclarer",
    "HostSandbox",
    "LandlockSandbox",
    "LinuxBackend",
    "LocalShellSandbox",
    "ProjectPathPolicy",
    "ProjectPathPolicyError",
    "Sandbox",
    "SandboxExecutionResult",
    "SandboxLifecycle",
    "SandboxLifecycleError",
    "SandboxLifecycleHooks",
    "SandboxUnavailableError",
    "SeatbeltSandbox",
    "WorkspaceSandbox",
    "build_host_sandbox",
    "declare_resources",
    "ensure_model_volume",
]

_LAZY_EXPORTS = {
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
