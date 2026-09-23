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
from collections.abc import Callable  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # tracked: #288

MODEL_MANIFEST_RELPATH = ".vibesys/models.json"
_ALLOW_ENV_VAR = "VIBESYS_MODEL_REQUEST_ALLOW"


class ModelRequestError(ValueError):
    """Raised when a model-request manifest is malformed or disallowed."""


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
        raise ModelRequestError(  # noqa: TRY003  # tracked: #288
            f"{MODEL_MANIFEST_RELPATH} is not valid JSON: {exc}"
        ) from exc

    entries = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ModelRequestError(  # noqa: TRY003  # tracked: #288
            f"{MODEL_MANIFEST_RELPATH} must be a JSON list of model objects, "
            'or an object with a "models" list'
        )

    requests: list[ModelRequest] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ModelRequestError(  # noqa: TRY003  # tracked: #288
                f"{MODEL_MANIFEST_RELPATH} entry {index} is not an object"
            )
        model_id = entry.get("id") or entry.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ModelRequestError(  # noqa: TRY003  # tracked: #288
                f"{MODEL_MANIFEST_RELPATH} entry {index} needs a non-empty "
                'string "id" (the HuggingFace repo id)'
            )
        revision = entry.get("revision")
        if revision is not None and not isinstance(revision, str):
            raise ModelRequestError(  # noqa: TRY003  # tracked: #288
                f'{MODEL_MANIFEST_RELPATH} entry {index} "revision" must be a string'
            )
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

    from vs_sandbox.api import ensure_model_volume  # noqa: PLC0415  # tracked: #288

    allow = _allow_prefixes()
    volumes: list[str] = []
    for request in requests:
        if not check_allowed(request.model_id, allow):
            raise ModelRequestError(  # noqa: TRY003  # tracked: #288
                f"model request {request.model_id!r} is not permitted by "
                f"{_ALLOW_ENV_VAR}={os.environ.get(_ALLOW_ENV_VAR)!r}"
            )
        suffix = f"@{request.revision}" if request.revision else ""
        log(f"[model-request] ensuring weights for {request.model_id}{suffix}")
        volumes.append(ensure_model_volume(request.model_id, revision=request.revision, log=log))
    return volumes
