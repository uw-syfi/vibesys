"""LLM-serving environment setup/teardown hooks."""

from __future__ import annotations

import json
from importlib import import_module
from typing import TYPE_CHECKING

from vibesys.domains.environment import (
    EnvironmentBindMount,
    EnvironmentContext,
    EnvironmentPatch,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _ensure_model_weights(
    ref_dir: Path,
    *,
    cache_dir: Path,
    runtime_artifact_dir: Path,
    log: Callable[[str], None],
) -> Path:
    """Resolve model weights without writing outside the runtime artifact root."""
    authored_model_path = ref_dir / "model"
    if authored_model_path.exists():
        return authored_model_path

    model_path = runtime_artifact_dir / "model"

    if model_path.is_symlink() and not model_path.exists():
        model_path.unlink()

    if model_path.exists():
        return model_path

    meta_path = ref_dir / "meta.json"
    if not meta_path.exists():
        message = f"Model weights not found at {model_path} and no meta.json to download from. Either create a model/ directory/symlink or add a meta.json with model_id."
        raise FileNotFoundError(message)

    meta = json.loads(meta_path.read_text())
    model_id = meta.get("model_id")
    if not model_id:
        _exception_message = f"meta.json at {meta_path} missing required 'model_id' field"
        raise ValueError(_exception_message)

    revision = meta.get("revision")
    log(f"[model] Weights not found at {model_path}. Downloading {model_id} to {cache_dir}...")
    snapshot_download = import_module("huggingface_hub").snapshot_download

    downloaded_path = snapshot_download(model_id, revision=revision, cache_dir=str(cache_dir))
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.symlink_to(downloaded_path)
    log(f"[model] Created symlink {model_path} -> {downloaded_path}")
    return model_path


class LLMServingEnvironmentHooks:
    """Prepare model weights and paths for LLM serving tasks."""

    _MODEL_ARTIFACT_NAMES = frozenset({"model", "draft_model"})

    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch:
        """Mount model artifacts into the selected evaluation environment."""
        ref_path = ctx.reference_path
        if not ref_path.is_dir():
            return EnvironmentPatch()

        model_path = ref_path / "model"
        meta_path = ref_path / "meta.json"
        if ctx.run_environment.materialize_local_model_weights or not meta_path.exists():
            model_path = _ensure_model_weights(
                ref_path,
                cache_dir=ctx.model_cache_dir,
                runtime_artifact_dir=ctx.runtime_artifact_dir,
                log=ctx.log,
            )

        bind_mounts: list[EnvironmentBindMount] = []
        if model_path.is_dir() or model_path.is_symlink():
            bind_mounts.append(EnvironmentBindMount(model_path, "/model", read_only=True))

        draft_model_path = ref_path / "draft_model"
        if draft_model_path.is_dir() or draft_model_path.is_symlink():
            bind_mounts.append(
                EnvironmentBindMount(draft_model_path, "/draft_model", read_only=True)
            )

        return EnvironmentPatch(
            copy_excludes=self._MODEL_ARTIFACT_NAMES
            if ctx.run_environment.isolated
            else frozenset(),
            bind_mounts=tuple(bind_mounts),
        )

    def teardown(self, ctx: EnvironmentContext) -> None:
        """Perform no cleanup because model preparation owns no live resources."""
        del ctx
