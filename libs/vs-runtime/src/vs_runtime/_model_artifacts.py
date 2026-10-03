"""Prepare model weights for local and isolated run environments.

Product composition supplies paths and environment facts through the concrete
``prepare_model_artifacts`` operation.  This module owns Hugging Face cache
reuse, runtime-local symlinks, and the mount/copy policy for model weights.  It
does not own domain selection or any long-lived resource that needs teardown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, cast

from vs_sandbox.api import EnvironmentBindMount

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


_MODEL_ARTIFACT_NAMES = frozenset({"model", "draft_model"})


class ModelArtifactDownloader(Protocol):
    """Download one immutable model snapshot into a shared cache."""

    def __call__(
        self,
        model_id: str,
        *,
        revision: str | None,
        cache_dir: str,
    ) -> str | Path:
        """Return the local directory containing the requested snapshot."""
        ...


@dataclass(frozen=True, slots=True)
class ModelArtifactRequest:
    """Paths and effects needed to prepare one task's model artifacts."""

    reference_dir: Path
    model_cache_dir: Path
    runtime_artifact_dir: Path
    log: Callable[[str], None] = print


@dataclass(frozen=True, slots=True)
class PreparedModelArtifacts:
    """Filesystem policy produced for project copying and sandbox opening."""

    copy_excludes: frozenset[str] = frozenset()
    bind_mounts: tuple[EnvironmentBindMount, ...] = ()


@dataclass(frozen=True, slots=True)
class _ModelMetadata:
    model_id: str
    revision: str | None


def prepare_model_artifacts(
    request: ModelArtifactRequest,
    *,
    isolated: bool,
    materialize_local_weights: bool,
    downloader: ModelArtifactDownloader | None = None,
) -> PreparedModelArtifacts:
    """Prepare primary model weights and return their copy and mount policy.

    An authored ``reference/model`` always wins over runtime-managed weights.
    Environments that materialize weights locally download a missing primary
    model from ``reference/meta.json``.  Environments that provision weights
    remotely skip that download when metadata exists, but a task without
    metadata must still provide its primary model locally.
    """
    reference_dir = request.reference_dir
    if not reference_dir.is_dir():
        return PreparedModelArtifacts()

    model_path = reference_dir / "model"
    metadata_path = reference_dir / "meta.json"
    if materialize_local_weights or not metadata_path.exists():
        model_path = _ensure_primary_model(
            request,
            downloader=(_download_model_snapshot if downloader is None else downloader),
        )

    mounts: list[EnvironmentBindMount] = []
    if model_path.is_dir() or model_path.is_symlink():
        mounts.append(EnvironmentBindMount(model_path, "/model", read_only=True))

    draft_model_path = reference_dir / "draft_model"
    if draft_model_path.is_dir() or draft_model_path.is_symlink():
        mounts.append(EnvironmentBindMount(draft_model_path, "/draft_model", read_only=True))

    return PreparedModelArtifacts(
        copy_excludes=_MODEL_ARTIFACT_NAMES if isolated else frozenset(),
        bind_mounts=tuple(mounts),
    )


def _ensure_primary_model(
    request: ModelArtifactRequest,
    *,
    downloader: ModelArtifactDownloader,
) -> Path:
    authored_model_path = request.reference_dir / "model"
    if authored_model_path.exists():
        return authored_model_path

    model_path = request.runtime_artifact_dir / "model"
    if model_path.is_symlink() and not model_path.exists():
        model_path.unlink()
    if model_path.exists():
        return model_path

    metadata_path = request.reference_dir / "meta.json"
    if not metadata_path.exists():
        message = (
            f"Model weights not found at {model_path} and no meta.json to download from. "
            "Either create a model/ directory/symlink or add a meta.json with model_id."
        )
        raise FileNotFoundError(message)

    metadata = _read_model_metadata(metadata_path)
    request.log(
        f"[model] Weights not found at {model_path}. "
        f"Downloading {metadata.model_id} to {request.model_cache_dir}..."
    )
    downloaded_path = downloader(
        metadata.model_id,
        revision=metadata.revision,
        cache_dir=str(request.model_cache_dir),
    )
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.symlink_to(downloaded_path)
    request.log(f"[model] Created symlink {model_path} -> {downloaded_path}")
    return model_path


def _read_model_metadata(path: Path) -> _ModelMetadata:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        message = f"meta.json at {path} must contain a JSON object"
        raise TypeError(message)

    model_id = raw.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        message = f"meta.json at {path} missing required 'model_id' field"
        raise ValueError(message)

    revision = raw.get("revision")
    if revision is not None and not isinstance(revision, str):
        message = f"meta.json at {path} field 'revision' must be a string"
        raise ValueError(message)
    return _ModelMetadata(model_id=model_id, revision=revision)


def _download_model_snapshot(
    model_id: str,
    *,
    revision: str | None,
    cache_dir: str,
) -> str:
    snapshot_download = cast(
        "Callable[..., str]",
        import_module("huggingface_hub").snapshot_download,
    )
    return snapshot_download(model_id, revision=revision, cache_dir=cache_dir)
