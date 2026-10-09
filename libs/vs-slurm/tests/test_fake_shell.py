"""The in-process shell the Fake cluster uses agrees with ``bash`` on the commands production builds."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_slurm.api import SlurmConfig, SlurmConnectorTransport

# test-isolation: the interpreter is the Fake under test and is not part of the library API.
from vs_slurm.fake_shell import run

# test-isolation: the builder is the source of the production command vocabulary the Fake must match.
from vs_slurm.remote_operations import RemoteOperationError, RemoteOperations


def _bash(command: str) -> tuple[int, str, str]:
    done = subprocess.run(  # noqa: S603  # lint-waiver: LW-140005 [S603]; bash is the oracle the interpreter must match.
        ("/bin/bash", "-c", command), capture_output=True, text=True, check=False
    )
    return done.returncode, done.stdout, done.stderr


def _interpreted(command: str) -> tuple[int, str, str]:
    result = run(command)
    assert result is not None, f"not interpreted: {command}"
    return result


_Shell = Callable[[str], tuple[int, str, str]]


@dataclass
class _Reply:
    stdout: str


@dataclass
class _Transport:
    """Executes remote operations on a local directory through one shell implementation."""

    shell: _Shell
    replies: list[tuple[int, str]] = field(default_factory=list)

    def exec(self, command: str) -> _Reply:
        status, stdout, _stderr = self.shell(command)
        self.replies.append((status, stdout))
        return _Reply(stdout)

    def put(self, local: Path, remote: PurePosixPath) -> None:
        target = Path(remote)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, target)


def _tree(root: Path) -> dict[str, str | None]:
    """Every entry under ``root`` by relative path; failed publishes leave random-named files."""
    entries: dict[str, str | None] = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix().split(".pending.")[0]
        entries[name] = path.read_text(encoding="utf-8") if path.is_file() else None
    return entries


_OPERATIONS = st.lists(
    st.tuples(
        st.sampled_from(["claim", "inspect", "accepted", "rejected", "cancel"]),
        st.sampled_from(["a", "b"]),
        st.sampled_from(["x", "y z", "q'uote", "é"]),
    ),
    max_size=8,
)


def _drive(root: Path, shell: _Shell, script: list[tuple[str, str, str]]) -> list[object]:
    config = SlurmConfig(
        name="fake",
        remote_workspace_root=str(root),
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
    )
    transport = _Transport(shell)
    operations = RemoteOperations(transport, config, None)
    outcomes: list[object] = []
    for name, operation, text in script:
        try:
            if name == "claim":
                outcomes.append(operations.claim(operation, json.dumps({"intent": text})).kind)
            elif name == "inspect":
                outcomes.append(operations.inspect(operation))
            elif name == "accepted":
                outcomes.append(operations.accepted(operation, json.dumps({"job": text})))
            elif name == "rejected":
                outcomes.append(operations.rejected(operation, text))
            else:
                outcomes.append(operations.cancel(operation))
        except RemoteOperationError:
            outcomes.append("invalid evidence")
    return [*outcomes, *transport.replies]


@settings(max_examples=12, deadline=None)
@given(script=_OPERATIONS)
def test_remote_operations_leave_the_same_state_and_answers_under_either_shell(
    script: list[tuple[str, str, str]],
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        under_bash, under_interpreter = scratch / "bash", scratch / "python"
        under_bash.mkdir()
        under_interpreter.mkdir()

        expected = _drive(under_bash, _bash, script)
        actual = _drive(under_interpreter, _interpreted, script)

        assert actual == expected
        assert _tree(under_interpreter) == _tree(under_bash)


_STAGING_PROBE = (
    "if [ -f {ready} ]; then printf 'READY'; "
    "elif [ -e {object} ]; then printf 'INCOMPLETE'; "
    "else mkdir -p {payload} && printf 'MISSING'; fi"
)


@pytest.mark.parametrize("state", ["ready", "incomplete", "missing"])
def test_the_staging_cache_probe_agrees_with_bash(tmp_path: Path, state: str) -> None:
    results = []
    for shell in (_bash, _interpreted):
        root = tmp_path / shell.__name__
        (root / "obj").mkdir(parents=True)
        if state == "ready":
            (root / "obj" / "ready").write_text("")
        if state == "missing":
            (root / "obj").rmdir()
        command = _STAGING_PROBE.format(
            ready=f"'{root}/obj/ready'", object=f"'{root}/obj'", payload=f"'{root}/staged/payload'"
        )
        results.append(
            (shell(command), sorted(p.relative_to(root).as_posix() for p in root.rglob("*")))
        )
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "command",
    [
        "mkdir {r}/a {r}/a",
        "mkdir {r}/missing/child",
        "mkdir -p {r}/file/child",
        "cat {r}/file {r}/nothing",
        "rm {r}/nothing",
        "rm -f {r}/nothing",
        "rm {r}/dir",
        "mv {r}/nothing {r}/x",
        "mv {r}/file {r}/dir",
        "ln {r}/file {r}/file",
        "ln {r}/nothing {r}/x",
        "[ ! -L {r}/file ] && printf yes",
        "test -d {r}/dir; printf '%s' a b",
    ],
)
def test_failures_and_exit_statuses_agree_with_bash(tmp_path: Path, command: str) -> None:
    outcomes = []
    for shell in (_bash, _interpreted):
        root = tmp_path / shell.__name__
        (root / "dir").mkdir(parents=True)
        (root / "file").write_text("content")
        text = command.format(r=root)
        status, stdout, _stderr = shell(text)
        outcomes.append((status, stdout.replace(str(root), "R"), _tree(root)))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize(
    "command",
    [
        "for attempt in $(seq 1 30); do exit 0; done",
        "echo hi | cat",
        "rsync -a a b",
        "mkdir -p {r}/a > {r}/out",
        "mkdir {r}/*",
        "printf '%d' 3",
        "mkdir -z {r}/a",
        "if mkdir {r}/a; then printf x",
        "printf one\nprintf two",
        "(mkdir {r}/a)",
    ],
)
def test_other_commands_are_left_to_bash_without_side_effects(tmp_path: Path, command: str) -> None:
    assert run(command.format(r=tmp_path)) is None
    assert list(tmp_path.iterdir()) == []
