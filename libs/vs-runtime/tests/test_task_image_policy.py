"""Properties of the one decision of which image a run environment's agent container builds from."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import (
    EditorImagePlan,
    RunEnvironmentSpec,
    TaskImageError,
    editor_image_plan,
    resolve_run_environment_spec,
)
from vs_sandbox.api import ComputeBackend

_CONTAINER_ENVIRONMENTS = ("docker", "modal", "skypilot", "slurm", "slurm-gpu")
_IMAGES = st.one_of(st.none(), st.text(min_size=1, max_size=20))
_DOCKERFILES = st.one_of(st.none(), st.just(Path("/project/.vibesys/tasks/t/Dockerfile")))
_LAUNCHES: st.SearchStrategy[Literal["fresh", "resume"]] = st.sampled_from(("fresh", "resume"))


@given(
    environment=st.sampled_from(_CONTAINER_ENVIRONMENTS),
    dockerfile=_DOCKERFILES,
    image=_IMAGES,
    base=_IMAGES,
    launch=_LAUNCHES,
)
def test_every_container_environment_decides_the_same_way(
    environment: str,
    dockerfile: Path | None,
    image: str | None,
    base: str | None,
    launch: Literal["fresh", "resume"],
) -> None:
    """Every container environment honors a task Dockerfile identically: none is special."""

    def decide(name: str) -> EditorImagePlan | str:
        try:
            return editor_image_plan(
                name, task_dockerfile=dockerfile, image=image, base_image=base, launch=launch
            )
        except TaskImageError as exc:
            return type(exc).__name__

    assert decide(environment) == decide("docker")


@given(
    environment=st.sampled_from(_CONTAINER_ENVIRONMENTS),
    dockerfile=_DOCKERFILES,
    image=_IMAGES,
    base=_IMAGES,
    launch=_LAUNCHES,
)
def test_a_task_dockerfile_is_built_on_the_backend_base_or_refused(
    environment: str,
    dockerfile: Path | None,
    image: str | None,
    base: str | None,
    launch: Literal["fresh", "resume"],
) -> None:
    """A declared Dockerfile is never dropped: the plan builds it or the call refuses."""
    try:
        plan = editor_image_plan(
            environment, task_dockerfile=dockerfile, image=image, base_image=base, launch=launch
        )
    except TaskImageError:
        assert dockerfile is not None
        # Fresh: an explicit image beside the Dockerfile. Resume: no recorded image.
        assert (image is not None) if launch == "fresh" else (image is None)
        return
    if dockerfile is None:
        assert plan == EditorImagePlan(image=image)
        assert plan.resolve(None) == image
        return
    assert plan.task_dockerfile == dockerfile
    assert plan.base_image == base


@given(recorded=st.text(min_size=1), built=st.text(min_size=1))
def test_resume_accepts_only_the_recorded_task_image(recorded: str, built: str) -> None:
    dockerfile = Path("/task/Dockerfile")
    plan = editor_image_plan(
        "slurm", task_dockerfile=dockerfile, image=recorded, base_image=None, launch="resume"
    )
    if built == recorded:
        assert plan.resolve(built) == recorded
    else:
        with pytest.raises(TaskImageError, match="changed since the run started"):
            plan.resolve(built)


@given(built=st.text(min_size=1))
def test_a_fresh_build_is_the_image(built: str) -> None:
    plan = editor_image_plan(
        "slurm", task_dockerfile=Path("/t/Dockerfile"), image=None, base_image="b", launch="fresh"
    )
    assert plan.resolve(built) == built


def test_the_host_environment_refuses_a_task_dockerfile() -> None:
    with pytest.raises(TaskImageError, match="runs the agent on the host"):
        editor_image_plan(
            "host",
            task_dockerfile=Path("/t/Dockerfile"),
            image=None,
            base_image=None,
            launch="fresh",
        )


def test_deriving_the_host_environment_refuses_a_built_task_image() -> None:
    """Regression: Metal on macOS dropped a task image when it derived ``host``."""
    spec = RunEnvironmentSpec(
        name="docker",
        options={"image": "sha256:" + "a" * 64},
        task_dockerfile=Path("/t/Dockerfile"),
    )
    with pytest.raises(TaskImageError, match="runs the agent on the host"):
        resolve_run_environment_spec(spec, ComputeBackend.METAL, platform="darwin", log=print)


def test_an_unknown_environment_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown run environment"):
        editor_image_plan(
            "local", task_dockerfile=None, image=None, base_image=None, launch="fresh"
        )
