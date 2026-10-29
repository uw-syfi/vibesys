"""Properties of the one decision of which image a run environment's agent container builds from."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import (
    EditorImagePlan,
    RunEnvironmentSpec,
    TaskImageError,
    TaskImageFacts,
    build_run_environment,
    editor_image_plan,
    require_built_task_image,
    resolve_run_environment_spec,
    run_environment_record,
    runs_agent_in_container,
)
from vs_sandbox.api import ComputeBackend

_ENVIRONMENTS = ("docker", "host", "modal", "skypilot", "slurm", "slurm-gpu")
_CONTAINER_ENVIRONMENTS = tuple(name for name in _ENVIRONMENTS if name != "host")
_DOCKERFILE = Path("/project/.vibesys/tasks/t/Dockerfile")
_REFERENCES = st.sampled_from(("vibesys-task-build:o-1", "vibesys-task-build:o-2"))
_LOCAL_ID = "sha256:" + "c" * 64
_IDS = st.sampled_from(("sha256:" + "a" * 64, "sha256:" + "b" * 64))
_BASES = st.one_of(st.none(), st.sampled_from(("nvcr.io/nvidia/pytorch:25.04-py3", "python:3")))


def _facts(
    reference: str, base: str | None, recorded: str | None, *, present: bool | None
) -> TaskImageFacts:
    is_reference = recorded is not None and recorded.startswith("vibesys-task-build:")
    return TaskImageFacts(
        reference=reference,
        base_image=base,
        recorded_is_reference=is_reference,
        # These fakes have no owner part: the whole reference is the inputs digest.
        recorded_inputs_match=is_reference and recorded == reference,
        recorded_id=_LOCAL_ID if present else None,
    )


def test_only_the_host_environment_runs_its_agent_outside_a_container() -> None:
    assert {name: runs_agent_in_container(name) for name in _ENVIRONMENTS} == {
        name: name != "host" for name in _ENVIRONMENTS
    }


@given(
    environment=st.sampled_from(_ENVIRONMENTS),
    image=st.one_of(st.none(), _REFERENCES, _IDS),
    launch=st.sampled_from(("fresh", "resume")),
)
def test_without_a_task_dockerfile_the_image_is_used_as_given(
    environment: str, image: str | None, launch: Literal["fresh", "resume"]
) -> None:
    plan = editor_image_plan(
        environment, task_dockerfile=None, image=image, launch=launch, facts=None
    )
    assert plan == EditorImagePlan(image=image)


_CASES = st.tuples(
    st.sampled_from(_ENVIRONMENTS),
    st.one_of(st.none(), _REFERENCES, _IDS),
    st.sampled_from(("fresh", "resume")),
    _REFERENCES,
    _BASES,
    st.booleans(),
)


@given(case=_CASES)
def test_a_task_dockerfile_is_honored_or_refused_never_dropped(
    case: tuple[str, str | None, Literal["fresh", "resume"], str, str | None, bool],
) -> None:
    environment, image, launch, reference, base, present = case
    facts = _facts(reference, base, image if launch == "resume" else None, present=present)
    try:
        plan = editor_image_plan(
            environment, task_dockerfile=_DOCKERFILE, image=image, launch=launch, facts=facts
        )
    except TaskImageError:
        refused_because = (
            environment == "host"
            or (launch == "fresh" and image is not None)
            or (
                launch == "resume"
                and (
                    image is None
                    or (facts.recorded_is_reference and image != reference and not present)
                )
            )
        )
        assert refused_because
        return
    assert environment != "host"
    assert plan.task_dockerfile == _DOCKERFILE
    if launch == "fresh":
        assert plan.image == reference
        assert plan.build is not None
        assert plan.build.base_image == base
        assert plan.resolve(_LOCAL_ID) == _LOCAL_ID
        return
    # A resume always records the recorded image, and builds only when it is gone.
    assert plan.image == image
    assert (plan.build is None) is present
    if present:
        assert plan.resolve(None) == _LOCAL_ID  # pinned to the local ID, not the tag
    if plan.build is not None and not facts.recorded_is_reference:
        assert plan.build.base_image is None  # built as before BASE_IMAGE
        assert plan.build.expected_id == image


@given(reference=_REFERENCES, base=_BASES, built=_IDS)
def test_unchanged_inputs_resume_on_a_new_image_id_when_the_cache_is_gone(
    reference: str, base: str | None, built: str
) -> None:
    """Regression: a pruned build cache gave a new image ID and resume was refused."""
    plan = editor_image_plan(
        "slurm",
        task_dockerfile=_DOCKERFILE,
        image=reference,
        launch="resume",
        facts=_facts(reference, base, reference, present=False),
    )

    assert plan.build is not None
    assert plan.notice is not None
    assert plan.image == reference
    assert plan.resolve(built) == built


def test_identical_inputs_at_another_location_resume() -> None:
    """Regression: a copied project's task, at another path, counted as changed inputs."""
    recorded = "vibesys-task-build:owner_a-inputs"
    facts = TaskImageFacts(
        reference="vibesys-task-build:owner_b-inputs",
        base_image=None,
        recorded_is_reference=True,
        recorded_inputs_match=True,
    )
    plan = editor_image_plan(
        "slurm", task_dockerfile=_DOCKERFILE, image=recorded, launch="resume", facts=facts
    )

    assert plan.image == recorded
    assert plan.build is not None
    assert plan.resolve(_LOCAL_ID) == _LOCAL_ID


