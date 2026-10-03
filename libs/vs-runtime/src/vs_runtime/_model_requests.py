"""Private reconciliation of candidate-declared model-weight requests."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

MODEL_MANIFEST_RELPATH = ".vibesys/models.json"
MODEL_REQUEST_ALLOW_ENV = "VIBESYS_MODEL_REQUEST_ALLOW"


class ModelRequestError(ValueError):
    """A model-request manifest is malformed or operator-disallowed."""

    @classmethod
    def invalid_json(cls, error: json.JSONDecodeError) -> ModelRequestError:
        return cls(f"{MODEL_MANIFEST_RELPATH} is not valid JSON: {error}")

    @classmethod
    def invalid_root(cls) -> ModelRequestError:
        return cls(
            f"{MODEL_MANIFEST_RELPATH} must be a JSON list of model objects, "
            'or an object with a "models" list'
        )

    @classmethod
    def entry_not_object(cls, index: int) -> ModelRequestError:
        return cls(f"{MODEL_MANIFEST_RELPATH} entry {index} is not an object")

    @classmethod
    def entry_id_missing(cls, index: int) -> ModelRequestError:
        return cls(
            f"{MODEL_MANIFEST_RELPATH} entry {index} needs a non-empty "
            'string "id" (the HuggingFace repo id)'
        )

    @classmethod
    def entry_revision_not_string(cls, index: int) -> ModelRequestError:
        return cls(f'{MODEL_MANIFEST_RELPATH} entry {index} "revision" must be a string')

    @classmethod
    def model_not_allowed(cls, model_id: str, allowlist: str | None) -> ModelRequestError:
        return cls(
            f"model request {model_id!r} is not permitted by "
            f"{MODEL_REQUEST_ALLOW_ENV}={allowlist!r}"
        )


@dataclass(frozen=True, slots=True)
class _ModelRequest:
    model_id: str
    revision: str | None = None


class _ModelRequestReconciler:
    """Apply one immutable operator environment through an injected provisioner."""

    def __init__(
        self,
        provision: Callable[..., str],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._provision = provision
        self._environment = os.environ if environment is None else environment

    def reconcile(
        self,
        workspace: Path,
        *,
        log: Callable[[str], object] = print,
    ) -> tuple[str, ...]:
        """Validate and provision every request in manifest order."""
        requests = _read_model_requests(workspace)
        if not requests:
            return ()

        allowlist = self._environment.get(MODEL_REQUEST_ALLOW_ENV)
        allowed = _allow_prefixes(allowlist)
        volumes: list[str] = []
        for request in requests:
            if not _check_allowed(request.model_id, allowed):
                raise ModelRequestError.model_not_allowed(request.model_id, allowlist)
            suffix = f"@{request.revision}" if request.revision else ""
            log(f"[model-request] ensuring weights for {request.model_id}{suffix}")
            volumes.append(self._provision(request.model_id, revision=request.revision, log=log))
        return tuple(volumes)


def _read_model_requests(workspace: Path) -> tuple[_ModelRequest, ...]:
    path = workspace / MODEL_MANIFEST_RELPATH
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ModelRequestError.invalid_json(exc) from exc

    entries = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ModelRequestError.invalid_root()

    requests: list[_ModelRequest] = []
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
        requests.append(_ModelRequest(model_id, revision))
    return tuple(requests)


def _allow_prefixes(raw: str | None) -> tuple[str, ...] | None:
    if raw is None or not raw.strip():
        return None
    return tuple(prefix.strip() for prefix in raw.split(",") if prefix.strip())


def _check_allowed(model_id: str, allow: tuple[str, ...] | None) -> bool:
    return allow is None or any(model_id.startswith(prefix) for prefix in allow)
