"""Starting a harness's login: the person is told what the CLI asked, and the child is kept.

The CLI is a ``ScriptedPipedProcess`` whose output the test writes, and time is ``SimThreads``',
so no process runs and no wait is real.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from server.host_agent_logins import LoginStarted, LoginUnavailable, LoginUnavailableReason
from server.host_helper_os import LoginBounds, LoginStarter, login_instruction
from vs_sim.api.testing import (
    HANG_GUARD_S,
    FakePipedProcessLauncher,
    PipedStart,
    ScriptedPipedProcess,
    SimThreads,
)

_URL = st.from_regex(r"https://[a-z]{3,10}\.example/[a-z]{1,8}", fullmatch=True)
_CODE = st.from_regex(r"[A-Z0-9]{4}-[A-Z0-9]{5}", fullmatch=True)
_NOISE = st.text(alphabet=string.ascii_letters + " .,", min_size=0, max_size=40)
_ARGV = ("codex", "login", "--device-auth")
_BOUNDS = LoginBounds(page_wait=10.0, code_wait=1.0, lifetime=900.0)
_ENV = {"PATH": "/usr/bin"}


@dataclass
class _Started:
    """What starting a login did, measured inside the simulated run."""

    result: LoginStarted | LoginUnavailable
    started: list[PipedStart]
    process: ScriptedPipedProcess
    waited: float


def _start(output: str, *, exit_status: int | None = None, then_wait: float = 0.0) -> _Started:
    """Start ``_ARGV`` against a CLI that prints ``output``, then let ``then_wait`` seconds pass."""
    threads = SimThreads()
    process = ScriptedPipedProcess(threads, merge_stderr=True)
    process.write(stdout=output)
    if exit_status is not None:
        process.exit(exit_status)
    launcher = FakePipedProcessLauncher(lambda _start: process)
    starter = LoginStarter(launcher, threads, lambda: _ENV, _BOUNDS)

    def main() -> _Started:
        began = threads.now()
        result = starter.start(_ARGV)
        waited = threads.now() - began
        threads.sleep(then_wait)
        return _Started(result, launcher.started, process, waited)

    return threads.run(main)


@given(url=_URL, code=_CODE, before=_NOISE, between=_NOISE)
def test_the_page_and_the_code_the_cli_prints_are_what_the_person_is_told(
    url: str, code: str, before: str, between: str
) -> None:
    output = f"\x1b[1m{before}\x1b[0m\n1. Open \x1b[94m{url}\x1b[0m\n{between}\n2. Enter {code}\n"

    done = _start(output)

    assert done.result == LoginStarted(instruction=f"Open {url} and enter the code {code}")
    assert login_instruction(url, code) == f"Open {url} and enter the code {code}"
    assert [(s.argv, s.env, s.merge_stderr) for s in done.started] == [(_ARGV, _ENV, True)]
    assert done.process.signals == []  # the login keeps running for the person to finish it


@given(url=_URL)
def test_a_page_with_no_code_is_just_the_page_after_a_short_wait(url: str) -> None:
    done = _start(f"Visit {url}\n")

    assert done.result == LoginStarted(instruction=f"Open {url}")
    assert done.waited == pytest.approx(_BOUNDS.code_wait)


def test_a_cli_that_shows_nothing_is_started_after_the_bound_and_not_failed() -> None:
    done = _start("")

    assert done.result == LoginStarted(instruction=None)
    assert done.waited == pytest.approx(_BOUNDS.page_wait)
    assert done.process.signals == []


@given(message=st.text(alphabet=string.ascii_letters, min_size=1, max_size=60))
def test_a_cli_that_ends_with_a_complaint_and_no_page_could_not_start(message: str) -> None:
    done = _start(f"  {message}  \n", exit_status=1)

    assert done.result == LoginUnavailable(
        reason=LoginUnavailableReason.START_FAILED, detail=message
    )


def test_a_program_that_cannot_run_is_a_failed_start_naming_it() -> None:
    threads = SimThreads()

    def refuse(_start: object) -> ScriptedPipedProcess:
        raise FileNotFoundError(2, "No such file or directory")

    starter = LoginStarter(FakePipedProcessLauncher(refuse), threads, lambda: _ENV, _BOUNDS)

    result = threads.run(lambda: starter.start(_ARGV))

    assert isinstance(result, LoginUnavailable)
    assert result.reason is LoginUnavailableReason.START_FAILED
    assert "codex" in result.detail


def test_cancelling_a_login_ends_its_group_before_its_lifetime() -> None:
    threads = SimThreads()
    process = ScriptedPipedProcess(threads, merge_stderr=True)
    process.write(stdout="Open https://a.example/x\n")
    starter = LoginStarter(
        FakePipedProcessLauncher(lambda _start: process), threads, lambda: _ENV, _BOUNDS
    )

    def main() -> list[str]:
        held = starter.hold(_ARGV)
        assert isinstance(held.result, LoginStarted)
        assert process.signals == []
        held.cancel()
        waiter = threads.spawn(held.wait, name="login-finished")
        waiter.join(HANG_GUARD_S)
        assert not waiter.is_alive()
        return process.signals

    assert threads.run(main) == ["terminate"]
    assert process.reaped


def test_a_login_nobody_finishes_is_ended_after_its_lifetime_and_reaped() -> None:
    done = _start("Open https://a.example/x\n", then_wait=_BOUNDS.lifetime + 1.0)

    assert done.process.signals == ["terminate"]
    assert done.process.reaped


def test_a_login_the_person_finishes_in_time_is_not_signalled() -> None:
    done = _start("Open https://a.example/x\n", exit_status=0, then_wait=_BOUNDS.lifetime + 1.0)

    assert done.process.signals == []
    assert done.process.reaped


@given(url=st.text(alphabet=string.ascii_lowercase, min_size=300, max_size=400))
def test_an_instruction_too_long_for_a_line_is_dropped_not_cut(url: str) -> None:
    assert login_instruction(f"https://{url}.example", None) is None
    assert login_instruction(None, "ABCD-EFGHI") is None