def test_changed_inputs_resume_on_the_recorded_image_while_it_is_local() -> None:
    """Regression: a benchmark edit mid-campaign refused resume though the image was local."""
    plan = editor_image_plan(
        "docker",
        task_dockerfile=_DOCKERFILE,
        image="vibesys-task-build:o-1",
        launch="resume",
        facts=_facts("vibesys-task-build:o-2", None, "vibesys-task-build:o-1", present=True),
    )

    assert plan.build is None
    assert plan.notice is not None
    assert "inputs changed" in plan.notice
    assert plan.resolve(None) == _LOCAL_ID


def test_changed_inputs_are_refused_on_resume_once_the_image_is_gone() -> None:
    with pytest.raises(TaskImageError, match="inputs changed"):
        editor_image_plan(
            "docker",
            task_dockerfile=_DOCKERFILE,
            image="vibesys-task-build:o-1",
            launch="resume",
            facts=_facts("vibesys-task-build:o-2", None, "vibesys-task-build:o-1", present=False),
        )


@given(recorded=_IDS, built=_IDS)
def test_a_pre_reference_run_resumes_only_on_its_recorded_id(recorded: str, built: str) -> None:
    plan = editor_image_plan(
        "docker",
        task_dockerfile=_DOCKERFILE,
        image=recorded,
        launch="resume",
        facts=_facts("vibesys-task-build:o-1", "python:3", recorded, present=False),
    )
    if built == recorded:
        assert plan.resolve(built) == recorded
    else:
        with pytest.raises(TaskImageError, match="predates input-keyed task images"):
            plan.resolve(built)


@given(
    environment=st.sampled_from(_ENVIRONMENTS),
    backend=st.sampled_from(tuple(ComputeBackend)),
    platform=st.sampled_from(("linux", "darwin")),
)
def test_a_selection_that_derives_nothing_is_returned_unchanged(
    environment: str, backend: ComputeBackend, platform: str
) -> None:
    """Regression: an explicitly selected ``host`` spec lost its options (its injected
    sandbox builder) as if it had been derived from ``docker``."""
    spec = RunEnvironmentSpec(name=environment, options={"marker": object()})
    resolved = resolve_run_environment_spec(
        spec, backend, task_dockerfile=None, platform=platform, log=print
    )
    derived = environment == "docker" and platform == "darwin" and resolved.name == "host"
    assert derived or resolved is spec


def test_a_selected_host_environment_refuses_a_task_dockerfile() -> None:
    spec = RunEnvironmentSpec(name="host", task_dockerfile=_DOCKERFILE)
    with pytest.raises(TaskImageError, match="runs the agent on the host"):
        resolve_run_environment_spec(
            spec, ComputeBackend.CPU, task_dockerfile=_DOCKERFILE, platform="linux", log=print
        )


def test_deriving_the_host_environment_refuses_a_built_task_image() -> None:
    """Regression: Metal on macOS dropped a task image when it derived ``host``."""
    spec = RunEnvironmentSpec(
        name="docker", options={"image": "vibesys-task-build:o-1"}, task_dockerfile=_DOCKERFILE
    )
    with pytest.raises(TaskImageError, match="runs the agent on the host"):
        resolve_run_environment_spec(
            spec, ComputeBackend.METAL, task_dockerfile=_DOCKERFILE, platform="darwin", log=print
        )


@given(
    environment=st.sampled_from(_CONTAINER_ENVIRONMENTS),
    backend=st.sampled_from(tuple(ComputeBackend)),
    platform=st.sampled_from(("linux", "darwin")),
)
def test_a_selection_not_built_from_the_task_dockerfile_is_refused(
    environment: str, backend: ComputeBackend, platform: str
) -> None:
    """Regression: a run opened with a task Dockerfile but no task image ignored it."""
    spec = RunEnvironmentSpec(name=environment, options={})
    with pytest.raises(TaskImageError, match="without building it"):
        resolve_run_environment_spec(
            spec, backend, task_dockerfile=_DOCKERFILE, platform=platform, log=print
        )


@pytest.mark.parametrize("environment", _CONTAINER_ENVIRONMENTS)
@pytest.mark.parametrize("image", [None, "vibesys-task-build:o-1"])
def test_a_run_opens_only_with_its_task_image_built(environment: str, image: str | None) -> None:
    options = {} if image is None else {"image": image}
    spec = RunEnvironmentSpec(name=environment, options=options, task_dockerfile=_DOCKERFILE)
    with pytest.raises(TaskImageError, match="without building it"):
        require_built_task_image(spec, _DOCKERFILE)  # no resolved image ID
    if image is not None:
        require_built_task_image(replace(spec, image_id=_LOCAL_ID), _DOCKERFILE)
    require_built_task_image(RunEnvironmentSpec(name=environment), None)


def test_an_unknown_environment_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown run environment"):
        editor_image_plan("local", task_dockerfile=None, image=None, launch="fresh", facts=None)


@pytest.mark.parametrize("environment", ["docker", "modal", "slurm"])
def test_a_container_environment_runs_its_backend_on_the_image_option(
    environment: str, tmp_path: Path
) -> None:
    """Regression: the Slurm editor ignored the task image and built from the backend default."""
    image = "vibesys-task-build:o-1"
    options: dict[str, object] = {"image": image}
    if environment == "slurm":
        config = tmp_path / "slurm.toml"
        config.write_text(
            '[slurm]\nname = "cluster"\nremote_workspace_root = "/remote/runs"\n'
            '[slurm.transport]\nkind = "ssh"\nhost = "cluster"\n',
            encoding="utf-8",
        )
        options["config_path"] = str(config)
    spec = RunEnvironmentSpec(name=environment, options=options)

    assert build_run_environment(spec).backend_image == image
    assert run_environment_record(spec).image == image
    # A resolved ID is what runs; the record keeps the reference.
    pinned = replace(spec, image_id=_LOCAL_ID)
    assert build_run_environment(pinned).backend_image == _LOCAL_ID
    assert run_environment_record(pinned).image == image
