"""Every model-serving bundle declares how remote environments find its weights."""

from __future__ import annotations

from pathlib import Path

import pytest

from vs_runtime.api.infrastructure import ModelArtifactRequest, prepare_model_artifacts

_MODEL_SERVING = Path(__file__).parents[2] / "examples" / "model-serving"
_BUNDLES = sorted(path for path in _MODEL_SERVING.iterdir() if (path / "reference").is_dir())


class _NoDownload:
    def __call__(self, model_id: str, *, revision: str | None, cache_dir: str) -> Path:
        message = f"remote environments must not download {model_id}@{revision} into {cache_dir}"
        raise AssertionError(message)


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda path: path.name)
def test_remote_environment_prepares_weights_without_a_host_copy(
    bundle: Path, tmp_path: Path
) -> None:
    # Slurm and SkyPilot environments provision weights remotely; preparation
    # must succeed from the bundle's declared metadata alone.
    prepare_model_artifacts(
        ModelArtifactRequest(
            reference_dir=bundle / "reference",
            model_cache_dir=tmp_path / "cache",
            runtime_artifact_dir=tmp_path / "runtime",
            log=lambda _message: None,
        ),
        isolated=False,
        materialize_local_weights=False,
        downloader=_NoDownload(),
    )
