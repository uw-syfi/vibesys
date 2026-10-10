"""Public contract for immutable Docker image identity resolution."""

from __future__ import annotations

import pytest

from vs_sandbox.api.evaluator_tools import (
    EvaluatorToolError,
    resolve_docker_image_id,
)
from vs_sandbox.api.testing import (
    DockerCommandCall,
    DockerCommandOutcome,
    FakeDockerCommandRunner,
    docker_missing,
    docker_result,
    docker_timed_out,
)

_IMAGE = "example:latest"
_INSPECT = ("docker", "image", "inspect", "--format={{.Id}}", _IMAGE)
_PULL = ("docker", "image", "pull", _IMAGE)


def test_cached_immutable_identity_avoids_pull() -> None:
    runner = FakeDockerCommandRunner()
    runner.script(_INSPECT, docker_result(stdout="sha256:abc\n"))

    assert resolve_docker_image_id(_IMAGE, command_runner=runner) == "sha256:abc"
    assert runner.calls == [DockerCommandCall(arguments=_INSPECT, timeout_seconds=30)]


def test_missing_image_is_pulled_then_inspected_again() -> None:
    runner = FakeDockerCommandRunner()
    runner.script(
        _INSPECT,
        docker_result(1, stderr="missing"),
        docker_result(stdout="sha256:resolved\n"),
    )
    runner.script(_PULL, docker_result(stdout="pulled"))

    assert resolve_docker_image_id(_IMAGE, command_runner=runner) == "sha256:resolved"
    assert runner.calls == [
        DockerCommandCall(arguments=_INSPECT, timeout_seconds=30),
        DockerCommandCall(arguments=_PULL, timeout_seconds=600),
        DockerCommandCall(arguments=_INSPECT, timeout_seconds=30),
    ]


@pytest.mark.parametrize(
    "inspect_outcome",
    [
        docker_result(stdout="mutable-tag"),
        docker_result(stdout="sha256:has whitespace"),
        docker_result(1, stderr="missing"),
        docker_missing(),
        docker_timed_out(_INSPECT, 30),
    ],
)
def test_invalid_or_unavailable_cached_identity_requires_pull(
    inspect_outcome: DockerCommandOutcome,
) -> None:
    runner = FakeDockerCommandRunner()
    runner.script(_INSPECT, inspect_outcome, docker_result(stdout="sha256:pinned"))
    runner.script(_PULL, docker_result())

    assert resolve_docker_image_id(_IMAGE, command_runner=runner) == "sha256:pinned"
    assert runner.calls[1] == DockerCommandCall(arguments=_PULL, timeout_seconds=600)


@pytest.mark.parametrize(
    ("pull_outcome", "message"),
    [
        (docker_missing(), "Docker was not found while resolving the evaluator image"),
        (
            docker_timed_out(_PULL, 600),
            f"Docker image pull timed out: {_IMAGE}",
        ),
        (
            docker_result(1, stderr="no such image"),
            f"Could not resolve Docker image {_IMAGE!r}: no such image",
        ),
        (
            docker_result(1),
            f"Could not resolve Docker image {_IMAGE!r}: docker image pull failed",
        ),
    ],
)
def test_pull_failures_keep_the_evaluator_diagnostic(
    pull_outcome: DockerCommandOutcome,
    message: str,
) -> None:
    runner = FakeDockerCommandRunner()
    runner.script(_INSPECT, docker_result(1))
    runner.script(_PULL, pull_outcome)

    with pytest.raises(EvaluatorToolError) as raised:
        resolve_docker_image_id(_IMAGE, command_runner=runner)
    assert str(raised.value) == message


def test_successful_pull_without_an_immutable_identity_is_rejected() -> None:
    runner = FakeDockerCommandRunner()
    runner.script(
        _INSPECT,
        docker_result(1),
        docker_result(stdout="not-a-digest"),
    )
    runner.script(_PULL, docker_result(stdout="pulled"))

    with pytest.raises(
        EvaluatorToolError,
        match="no resolvable immutable image ID after pull",
    ):
        resolve_docker_image_id(_IMAGE, command_runner=runner)


def test_pull_failure_detail_is_bounded() -> None:
    runner = FakeDockerCommandRunner()
    runner.script(_INSPECT, docker_result(1))
    runner.script(_PULL, docker_result(1, stderr="x" * 800))

    with pytest.raises(EvaluatorToolError) as raised:
        resolve_docker_image_id(_IMAGE, command_runner=runner)

    detail = str(raised.value).partition(": ")[2]
    assert detail == "x" * 500
