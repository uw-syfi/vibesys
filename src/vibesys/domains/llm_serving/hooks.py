"""LLM-serving environment setup/teardown hooks."""

from __future__ import annotations

import json
from collections.abc import Callable  # noqa: TC003  # tracked: #288
from pathlib import Path  # noqa: TC003  # tracked: #288

from vibesys.domains.environment import (
    EnvironmentBindMount,
    EnvironmentContext,
    EnvironmentPatch,
)


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
        raise FileNotFoundError(  # noqa: TRY003  # tracked: #288
            f"Model weights not found at {model_path} and no meta.json to download from. "
            f"Either create a model/ directory/symlink or add a meta.json with model_id."
        )

    meta = json.loads(meta_path.read_text())
    model_id = meta.get("model_id")
    if not model_id:
        raise ValueError(f"meta.json at {meta_path} missing required 'model_id' field")  # noqa: TRY003  # tracked: #288

    revision = meta.get("revision")
    log(f"[model] Weights not found at {model_path}. Downloading {model_id} to {cache_dir}...")
    from huggingface_hub import snapshot_download  # noqa: PLC0415  # tracked: #288

    downloaded_path = snapshot_download(model_id, revision=revision, cache_dir=str(cache_dir))
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.symlink_to(downloaded_path)
    log(f"[model] Created symlink {model_path} -> {downloaded_path}")
    return model_path


class LLMServingEnvironmentHooks:  # noqa: D101  # tracked: #288
    _MODEL_ARTIFACT_NAMES = frozenset({"model", "draft_model"})

    def prepare(self, ctx: EnvironmentContext) -> EnvironmentPatch:  # noqa: D102  # tracked: #288
        ref_path = ctx.reference_path
        if not ref_path.is_dir():
            return EnvironmentPatch()

        model_path = ref_path / "model"
        meta_path = ref_path / "meta.json"
        if not ctx.run_environment.provides_remote_model_weights and (
            ctx.run_environment.materialize_local_model_weights or not meta_path.exists()
        ):
            model_path = _ensure_model_weights(
                ref_path,
                cache_dir=ctx.model_cache_dir,
                runtime_artifact_dir=ctx.runtime_artifact_dir,
                log=ctx.log,
            )

        bind_mounts: list[EnvironmentBindMount] = []
        if model_path.is_dir() or model_path.is_symlink():
            bind_mounts.append(EnvironmentBindMount(model_path, "/model", True))  # noqa: FBT003  # tracked: #288

        draft_model_path = ref_path / "draft_model"
        if draft_model_path.is_dir() or draft_model_path.is_symlink():
            bind_mounts.append(EnvironmentBindMount(draft_model_path, "/draft_model", True))  # noqa: FBT003  # tracked: #288

        return EnvironmentPatch(
            copy_excludes=self._MODEL_ARTIFACT_NAMES
            if ctx.run_environment.isolated
            else frozenset(),
            bind_mounts=tuple(bind_mounts),
        )

    def teardown(self, ctx: EnvironmentContext) -> None:  # noqa: ARG002, D102  # tracked: #288
        return None
