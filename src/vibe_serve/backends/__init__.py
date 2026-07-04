"""Compute backend registry.

Each backend with real hardware logic lives in its own subpackage (e.g.
``cuda/``, ``trainium/``) and exposes a ``ComputeBackendImpl`` matching the
protocol in :mod:`backends.base`. Local-only, no-accelerator backends
(``metal``, ``cpu``) share :class:`backends.local.LocalBackend`, bound to
their platform at registration.

Add a new backend by:

1. Adding the variant to :class:`vibe_serve.constants.ComputeBackend`.
2. Providing an impl: a new ``backends/<name>/`` subpackage for a backend
   with device logic, or — if it just runs locally with no accelerator —
   registering ``LocalBackend`` bound to the new variant.
3. Registering it in :func:`_register_defaults` below.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from vibe_serve.backends.base import (
    ComputeBackendImpl,
    ContentionMonitor,
    Device,
    ModalOptions,
    SandboxKind,
)
from vibe_serve.constants import ComputeBackend

# A registry entry is any callable that, given ``log_dir``/``log``/``image``,
# returns a ``ComputeBackendImpl`` — a bare impl class, or ``LocalBackend``
# with its platform identity bound via ``functools.partial``.
_REGISTRY: dict[ComputeBackend, Callable[..., ComputeBackendImpl]] = {}


def register(backend: ComputeBackend, factory: Callable[..., ComputeBackendImpl]) -> None:
    _REGISTRY[backend] = factory


def get(
    backend: ComputeBackend,
    log_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
) -> ComputeBackendImpl:
    """Construct the ComputeBackendImpl for *backend*."""
    if backend not in _REGISTRY:
        raise ValueError(f"No backend impl registered for {backend!r}")
    return _REGISTRY[backend](
        log_dir=log_dir,
        log=log,
        image=image,
    )


# Default registration.  Imported lazily to avoid pulling deepagents/Modal
# into modules that just want the protocol types.
def _register_defaults() -> None:
    from vibe_serve.backends.cuda import CudaBackend
    from vibe_serve.backends.local import cpu_backend, metal_backend
    from vibe_serve.backends.trainium import TrainiumBackend

    register(ComputeBackend.CUDA, CudaBackend)
    register(ComputeBackend.METAL, metal_backend)
    register(ComputeBackend.TRAINIUM, TrainiumBackend)
    register(ComputeBackend.CPU, cpu_backend)


_register_defaults()


__all__ = [
    "ComputeBackendImpl",
    "ContentionMonitor",
    "Device",
    "ModalOptions",
    "SandboxKind",
    "get",
    "register",
]
