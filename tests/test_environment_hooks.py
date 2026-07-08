from pathlib import Path

import pytest

from vibe_serve.domains.environment import (
    EnvironmentBindMount,
    EnvironmentContext,
    NoopEnvironmentHooks,
)
from vibe_serve.domains.llm_serving.hooks import LLMServingEnvironmentHooks


class _RunEnvironment:
    def __init__(
        self,
        *,
        isolated: bool = True,
        materialize_local_model_weights: bool = True,
    ) -> None:
        self.isolated = isolated
        self.materialize_local_model_weights = materialize_local_model_weights


def _ctx(
    reference_path: Path,
    tmp_path: Path,
    *,
    isolated: bool = True,
    materialize_local_model_weights: bool = True,
) -> EnvironmentContext:
    return EnvironmentContext(
        reference_path=reference_path,
        workspace=tmp_path / "workspace",
        run_environment=_RunEnvironment(
            isolated=isolated,
            materialize_local_model_weights=materialize_local_model_weights,
        ),
        project_root=tmp_path / "project",
        log=lambda _msg: None,
    )


def test_noop_environment_hooks_return_empty_patch(tmp_path):
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()

    patch = NoopEnvironmentHooks().prepare(_ctx(ref_dir, tmp_path))

    assert patch.copy_excludes == frozenset()
    assert patch.bind_mounts == ()


def test_llm_serving_hooks_require_model_artifacts_for_reference_dir(tmp_path):
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "reference.py").write_text("pass\n")

    with pytest.raises(FileNotFoundError, match="Model weights not found"):
        LLMServingEnvironmentHooks().prepare(_ctx(ref_dir, tmp_path))


def test_llm_serving_hooks_return_model_mount_and_isolated_copy_excludes(tmp_path):
    ref_dir = tmp_path / "reference"
    model_dir = ref_dir / "model"
    model_dir.mkdir(parents=True)
    (ref_dir / "reference.py").write_text("pass\n")

    patch = LLMServingEnvironmentHooks().prepare(
        _ctx(ref_dir, tmp_path, materialize_local_model_weights=False)
    )

    assert patch.copy_excludes == frozenset({"model", "draft_model"})
    assert patch.bind_mounts == (EnvironmentBindMount(model_dir, "/model", True),)


def test_llm_serving_hooks_keep_model_in_local_workspace_copy(tmp_path):
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
