"""An image build reports typed step observations parsed from Docker's progress output."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import ImageBuildProgress, TaskImageBuildError, agent_image
from vs_agent.api.testing import FakeDockerBuildRunner, docker_result

_TOTAL = 14


def _step_line(sequence: int, step: int, total: int, instruction: str) -> str:
    return f"#{sequence} [{step:>2}/{total}] {instruction}"


def _build(stderr: str, *, returncode: int = 0) -> list[ImageBuildProgress]:
    observed: list[ImageBuildProgress] = []
    runner = FakeDockerBuildRunner(build_result=docker_result(returncode=returncode, stderr=stderr))
    agent_image("python:3.12-bookworm", command_runner=runner, on_progress=observed.append)
    return observed


def test_each_step_docker_starts_is_one_observation() -> None:
    stderr = "\n".join(
        [
            '#0 building with "default" instance using docker driver',
            "#1 [internal] load build definition from agent.Dockerfile",
            _step_line(5, 1, 3, "FROM docker.io/library/python:3.12-bookworm"),
            _step_line(6, 2, 3, "RUN npm install -g --include=optional"),
            "#6 0.512 added 12 packages in 3s",
            _step_line(7, 3, 3, "RUN pip install --no-cache-dir uv"),
            "#8 exporting to image",
        ]
    )

    assert _build(stderr) == [
        ImageBuildProgress(done=0, total=3, label="FROM docker.io/library/python:3.12-bookworm"),
        ImageBuildProgress(done=1, total=3, label="RUN npm install -g --include=optional"),
        ImageBuildProgress(done=2, total=3, label="RUN pip install --no-cache-dir uv"),
    ]


_LINES = st.one_of(
    st.builds(
        _step_line,
        st.integers(0, 99),
        st.integers(0, _TOTAL + 2),
        st.just(_TOTAL),
        st.text(st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=400),
    ),
    st.integers(0, 99).map(lambda sequence: f"#{sequence} CACHED"),
    st.text(st.characters(min_codepoint=32, max_codepoint=126)),
)


@given(lines=st.lists(_LINES, max_size=40))
def test_observations_only_move_forward_within_the_build(lines: list[str]) -> None:
    observed = _build("\n".join(lines))

    done = [progress.done for progress in observed]
    assert done == sorted(set(done))
    for progress in observed:
        assert 0 <= progress.done < progress.total
        assert 0 < len(progress.label) <= 200


def test_a_failed_build_still_reports_the_steps_it_reached() -> None:
    observed: list[ImageBuildProgress] = []
    runner = FakeDockerBuildRunner(
        build_result=docker_result(
            returncode=1,
            stderr=_step_line(6, 2, 3, "RUN npm install") + "\nERROR: process did not complete",
        )
    )

    with pytest.raises(TaskImageBuildError, match="ERROR: process did not complete"):
        agent_image("python:3.12-bookworm", command_runner=runner, on_progress=observed.append)

    assert observed == [ImageBuildProgress(done=1, total=3, label="RUN npm install")]


def test_a_build_with_no_sink_streams_nothing() -> None:
    runner = FakeDockerBuildRunner(
        build_result=docker_result(stderr=_step_line(6, 2, 3, "RUN npm install"))
    )

    assert agent_image("python:3.12-bookworm", command_runner=runner).startswith("sha256:")
