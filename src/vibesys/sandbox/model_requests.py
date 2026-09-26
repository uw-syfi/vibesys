"""Candidate-declared model-weight requests.

A candidate may declare additional model weights its implementation needs by
writing a small manifest to ``.vibesys/models.json`` in its workspace root.
Between rounds, before the framework deploys the candidate for its accuracy and
benchmark gates, the framework reads this manifest and ensures each requested
model is staged into a Modal Volume. Staging is idempotent: a volume that
already carries the ready sentinel is a no-op, so reconciling every round (and
again on resume) is cheap and safe.

This is a general resource-request mechanism, deliberately narrow: the manifest
expresses only *which model weights* the implementation needs. It cannot change
the measurement envelope (GPU count, GPU type, benchmark configuration), which
stays operator-owned. An operator may further restrict which repositories are
permitted via the ``VIBESYS_MODEL_REQUEST_ALLOW`` environment variable (a
comma-separated list of allowed HuggingFace repo-id prefixes); unset means no
prefix restriction.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MODEL_MANIFEST_RELPATH = ".vibesys/models.json"
_ALLOW_ENV_VAR = "VIBESYS_MODEL_REQUEST_ALLOW"


class ModelRequestError(ValueError):
    """Raised when a model-request manifest is malformed or disallowed."""

    @classmethod
    def invalid_json(cls, error: json.JSONDecodeError) -> ModelRequestError:
        """Describe a model manifest that could not be decoded as JSON."""
        return cls(f"{MODEL_MANIFEST_RELPATH} is not valid JSON: {error}")

    @classmethod
    def invalid_root(cls) -> ModelRequestError:
        """Describe a model manifest without its expected list structure."""
        return cls(
            f"{MODEL_MANIFEST_RELPATH} must be a JSON list of model objects, "
            'or an object with a "models" list'
        )

    @classmethod
    def entry_not_object(cls, index: int) -> ModelRequestError:
        """Describe a model manifest entry that is not an object."""
        return cls(f"{MODEL_MANIFEST_RELPATH} entry {index} is not an object")

    @classmethod
    def entry_id_missing(cls, index: int) -> ModelRequestError:
        """Describe a model entry without a nonempty repository ID."""
        return cls(
            f"{MODEL_MANIFEST_RELPATH} entry {index} needs a non-empty "
            'string "id" (the HuggingFace repo id)'
        )

    @classmethod
    def entry_revision_not_string(cls, index: int) -> ModelRequestError:
        """Describe a model entry whose optional revision is not a string."""
        return cls(f'{MODEL_MANIFEST_RELPATH} entry {index} "revision" must be a string')

    @classmethod
    def model_not_allowed(cls, model_id: str, allowlist: str | None) -> ModelRequestError:
        """Describe a model request excluded by the operator allowlist."""
        return cls(f"model request {model_id!r} is not permitted by {_ALLOW_ENV_VAR}={allowlist!r}")


@dataclass(frozen=True)
class ModelRequest:
    """A single requested model: a HuggingFace repo id and optional revision."""

    model_id: str
    revision: str | None = None


def read_model_requests(workspace: Path) -> list[ModelRequest]:
    """Parse ``.vibesys/models.json`` under *workspace*.

    Accepts either a bare JSON list of entries or an object with a ``"models"``
    list. Each entry is ``{"id": "<repo-id>", "revision": "<optional>"}`` (the
    key ``"model_id"`` is accepted as an alias for ``"id"``). Duplicate ids are
    collapsed, keeping the first occurrence. A missing file yields ``[]``.

    Raises:
        ModelRequestError: on invalid JSON or an entry that is not a mapping
            with a non-empty string id (and, if present, a string revision).
    """
    path = workspace / MODEL_MANIFEST_RELPATH
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ModelRequestError.invalid_json(exc) from exc

    entries = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ModelRequestError.invalid_root()

    requests: list[ModelRequest] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ModelRequestError.entry_not_object(index)
        model_id = entry.get("id") or entry.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ModelRequestError.entry_id_missing(index)
        revision = entry.get("revision")
        if revision is not None and not isinstance(revision, str):
            raise ModelRequestError.entry_revision_not_string(index)
        model_id = model_id.strip()
        if model_id in seen:
            continue
        seen.add(model_id)
        requests.append(ModelRequest(model_id=model_id, revision=revision))
    return requests


def _allow_prefixes() -> tuple[str, ...] | None:
    """Return operator-configured allowed repo-id prefixes, or None if unset."""
    raw = os.environ.get(_ALLOW_ENV_VAR, "").strip()
    if not raw:
        return None
    return tuple(prefix.strip() for prefix in raw.split(",") if prefix.strip())


def check_allowed(model_id: str, allow: tuple[str, ...] | None) -> bool:
    """Return True if *model_id* is permitted by the *allow* prefix list.

    ``allow=None`` means no prefix restriction (all model ids permitted).
    """
    if allow is None:
        return True
    return any(model_id.startswith(prefix) for prefix in allow)


def reconcile_model_requests(
    workspace: Path,
    *,
    log: Callable[[str], object] = print,
) -> list[str]:
    """Ensure every model declared under *workspace* is staged into a Volume.

    Returns the list of provisioned Modal Volume names (empty when the manifest
    is absent or empty). Idempotent: already-ready volumes are skipped by
    :func:`vs_sandbox.ensure_model_volume`.

    Raises:
        ModelRequestError: if the manifest is malformed or requests a model that
            the operator allowlist does not permit.
    """
    requests = read_model_requests(workspace)
    if not requests:
        return []

    ensure_model_volume = import_module("framework.api").ensure_model_volume

    allow = _allow_prefixes()
    volumes: list[str] = []
    for request in requests:
        if not check_allowed(request.model_id, allow):
            raise ModelRequestError.model_not_allowed(
                request.model_id,
                os.environ.get(_ALLOW_ENV_VAR),
            )
        suffix = f"@{request.revision}" if request.revision else ""
        log(f"[model-request] ensuring weights for {request.model_id}{suffix}")
        volumes.append(ensure_model_volume(request.model_id, revision=request.revision, log=log))
    return volumes
