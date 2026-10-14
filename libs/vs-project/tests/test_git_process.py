"""Git started through ``Git``: its environment policy, how it ends, and no background maintenance.

Git runs auto maintenance after a commit and, from Git 2.47, detaches it. A
detached process outlives the command and keeps writing under ``.git`` while the
caller removes or moves the repository ("Directory not empty: .git").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.real_git import REAL_GIT

from vs_project.api import Git, GitCommandError
from vs_sim.api import ProcessTimeoutError
from vs_sim.api.testing import FakeProcesses, ProcessScript

_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@local")


def _started_children(trace: Path) -> list[list[str]]:
    """Return the argv of every child process Git started, from its trace2 log."""
    if not trace.exists():
        return []
    events = (json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines())
    return [event["argv"] for event in events if event.get("event") == "child_start"]


@settings(max_examples=3, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(commits=st.integers(min_value=1, max_value=4))
def test_commits_through_run_git_start_no_maintenance(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    commits: int,
) -> None:
    repo = tmp_path_factory.mktemp("repo")
    trace = tmp_path_factory.mktemp("trace") / "git-trace2.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    # Make Git eager to maintain, so only the VibeSys config can stop it.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "gc.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "maintenance.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")

    assert REAL_GIT.run(["init", "-q"], cwd=repo).returncode == 0
    for value in range(commits):
        (repo / "file.txt").write_text(f"{value}\n", encoding="utf-8")
        REAL_GIT.run(["add", "."], cwd=repo)
        committed = REAL_GIT.run([*_IDENTITY, "commit", "-q", "-m", f"c{value}"], cwd=repo)
        assert committed.returncode == 0

    children = _started_children(trace)
    assert [argv for argv in children if {"maintenance", "gc"} & set(argv)] == []


_ARGS = st.lists(st.text(alphabet="abcdefgh-=./", min_size=1, max_size=6), min_size=1, max_size=4)
_OUTPUT = st.binary(max_size=16)


@given(args=_ARGS, returncode=st.integers(-9, 3), stdout=_OUTPUT, stderr=_OUTPUT)
def test_a_command_that_ends_reports_its_exit_and_output(
    args: list[str], returncode: int, stdout: bytes, stderr: bytes
) -> None:
    script = ProcessScript(returncode=returncode, stdout=stdout, stderr=stderr)
    processes = FakeProcesses(lambda _spec: script)
    git = Git(processes)

    outcome = git.run(args, cwd=Path("/repo"))
    text = git.run_text(args, cwd=Path("/repo"))

    assert (outcome.returncode, outcome.stdout, outcome.stderr) == (returncode, stdout, stderr)
    assert (text.returncode, text.stdout, text.stderr) == (
        returncode,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )
    spec = processes.ran[0]
    assert spec.argv[0].endswith("git")
    assert spec.argv[1:] == tuple(args)
    assert spec.cwd == Path("/repo")
    env = dict(spec.env or {})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    keys = {env[f"GIT_CONFIG_KEY_{index}"] for index in range(int(env["GIT_CONFIG_COUNT"]))}
    assert {"maintenance.auto", "gc.auto"} <= keys
    if returncode == 0:
        assert text.check() is text
    else:
        with pytest.raises(GitCommandError) as failure:
            text.check()
        assert failure.value.command == ("git", *args)
        assert failure.value.returncode == returncode


@given(args=_ARGS, survives_kill=st.booleans(), timeout=st.floats(0.1, 30.0))
def test_a_command_past_its_timeout_raises_and_says_what_is_left(
    *, args: list[str], survives_kill: bool, timeout: float
) -> None:
    script = ProcessScript(runs_until_signalled=True, survives_kill=survives_kill)
    git = Git(FakeProcesses(lambda _spec: script))

    with pytest.raises(ProcessTimeoutError) as raised:
        git.run(args, cwd=Path("/repo"), timeout=timeout)

    assert raised.value.timeout == timeout
    notes = getattr(raised.value, "__notes__", [])
    assert bool(notes) == survives_kill
