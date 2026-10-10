"""The scripted Docker CLI records every call and answers only what a test scripted."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.testing import (
    DockerCliCall,
    ScriptedDockerCli,
    docker_missing,
    docker_result,
    docker_timed_out,
)

_ARGV = st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=5)
_RESULTS = st.builds(
    docker_result,
    returncode=st.integers(0, 255),
    stdout=st.text(max_size=12),
    stderr=st.text(max_size=12),
)


@given(argvs=st.lists(_ARGV, max_size=6), timeout=st.floats(0.1, 600))
def test_every_call_is_recorded_in_order_with_its_timeout(
    argvs: list[list[str]], timeout: float
) -> None:
    docker = ScriptedDockerCli()
    docker.always(docker_result())

    for argv in argvs:
        docker.run(argv, timeout_seconds=timeout)

    assert docker.calls == [DockerCliCall(tuple(argv), timeout) for argv in argvs]
    assert docker.argvs == [tuple(argv) for argv in argvs]


@given(results=st.lists(_RESULTS, min_size=1, max_size=6))
def test_sequenced_outcomes_replay_in_order_then_run_dry(results: list[object]) -> None:
    docker = ScriptedDockerCli()
    docker.then(*results)  # ty: ignore[invalid-argument-type]

    replayed = [docker.run(["docker", "ps"], timeout_seconds=1) for _ in results]

    assert replayed == results
    with pytest.raises(AssertionError, match="no scripted Docker CLI outcome"):
        docker.run(["docker", "ps"], timeout_seconds=1)
    assert len(docker.calls) == len(results) + 1


def test_an_unscripted_call_is_still_recorded() -> None:
    docker = ScriptedDockerCli()

    with pytest.raises(AssertionError):
        docker.run(["docker", "info"], timeout_seconds=5)

    assert docker.argvs == [("docker", "info")]


@given(first=_RESULTS, second=_RESULTS)
def test_the_longest_matching_prefix_wins_and_its_last_outcome_repeats(
    first: object, second: object
) -> None:
    docker = ScriptedDockerCli()
    docker.always(docker_result(stdout="fallback"))
    docker.on(("docker",), docker_result(stdout="any docker"))
    docker.on(("docker", "rm"), first, second)  # ty: ignore[invalid-argument-type]

    answers = [docker.run(["docker", "rm", "-f", "c"], timeout_seconds=1) for _ in range(3)]

    assert answers == [first, second, second]
    assert docker.run(["docker", "ps"], timeout_seconds=1).stdout == "any docker"
    assert docker.run(["podman", "ps"], timeout_seconds=1).stdout == "fallback"


def test_scripted_failures_are_raised() -> None:
    docker = ScriptedDockerCli()
    docker.on(("docker", "run"), docker_timed_out(["docker", "run"], 120))
    docker.on(("docker", "stop"), docker_missing())
    docker.on(("docker", "rm"), KeyboardInterrupt())

    with pytest.raises(Exception, match="timed out") as timed_out:
        docker.run(["docker", "run", "img"], timeout_seconds=120)
    assert timed_out.value.timeout == 120  # ty: ignore[unresolved-attribute]
    with pytest.raises(FileNotFoundError):
        docker.run(["docker", "stop", "c"], timeout_seconds=30)
    with pytest.raises(KeyboardInterrupt):
        docker.run(["docker", "rm", "c"], timeout_seconds=10)


def test_clear_script_drops_answers_but_keeps_history() -> None:
    docker = ScriptedDockerCli()
    docker.always(docker_result())
    docker.run(["docker", "ps"], timeout_seconds=1)

    docker.clear_script()

    with pytest.raises(AssertionError):
        docker.run(["docker", "ps"], timeout_seconds=1)
    assert len(docker.calls) == 2


def test_spawn_is_recorded_without_a_timeout_and_refused() -> None:
    docker = ScriptedDockerCli()

    with pytest.raises(AssertionError, match="FakeDockerEngine"):
        docker.spawn(["docker", "exec", "c", "true"])

    assert docker.calls == [DockerCliCall(("docker", "exec", "c", "true"), None)]


def test_an_empty_prefix_script_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one outcome"):
        ScriptedDockerCli().on(("docker",))
