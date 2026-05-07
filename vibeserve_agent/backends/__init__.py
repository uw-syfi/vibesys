"""Compute backend registry.

Each backend lives in its own subpackage (e.g. ``cuda/``) and exposes a
``ComputeBackendImpl`` matching the protocol in :mod:`backends.base`.

Add a new backend by:

1. Adding the variant to :class:`vibeserve_agent.constants.ComputeBackend`.
2. Creating ``backends/<name>/`` with an ``__init__.py`` that defines a
   ``ComputeBackendImpl`` and any backend-private helpers (e.g. monitor modules).
3. Registering the impl in :data:`_REGISTRY` below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from vibeserve_agent.backends.base import (
    ComputeBackendImpl,
    ContentionMonitor,
    Device,
    ModalOptions,
    SandboxKind,
)
from vibeserve_agent.constants import ComputeBackend

_REGISTRY: dict[ComputeBackend, type[ComputeBackendImpl]] = {}


def register(backend: ComputeBackend, impl_cls: type[ComputeBackendImpl]) -> None:
    _REGISTRY[backend] = impl_cls


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
        log_dir=log_dir, log=log, image=image,
    )


# Default registration.  Imported lazily to avoid pulling deepagents/Modal
# into modules that just want the protocol types.
def _register_defaults() -> None:
    from vibeserve_agent.backends.cuda import CudaBackend
    from vibeserve_agent.backends.metal import MetalBackend

    register(ComputeBackend.CUDA, CudaBackend)
    register(ComputeBackend.METAL, MetalBackend)


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
