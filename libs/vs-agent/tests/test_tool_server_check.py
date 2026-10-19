"""A tool server's startup check runs in the agent's container exactly as the server will.

A Fake command runner stands for the container: it records the command it was given and answers
as scripted, so what a check runs, and how its outcome becomes a failure, is observed directly.
"""

from __future__ import annotations

import os
import shlex

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_agent.api import (
    CHECK_TIMEOUT_S,
    CONTAINER_PYTHON,
    check_tool_servers,
    container_import_roots,
)
from vs_mcp.api import StdioServerDescriptor
from vs_sandbox.api import CommandResult
from vs_sandbox.api.testing import FakeCommandRunner
from vs_sim.api.testing import SimThreads, examples

_names = st.text("abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8)
_env = st.dictionaries(_names.map(str.upper), _names, max_size=3).filter(
    lambda env: "PYTHONPATH" not in env
)
_DETAIL_LIMIT = 1000


def _runner(result: CommandResult | None = None) -> FakeCommandRunner:
    runner = FakeCommandRunner(threads=SimThreads(schedule_seed=0))
    if result is not None:
        runner.default_result = result
    return runner


@settings(max_examples=examples(pr=15, nightly=200), deadline=None)
@given(name=_names, args=st.lists(_names, max_size=3), env=_env, launch_env=_env)
def test_a_check_runs_on_the_container_interpreter_and_import_roots_with_its_environment(
    name: str, args: list[str], env: dict[str, str], launch_env: dict[str, str]
) -> None:
    launch_env = {key: value for key, value in launch_env.items() if key not in env}
    check = StdioServerDescriptor(
        name, "python", tuple(args), tuple(env.items()), tuple(launch_env.items())
    )
    runner = _runner()

    failures = check_tool_servers(runner, [check], agent_path=str)

    assert failures == ()
    [call] = runner.calls
    words = shlex.split(call.command)
    assert words[0] == "env"
    program = words.index(CONTAINER_PYTHON)
    assert words[program:] == [CONTAINER_PYTHON, *args]
    assert dict(word.split("=", 1) for word in words[1:program]) == {
        **env,
        **launch_env,
        "PYTHONPATH": os.pathsep.join(container_import_roots()),
    }


@settings(max_examples=examples(pr=15, nightly=200), deadline=None)
@given(
    code=st.one_of(st.integers(min_value=1, max_value=255), st.none()),
    output=st.text(max_size=3000),
)
def test_a_check_that_does_not_exit_zero_is_a_failure_naming_the_server_with_bounded_output(
    code: int | None, output: str
) -> None:
    check = StdioServerDescriptor("vibesys-run", "python", ("-m", "server", "--check"))
    runner = _runner(CommandResult(output=output, exit_code=code))

    [failure] = check_tool_servers(runner, [check], agent_path=str)

    assert failure.server == "vibesys-run"
    assert failure.detail
    assert len(failure.detail) <= max(_DETAIL_LIMIT, len(f"exited with code {code}"))
    if output.strip():
        assert failure.detail == output.strip()[-_DETAIL_LIMIT:]


def test_a_cancelled_check_is_a_failure_even_with_a_zero_exit() -> None:
    check = StdioServerDescriptor("vibesys-run", "python", ("-m", "server"))
    runner = _runner(CommandResult(output="", exit_code=0, cancelled=True))

    failures = check_tool_servers(runner, [check], agent_path=str)

    assert [failure.server for failure in failures] == ["vibesys-run"]


def test_each_check_runs_and_only_the_failing_ones_are_reported() -> None:
    passing = StdioServerDescriptor("passing", "python", ("-m", "fine"))
    broken = StdioServerDescriptor("broken", "python", ("-m", "broken"))
    runner = _runner()
    runner.script_process(
        shlex.join(
            [
                "env",
                f"PYTHONPATH={os.pathsep.join(container_import_roots())}",
                CONTAINER_PYTHON,
                "-m",
                "broken",
            ]
        ),
        stderr="No module named 'broken'",
        returncode=1,
    )

    failures = check_tool_servers(runner, [passing, broken, passing], agent_path=str)

    assert [(failure.server, failure.detail) for failure in failures] == [
        ("broken", "No module named 'broken'")
    ]
    assert len(runner.calls) == 3


def test_a_check_is_bounded_in_time() -> None:
    runner = _runner()

    check_tool_servers(runner, [StdioServerDescriptor("s", "python", ())], agent_path=str)

    assert runner.calls[0].timeout == CHECK_TIMEOUT_S
