"""Composition-only factories for production runtime infrastructure.

Orchestration plugins use :mod:`vs_runtime.api`, not this module. VibeSys
composition imports this factory to bind lower-library effects to the private
runtime implementation.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Protocol

from vs_runtime._model_requests import ModelRequestError, _ModelRequestReconciler

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
    "ModelRequestError",
    "ModelRequestReconciler",
    "ModelVolumeProvisioner",
    "create_model_request_reconciler",
]
