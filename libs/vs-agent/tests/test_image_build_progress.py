"""An image build reports typed step observations parsed from Docker's progress output."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.images import ImageBuildProgress, TaskImageBuildError, agent_image
from vs_agent.api.testing import FakeDockerBuildRunner, docker_result
from vs_sim.api import DISCARD, Capture, OutputStream
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vs_agent.api.testing import DockerBuildResult
    from vs_sim.api import OutputSink

_TOTAL = 14


def _step_line(sequence: int, step: int, total: int, instruction: str) -> str:
    return f"#{sequence} [{step:>2}/{total}] {instruction}"


def _build(stderr: str, *, returncode: int = 0) -> list[ImageBuildProgress]:
    observed: list[ImageBuildProgress] = []
    runner = FakeDockerBuildRunner(build_result=docker_result(returncode=returncode, stderr=stderr))
    agent_image(
        "python:3.12-bookworm",
        command_runner=runner,
        on_progress=observed.append,
        clock=ManualClock(),
        output=DISCARD,
    )
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
        ImageBuildProgress(done=3, total=3, label="exporting to image"),
    ]


def test_the_export_after_the_last_step_is_one_observation_not_a_step() -> None:
    """Regression: a large base image spent twelve minutes exporting with no observation."""
    stderr = "\n".join(
        [
            _step_line(5, 3, 3, "RUN pip install --no-cache-dir uv"),
            "#8 exporting to image",
            "#8 exporting layers 603.8s done",
            "#8 exporting to image 604.0s done",
            "#9 exporting to image",
        ]
    )

    assert _build(stderr) == [
        ImageBuildProgress(done=2, total=3, label="RUN pip install --no-cache-dir uv"),
        ImageBuildProgress(done=3, total=3, label="exporting to image"),
    ]


def test_an_export_without_a_step_before_it_reports_nothing() -> None:
    assert _build("#8 exporting to image") == []


_LINES = st.one_of(
    st.builds(
        _step_line,
        st.integers(0, 99),
        st.integers(0, _TOTAL + 2),
        st.just(_TOTAL),
        st.text(st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=400),
    ),
    st.integers(0, 99).map(lambda sequence: f"#{sequence} CACHED"),
    st.integers(0, 99).map(lambda sequence: f"#{sequence} exporting to image"),
    st.text(st.characters(min_codepoint=32, max_codepoint=126)),
)


@given(lines=st.lists(_LINES, max_size=40))
def test_observations_only_move_forward_within_the_build(lines: list[str]) -> None:
    observed = _build("\n".join(lines))

    done = [progress.done for progress in observed]
    assert done == sorted(set(done))
    for progress in observed:
        exporting = progress.label == "exporting to image"
        assert 0 <= progress.done <= progress.total
        assert (progress.done == progress.total) == exporting
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
        agent_image(
            "python:3.12-bookworm",
            command_runner=runner,
            on_progress=observed.append,
            clock=ManualClock(),
            output=DISCARD,
        )

    assert observed == [ImageBuildProgress(done=1, total=3, label="RUN npm install")]


def test_a_build_with_no_sink_streams_nothing() -> None:
    runner = FakeDockerBuildRunner(
        build_result=docker_result(stderr=_step_line(6, 2, 3, "RUN npm install"))
    )

    assert agent_image(
        "python:3.12-bookworm",
        command_runner=runner,
        clock=ManualClock(),
        output=DISCARD,
    ).startswith("sha256:")


_STEP = re.compile(r"^#\d+ \[(?:\S+ )?\s*(\d+)/(\d+)\] (.+)$")
_EXPORT = re.compile(r"^#\d+ exporting to image")


def _expected_steps(lines: Sequence[str]) -> list[ImageBuildProgress]:
    """The observations a build reports for its stderr lines: the rules this suite pins."""
    expected: list[ImageBuildProgress] = []
    exported = False
    for line in lines:
        if expected and not exported and _EXPORT.match(line):
            exported = True
            total = expected[-1].total
            expected.append(ImageBuildProgress(done=total, total=total, label="exporting to image"))
            continue
        match = _STEP.match(line)
        if match is None:
            continue
        step, total = int(match.group(1)), int(match.group(2))
        done = step - 1
        if 1 <= step <= total and (not expected or done > expected[-1].done):
            expected.append(ImageBuildProgress(done=done, total=total, label=match.group(3)[:200]))
    return expected


class _ChunkingRunner:
    """Write each scripted command's output to the caller's sink in arbitrary chunks.

    A real runner delivers whatever the pipe returns; chunk boundaries carry no
    meaning, so a line or a UTF-8 character may be split anywhere.
    """

    def __init__(self, inner: FakeDockerBuildRunner, cuts: Sequence[int]) -> None:
        self._inner = inner
        self._cuts = cuts
        self.written = {stream: bytearray() for stream in OutputStream}

    def run(
        self, argv: Sequence[str], *, cwd: Path, timeout: float, output: OutputSink
    ) -> DockerBuildResult:
        scripted = Capture(limit=1 << 20)
        result = self._inner.run(argv, cwd=cwd, timeout=timeout, output=scripted)
        for stream in (OutputStream.STDERR, OutputStream.STDOUT):
            data = scripted.kept(stream)
            self.written[stream] += data
            start = 0
            for cut in sorted({cut % (len(data) + 1) for cut in self._cuts}):
                output.write(stream, data[start:cut])
                start = cut
            output.write(stream, data[start:])
        return result


@given(
    lines=st.lists(_LINES, max_size=40),
    stdout=st.text(max_size=200),
    cuts=st.lists(st.integers(0, 10_000), max_size=12),
)
def test_a_build_writes_every_byte_to_its_output_and_reads_its_steps_from_those_bytes(
    lines: list[str], stdout: str, cuts: list[int]
) -> None:
    stderr = "\n".join(lines)
    runner = _ChunkingRunner(
        FakeDockerBuildRunner(build_result=docker_result(stdout=stdout, stderr=stderr)), cuts
    )
    observed: list[ImageBuildProgress] = []
    output = Capture(limit=1 << 20)

    agent_image(
        "python:3.12-bookworm",
        command_runner=runner,
        on_progress=observed.append,
        output=output,
        clock=ManualClock(),
    )

    assert output.stdout == bytes(runner.written[OutputStream.STDOUT])
    assert output.stderr == bytes(runner.written[OutputStream.STDERR])
    assert stderr.encode() in output.stderr
    assert observed == _expected_steps(lines)
