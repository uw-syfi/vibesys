"""Public API of the ``vs_sandbox`` library.

The export list below is the deliberate surface consumers (vibesys) depend
on; everything else in the submodules is internal. Exports resolve lazily
via :pep:`562` so importing the package root does not pull in the heavy
optional dependencies of unused backends (``modal``, in particular, is only
imported when a Modal-backed export is first accessed).
"""

from typing import TYPE_CHECKING, Any

from vs_sandbox.project_paths import ProjectPathPolicy, ProjectPathPolicyError

if TYPE_CHECKING:
    from vs_sandbox.docker_sandbox import DockerSandbox
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
    from vs_sandbox.host_sandbox import (
        build as build_host_sandbox,
    )
    from vs_sandbox.modal_model_setup import ensure_model_volume
    from vs_sandbox.modal_sandbox import ModalSandbox

__all__ = [
    "DockerSandbox",
    "HostResource",
    "HostResourceAccess",
    "HostResourceContext",
    "HostResourceDeclarer",
    "HostSandbox",
    "LandlockSandbox",
    "LinuxBackend",
    "ModalSandbox",
    "ProjectPathPolicy",
    "ProjectPathPolicyError",
    "SandboxUnavailableError",
    "SeatbeltSandbox",
    "WorkspaceSandbox",
    "build_host_sandbox",
    "declare_resources",
    "ensure_model_volume",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401  # tracked: #288
    if name == "DockerSandbox":
        from vs_sandbox.docker_sandbox import DockerSandbox  # noqa: PLC0415  # tracked: #288

        return DockerSandbox
    if name in {
        "HostResource",
        "HostResourceAccess",
        "HostResourceContext",
        "HostResourceDeclarer",
        "declare_resources",
    }:
        from vs_sandbox import host_resources  # noqa: PLC0415  # tracked: #288

        return getattr(host_resources, name)
    if name in {
        "HostSandbox",
        "LandlockSandbox",
        "LinuxBackend",
        "SandboxUnavailableError",
        "SeatbeltSandbox",
        "WorkspaceSandbox",
    }:
        from vs_sandbox import host_sandbox  # noqa: PLC0415  # tracked: #288

        return getattr(host_sandbox, name)
    if name == "build_host_sandbox":
        from vs_sandbox.host_sandbox import build  # noqa: PLC0415  # tracked: #288

        return build
    if name == "ModalSandbox":
        from vs_sandbox.modal_sandbox import ModalSandbox  # noqa: PLC0415  # tracked: #288

        return ModalSandbox
    if name == "ensure_model_volume":
        from vs_sandbox.modal_model_setup import (  # noqa: PLC0415  # tracked: #288
            ensure_model_volume,
        )

        return ensure_model_volume
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")  # noqa: TRY003  # tracked: #288
