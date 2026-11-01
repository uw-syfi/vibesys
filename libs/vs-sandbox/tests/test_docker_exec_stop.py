"""A stop request to a container's command is retried until one answers, and never guesses.

The answers come from the scripted ``docker`` CLI, so a request that fails, times out or
answers outside the script's protocol costs nothing, and the bound on attempts is a count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.testing import ScriptedDockerCli, docker_result, docker_timed_out

# test-isolation: the exec stop adapter and its attempt bound are internal to the Docker sandbox; this pins how a stop answers.
from vs_sandbox.docker_sandbox import _SIGNAL_ATTEMPTS, _ExecStop

# test-isolation: the stop signals and the remote-processes answer are internal to the command runners; the stop adapter above speaks them.
from vs_sandbox.process_execution import KILL, TERMINATE, RemoteProcesses, StopSignal

if TYPE_CHECKING:
    import subprocess

type Failure = Literal["nonzero", "oserror", "timeout", "garbled"]
_FAILURES: list[Failure] = ["nonzero", "oserror", "timeout", "garbled"]
_CONTAINER = "c0ffee"
_EXEC_ID = "exec-1"


def _failing(kind: Failure) -> subprocess.CompletedProcess[str] | BaseException:
    if kind == "oserror":
        return OSError("cannot reach the daemon")
    if kind == "timeout":
        return docker_timed_out(["docker", "exec"], 30)
    if kind == "garbled":
        return docker_result(stdout="not the script's line\n")
    return docker_result(returncode=1, stderr="signal request failed\n")


def _answer(survivors: int | None) -> subprocess.CompletedProcess[str]:
    return docker_result(stdout=f"{'unknown' if survivors is None else survivors} -\n")


@given(
    failures=st.lists(st.sampled_from(_FAILURES), max_size=_SIGNAL_ATTEMPTS + 2),
    survivors=st.one_of(st.none(), st.integers(0, 4)),
    number=st.sampled_from([TERMINATE, KILL]),
)
def test_a_stop_request_is_retried_through_failures_and_knows_nothing_after_the_last_attempt(
    failures: list[Failure], survivors: int | None, number: StopSignal
) -> None:
    docker = ScriptedDockerCli()
    docker.then(*(_failing(kind) for kind in failures), _answer(survivors))
    docker.always(_failing("nonzero"))
    stop = _ExecStop(docker, _CONTAINER, _EXEC_ID, None)

    reply = stop.send(number)

    answered = len(failures) < _SIGNAL_ATTEMPTS
    assert reply == RemoteProcesses(survivors if answered else None)
    assert len(docker.calls) == (len(failures) + 1 if answered else _SIGNAL_ATTEMPTS)
    for argv in docker.argvs:
        assert argv[:2] == ("docker", "exec")
        assert _CONTAINER in argv
        assert f"VIBESYS_EXEC_ID={_EXEC_ID}" in argv
        assert str(number) in argv


def test_an_answer_of_unknown_is_not_retried_because_the_script_already_waited() -> None:
    docker = ScriptedDockerCli()
    docker.always(_answer(None))

    assert _ExecStop(docker, _CONTAINER, _EXEC_ID, None).send(KILL) == RemoteProcesses(None)
    assert len(docker.calls) == 1


@given(sessions=st.lists(st.integers(1, 99_999).map(str), min_size=1, max_size=3, unique=True))
def test_what_one_request_found_is_handed_to_the_next(sessions: list[str]) -> None:
    """A later request still finds a session whose marked processes have already exited."""
    docker = ScriptedDockerCli()
    docker.then(
        docker_result(stdout=f"1 4242 {' '.join(sessions)}\n"), docker_result(stdout="0 4242\n")
    )
    stop = _ExecStop(docker, _CONTAINER, _EXEC_ID, None)

    stop.send(TERMINATE)
    stop.send(KILL)

    assert docker.argvs[1][-2:] == (" ".join(sessions), "4242")
