"""Compute backend registry.

Each backend with real hardware logic lives in its own subpackage (e.g.
``cuda/``, ``trainium/``) and exposes a ``ComputeBackendImpl`` matching the
protocol in :mod:`backends.base`. Local-only, no-accelerator backends
(``metal``, ``cpu``) share :class:`backends.local.LocalBackend`, bound to
their platform at registration.

Add a new backend by:

1. Adding the variant to :class:`vibesys.constants.ComputeBackend`.
2. Providing an impl: a new ``backends/<name>/`` subpackage for a backend
   with device logic, or — if it just runs locally with no accelerator —
   registering ``LocalBackend`` bound to the new variant.
3. Registering it in :func:`_register_defaults` below.
"""

from __future__ import annotations

from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING

from vibesys.backends.base import (
    ComputeBackendImpl,
    ContentionMonitor,
    Device,
    SandboxKind,
)
from vibesys.constants import ComputeBackend

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
# A registry entry is any callable that, given ``log_dir``/``log``/``image``,
# returns a ``ComputeBackendImpl`` — a bare impl class, or ``LocalBackend``
# with its platform identity bound via ``functools.partial``.
_REGISTRY: dict[ComputeBackend, Callable[..., ComputeBackendImpl]] = {}


def register(backend: ComputeBackend, factory: Callable[..., ComputeBackendImpl]) -> None:
    """Register the factory used to construct a compute backend."""
    _REGISTRY[backend] = factory


def get(
    backend: ComputeBackend,
    log_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
) -> ComputeBackendImpl:
    """Construct the ComputeBackendImpl for *backend*."""
    _ensure_defaults()
    if backend not in _REGISTRY:
        message = f"No backend impl registered for {backend!r}"
        raise ValueError(message)
    return _REGISTRY[backend](
        log_dir=log_dir,
        log=log,
        image=image,
    )


# Deferred until get() so importing this module stays cheap. The backend
# modules themselves defer DockerSandbox (process-wide signal and atexit
# handlers) to first sandbox construction, so registration stays side-effect free.
def _register_defaults() -> None:
    cuda = import_module("vibesys.backends.cuda")
    local = import_module("vibesys.backends.local")
    rocm = import_module("vibesys.backends.rocm")
    trainium = import_module("vibesys.backends.trainium")

    register(ComputeBackend.CUDA, cuda.CudaBackend)
    register(ComputeBackend.METAL, local.metal_backend)
    register(ComputeBackend.TRAINIUM, trainium.TrainiumBackend)
    register(ComputeBackend.ROCM, rocm.RocmBackend)
    register(ComputeBackend.CPU, local.cpu_backend)


@cache
def _ensure_defaults() -> None:
    _register_defaults()


__all__ = [
    "ComputeBackendImpl",
    "ContentionMonitor",
    "Device",
    "SandboxKind",
    "get",
    "register",
]
