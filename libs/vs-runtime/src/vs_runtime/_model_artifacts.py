"""Prepare model weights for local and isolated run environments.

Product composition supplies paths and environment facts through the concrete
``prepare_model_artifacts`` operation.  This module owns Hugging Face cache
reuse, runtime-local symlinks, and the mount/copy policy for model weights.  It
does not own domain selection or any long-lived resource that needs teardown.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from pydantic import JsonValue, TypeAdapter

from vs_decode.api import Unreadable, read_json
from vs_sandbox.api import EnvironmentBindMount

if TYPE_CHECKING:
    from collections.abc import Callable


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
    """Paths and effects needed to prepare one run's model artifacts.

    ``reference_dir`` is the task reference that may author the weights;
    ``model_cache_dir`` is the download cache; ``runtime_artifact_dir`` is the
    run's model record, which must be the same directory on every launch and
    resume of one run.
    """

    reference_dir: Path
    model_cache_dir: Path
    runtime_artifact_dir: Path
    log: Callable[[str], None] = print


@dataclass(frozen=True, slots=True)
class PreparedModelArtifacts:
    """Filesystem policy produced for project copying and sandbox opening."""

    copy_excludes: frozenset[str] = frozenset()
    bind_mounts: tuple[EnvironmentBindMount, ...] = ()


_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_MAX_METADATA_BYTES = 1024 * 1024
"""The largest task ``meta.json`` read."""


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """What a task's ``meta.json`` (or ``draft_meta.json``) says to download."""

    model_id: str
    revision: str | None


def model_artifact_copy_excludes(*, isolated: bool) -> frozenset[str]:
    """Reference entries a project copy leaves out because they are mounted instead."""
    return _MODEL_ARTIFACT_NAMES if isolated else frozenset()


def prepare_model_artifacts(
    request: ModelArtifactRequest,
    *,
    isolated: bool,
    materialize_local_weights: bool,
    downloader: ModelArtifactDownloader | None = None,
) -> PreparedModelArtifacts:
    """Resolve a run's model weights and return their copy and mount policy.

    ``request.runtime_artifact_dir`` holds the run's model record: one link
    per artifact (``model``, ``draft_model``) to the weights the run uses.
    Every mount comes from that record, so a fresh launch and a resume of the
    same run mount the same weights.  An authored ``reference/<name>`` always
    wins and is recorded; a resume, whose project copy holds no external
    weights, reads the record.  Environments that materialize weights locally
    download a missing primary model from ``reference/meta.json``.
    Environments that provision weights remotely skip that download when
    metadata exists, but a task without metadata must still provide its
    primary model, authored or recorded.
    """
    reference_dir = request.reference_dir
    if not reference_dir.is_dir() and not any(
        _recorded_artifact(request, name) is not None for name in sorted(_MODEL_ARTIFACT_NAMES)
    ):
        return PreparedModelArtifacts()

    mounts: list[EnvironmentBindMount] = []
    model_path = _resolve_artifact(request, "model")
    if model_path is None and (
        materialize_local_weights or not (reference_dir / "meta.json").exists()
    ):
        model_path = _download_primary_model(
            request,
            downloader=(_download_model_snapshot if downloader is None else downloader),
        )
    if model_path is not None:
        mounts.append(EnvironmentBindMount(model_path, "/model", read_only=True))

    draft_model_path = _resolve_artifact(request, "draft_model")
    if draft_model_path is not None:
        mounts.append(EnvironmentBindMount(draft_model_path, "/draft_model", read_only=True))

    return PreparedModelArtifacts(
        copy_excludes=model_artifact_copy_excludes(isolated=isolated),
        bind_mounts=tuple(mounts),
    )


def _resolve_artifact(request: ModelArtifactRequest, name: str) -> Path | None:
    """Return the run's recorded ``name`` weights, recording an authored reference first."""
    authored = request.reference_dir / name
    recorded = request.runtime_artifact_dir / name
    if authored.exists() and authored.absolute() != recorded.absolute():
        _record(recorded, authored.resolve())
    return _recorded_artifact(request, name)


def _recorded_artifact(request: ModelArtifactRequest, name: str) -> Path | None:
    recorded = request.runtime_artifact_dir / name
    if recorded.is_symlink() and not recorded.exists():
        recorded.unlink()
    return recorded if recorded.exists() else None


def _record(recorded: Path, target: Path) -> None:
    """Point the run's record for one artifact at ``target``, replacing it atomically."""
    if recorded.is_symlink() and recorded.resolve() == target:
        return
    if recorded.exists() and not recorded.is_symlink():
        message = (
            f"Cannot record the run's model at {recorded}: it holds files, not a link. "
            "Remove it so the run's model record can point at the task's weights."
        )
        raise FileExistsError(message)
    recorded.parent.mkdir(parents=True, exist_ok=True)
    staged = recorded.with_name(f".{recorded.name}.recording")
    staged.unlink(missing_ok=True)
    staged.symlink_to(target, target_is_directory=True)
    staged.replace(recorded)


def _download_primary_model(
    request: ModelArtifactRequest,
    *,
    downloader: ModelArtifactDownloader,
) -> Path:
    model_path = request.runtime_artifact_dir / "model"
    metadata_path = request.reference_dir / "meta.json"
    if not metadata_path.exists():
        message = (
            f"Model weights not found: no {request.reference_dir / 'model'}, no run model "
            f"record at {model_path}, and no meta.json to download from. Either create a "
            "model/ directory/symlink or add a meta.json with model_id."
        )
        raise FileNotFoundError(message)

    metadata = read_model_metadata(metadata_path)
    request.log(
        f"[model] Weights not found at {model_path}. "
        f"Downloading {metadata.model_id} to {request.model_cache_dir}..."
    )
    downloaded_path = downloader(
        metadata.model_id,
        revision=metadata.revision,
        cache_dir=str(request.model_cache_dir),
    )
    _record(model_path, Path(downloaded_path).resolve())
    request.log(f"[model] Created symlink {model_path} -> {downloaded_path}")
    return model_path


class ModelMetadataUnreadableError(ValueError):
    """A ``meta.json`` that cannot be read, is too large, or is not JSON."""


def read_model_metadata(path: Path) -> ModelMetadata:
    """Read a task's model metadata file: the one reader of ``meta.json``.

    Raises ``ValueError`` for a file that cannot be read, is too large, is not JSON (deep
    nesting included) or lacks a usable ``model_id``, and ``TypeError`` for a document
    that is not an object.
    """
    raw = read_json(path, _JSON_VALUE, max_bytes=_MAX_METADATA_BYTES)
    if isinstance(raw, Unreadable):
        raise ModelMetadataUnreadableError(str(raw))
    if not isinstance(raw, dict):
        message = f"{path.name} at {path} must contain a JSON object"
        raise TypeError(message)

    model_id = raw.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        message = f"{path.name} at {path} missing required 'model_id' field"
        raise ValueError(message)

    revision = raw.get("revision")
    if revision is not None and not isinstance(revision, str):
        message = f"{path.name} at {path} field 'revision' must be a string"
        raise ValueError(message)
    return ModelMetadata(model_id=model_id, revision=revision)


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
