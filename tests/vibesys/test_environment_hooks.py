from pathlib import Path
from unittest.mock import patch

import pytest

from vibesys.domains.environment import (
    EnvironmentBindMount,
    EnvironmentContext,
    NoopEnvironmentHooks,
)
from vibesys.domains.llm_serving.hooks import LLMServingEnvironmentHooks


class _RunEnvironment:
    def __init__(
        self,
        *,
        isolated: bool = True,
        materialize_local_model_weights: bool = True,
        provides_remote_model_weights: bool = False,
    ) -> None:
        self.isolated = isolated
        self.materialize_local_model_weights = materialize_local_model_weights
        self.provides_remote_model_weights = provides_remote_model_weights


def _ctx(  # noqa: PLR0913
    reference_path: Path,
    tmp_path: Path,
    *,
    isolated: bool = True,
    materialize_local_model_weights: bool = True,
    provides_remote_model_weights: bool = False,
    runtime_artifact_dir: Path | None = None,
) -> EnvironmentContext:
    return EnvironmentContext(
        reference_path=reference_path,
        workspace=tmp_path / "workspace",
        run_environment=_RunEnvironment(
            isolated=isolated,
            materialize_local_model_weights=materialize_local_model_weights,
            provides_remote_model_weights=provides_remote_model_weights,
        ),
        project_root=tmp_path / "project",
        model_cache_dir=tmp_path / "runs" / ".cache" / "huggingface",
        runtime_artifact_dir=runtime_artifact_dir or reference_path,
        log=lambda _msg: None,
    )


def test_noop_environment_hooks_return_empty_patch(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()

    patch = NoopEnvironmentHooks().prepare(_ctx(ref_dir, tmp_path))

    assert patch.copy_excludes == frozenset()
    assert patch.bind_mounts == ()


def test_llm_serving_hooks_require_model_artifacts_for_reference_dir(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "reference.py").write_text("pass\n")

    with pytest.raises(FileNotFoundError, match="Model weights not found"):
        LLMServingEnvironmentHooks().prepare(_ctx(ref_dir, tmp_path))


def test_llm_serving_hooks_skip_local_weights_when_environment_provides_them_remotely(
    tmp_path: Path,
) -> None:
    """A remote environment (e.g. skypilot) must not require local weights.

    Neither a local ``model/`` directory nor a ``meta.json`` is present, which
    would otherwise force ``_ensure_model_weights`` regardless of
    ``materialize_local_model_weights``. When the run environment declares
    ``provides_remote_model_weights``, weights already live on the remote
    execution surface (persistent cluster storage) and the candidate transfer
    deliberately excludes them, so ``prepare`` must not error and must not
    invent a ``/model`` bind mount.
    """
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "reference.py").write_text("pass\n")

    patch = LLMServingEnvironmentHooks().prepare(
        _ctx(
            ref_dir,
            tmp_path,
            materialize_local_model_weights=False,
            provides_remote_model_weights=True,
        )
    )

    assert patch.bind_mounts == ()


def test_llm_serving_hooks_still_mount_a_committed_model_under_a_remote_environment(
    tmp_path: Path,
) -> None:
    """A remote environment still honors a model an operator did commit locally.

    ``provides_remote_model_weights`` only exempts the hook from *requiring*
    local weights; it must not hide a real ``model/`` directory that is
    already present, so the resolved path still propagates to the evaluator
    as a bind mount when one exists.
    """
    ref_dir = tmp_path / "reference"
    model_dir = ref_dir / "model"
    model_dir.mkdir(parents=True)

    patch = LLMServingEnvironmentHooks().prepare(
        _ctx(
            ref_dir,
            tmp_path,
            materialize_local_model_weights=False,
            provides_remote_model_weights=True,
        )
    )

    assert patch.bind_mounts == (EnvironmentBindMount(model_dir, "/model", True),)  # noqa: FBT003


def test_llm_serving_hooks_return_model_mount_and_isolated_copy_excludes(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    ref_dir = tmp_path / "reference"
    model_dir = ref_dir / "model"
    model_dir.mkdir(parents=True)
    (ref_dir / "reference.py").write_text("pass\n")

    patch = LLMServingEnvironmentHooks().prepare(
        _ctx(ref_dir, tmp_path, materialize_local_model_weights=False)
    )

    assert patch.copy_excludes == frozenset({"model", "draft_model"})
    assert patch.bind_mounts == (EnvironmentBindMount(model_dir, "/model", True),)  # noqa: FBT003  # tracked: #288


def test_llm_serving_hooks_prefer_existing_authored_model(tmp_path: Path) -> None:
    ref_dir = tmp_path / "reference"
    model_dir = ref_dir / "model"
    model_dir.mkdir(parents=True)
    runtime_artifacts = tmp_path / "state" / "cache" / "llm-serving"

    patch = LLMServingEnvironmentHooks().prepare(
        _ctx(
            ref_dir,
            tmp_path,
            runtime_artifact_dir=runtime_artifacts,
        )
    )

    assert patch.bind_mounts == (EnvironmentBindMount(model_dir, "/model", True),)  # noqa: FBT003
    assert not runtime_artifacts.exists()


def test_llm_serving_hooks_keep_model_in_local_workspace_copy(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    ref_dir = tmp_path / "reference"
    (ref_dir / "model").mkdir(parents=True)
    (ref_dir / "reference.py").write_text("pass\n")

    patch = LLMServingEnvironmentHooks().prepare(
        _ctx(
            ref_dir,
            tmp_path,
            isolated=False,
            materialize_local_model_weights=False,
        )
    )

    assert patch.copy_excludes == frozenset()


def test_llm_serving_model_download_uses_shared_runs_cache(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "meta.json").write_text('{"model_id": "org/model", "revision": "abc"}')
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()

    with patch(
        "huggingface_hub.snapshot_download",
        return_value=str(downloaded),
    ) as snapshot_download:
        LLMServingEnvironmentHooks().prepare(_ctx(ref_dir, tmp_path))

    snapshot_download.assert_called_once_with(
        "org/model",
        revision="abc",
        cache_dir=str(tmp_path / "runs" / ".cache" / "huggingface"),
    )
    assert (ref_dir / "model").resolve() == downloaded


def test_llm_serving_model_download_can_materialize_outside_reference(tmp_path: Path) -> None:
    ref_dir = tmp_path / "project" / ".vibesys" / "tasks" / "serve" / "reference"
    ref_dir.mkdir(parents=True)
    (ref_dir / "meta.json").write_text('{"model_id": "org/model", "revision": "abc"}')
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    runtime_artifacts = (
        tmp_path / "project" / ".vibesys" / "state" / "local" / "cache" / "llm-serving"
    )

    with patch("huggingface_hub.snapshot_download", return_value=str(downloaded)):
        environment_patch = LLMServingEnvironmentHooks().prepare(
            _ctx(
                ref_dir,
                tmp_path,
                runtime_artifact_dir=runtime_artifacts,
            )
        )

    runtime_model = runtime_artifacts / "model"
    assert not (ref_dir / "model").exists()
    assert runtime_model.resolve() == downloaded
    assert environment_patch.bind_mounts == (
        EnvironmentBindMount(runtime_model, "/model", True),  # noqa: FBT003
    )
