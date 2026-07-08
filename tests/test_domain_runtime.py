from pathlib import Path

import pytest

from vibe_serve.domain_runtime import (
    DomainBindMount,
    DomainSetupContext,
    LLMServingDomainRuntime,
    NoopDomainRuntime,
    runtime_for_domain_name,
)


class _RunEnvironment:
    def __init__(self, *, materialize_local_model_weights: bool) -> None:
        self.materialize_local_model_weights = materialize_local_model_weights


def _ctx(
    reference_path: Path,
    tmp_path: Path,
    *,
    materialize_local_model_weights: bool = True,
) -> DomainSetupContext:
    return DomainSetupContext(
        reference_path=reference_path,
        workspace=tmp_path / "workspace",
        run_environment=_RunEnvironment(
            materialize_local_model_weights=materialize_local_model_weights
        ),
        project_root=tmp_path / "project",
        log=lambda _msg: None,
    )


def test_generic_domain_runtime_is_noop(tmp_path):
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()

    patch = NoopDomainRuntime().prepare_environment(_ctx(ref_dir, tmp_path))

    assert patch.copy_excludes == frozenset()
    assert patch.bind_mounts == ()


def test_llm_serving_runtime_requires_model_artifacts_for_reference_dir(tmp_path):
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "reference.py").write_text("pass\n")

    with pytest.raises(FileNotFoundError, match="Model weights not found"):
        LLMServingDomainRuntime().prepare_environment(_ctx(ref_dir, tmp_path))


def test_llm_serving_runtime_returns_model_mount_and_copy_excludes(tmp_path):
    ref_dir = tmp_path / "reference"
    model_dir = ref_dir / "model"
    model_dir.mkdir(parents=True)
    (ref_dir / "reference.py").write_text("pass\n")

    patch = LLMServingDomainRuntime().prepare_environment(
        _ctx(ref_dir, tmp_path, materialize_local_model_weights=False)
    )

    assert patch.copy_excludes == frozenset({"model", "draft_model"})
    assert patch.bind_mounts == (DomainBindMount(model_dir, "/model", True),)


def test_runtime_for_domain_name_maps_only_llm_serving_to_model_runtime():
    assert isinstance(runtime_for_domain_name("llm-serving"), LLMServingDomainRuntime)
    assert isinstance(runtime_for_domain_name("generic"), NoopDomainRuntime)
