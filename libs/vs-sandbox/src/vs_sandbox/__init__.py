"""Public API of the ``vs_sandbox`` library.

The export list below is the deliberate surface consumers (vibesys) depend
on; everything else in the submodules is internal. Exports resolve lazily
via :pep:`562` so importing the package root does not pull in the heavy
optional dependencies of unused backends (``modal``, in particular, is only
imported when a Modal-backed export is first accessed).
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vs_sandbox.docker_sandbox import DockerSandbox
    from vs_sandbox.modal_model_setup import ensure_model_volume
    from vs_sandbox.modal_sandbox import ModalSandbox

__all__ = [
    "DockerSandbox",
    "ModalSandbox",
    "ensure_model_volume",
]


def __getattr__(name: str) -> Any:
    if name == "DockerSandbox":
        from vs_sandbox.docker_sandbox import DockerSandbox

        return DockerSandbox
    if name == "ModalSandbox":
        from vs_sandbox.modal_sandbox import ModalSandbox

        return ModalSandbox
    if name == "ensure_model_volume":
        from vs_sandbox.modal_model_setup import ensure_model_volume

        return ensure_model_volume
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
