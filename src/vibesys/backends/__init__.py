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

from collections.abc import Callable  # noqa: TC003  # tracked: #288
from pathlib import Path  # noqa: TC003  # tracked: #288

from vibesys.backends.base import (
    ComputeBackendImpl,
    ContentionMonitor,
    Device,
    SandboxKind,
)
from vibesys.constants import ComputeBackend

# A registry entry is any callable that, given ``log_dir``/``log``/``image``,
# returns a ``ComputeBackendImpl`` — a bare impl class, or ``LocalBackend``
# with its platform identity bound via ``functools.partial``.
_REGISTRY: dict[ComputeBackend, Callable[..., ComputeBackendImpl]] = {}


_defaults_registered = False


def register(backend: ComputeBackend, factory: Callable[..., ComputeBackendImpl]) -> None:  # noqa: D103  # tracked: #288
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
        raise ValueError(f"No backend impl registered for {backend!r}")  # noqa: TRY003  # tracked: #288
    return _REGISTRY[backend](
        log_dir=log_dir,
        log=log,
        image=image,
    )


# Deferred until get() so importing this module stays cheap. The backend
# modules themselves must stay cheap too: they defer deepagents (langchain +
# anthropic) to first sandbox construction, so registration costs milliseconds.
def _register_defaults() -> None:
    from vibesys.backends.cuda import CudaBackend  # noqa: PLC0415  # tracked: #288
    from vibesys.backends.local import cpu_backend, metal_backend  # noqa: PLC0415  # tracked: #288
    from vibesys.backends.rocm import RocmBackend  # noqa: PLC0415  # tracked: #288
    from vibesys.backends.trainium import TrainiumBackend  # noqa: PLC0415  # tracked: #288

    register(ComputeBackend.CUDA, CudaBackend)
    register(ComputeBackend.METAL, metal_backend)
    register(ComputeBackend.TRAINIUM, TrainiumBackend)
    register(ComputeBackend.ROCM, RocmBackend)
    register(ComputeBackend.CPU, cpu_backend)


def _ensure_defaults() -> None:
    global _defaults_registered  # noqa: PLW0603  # tracked: #288
    if _defaults_registered:
        return
    _defaults_registered = True
    _register_defaults()


__all__ = [
    "ComputeBackendImpl",
    "ContentionMonitor",
    "Device",
    "SandboxKind",
    "get",
    "register",
]
