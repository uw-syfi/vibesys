"""Every registered read starts with only what it uses, and a catalog read starts nothing.

``vibesys host <read>`` is a new process for each read, and a desktop launch asks for about
twenty-five at once, so what a read imports is paid twenty-five times on a busy machine. A
catalog read does a few milliseconds of work, which makes the import of what it never uses the
whole cost. The probe below runs each read through the console launcher in a fresh interpreter
(the import side effects a pytest session has already used up) and reports the modules loaded and
the processes started, never a time, so the verdict is the same on a busy or an idle machine.

The budgets are what each read loads today with a little room for the interpreter's own version.
A read that needs more has a reason to name in the PR; a budget is not raised to make a check pass.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command
from tests.support.task_project import write_task

from vs_project.api import Project
from vs_proto.api import (
    Oneof,
    ReadRequest,
    ReadResponse,
    Timestamp,
    WatchRequest,
    WatchResponse,
    host_operations,
    validate,
)
from vs_proto.api import host_payloads as pb

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_PROBE = """
import json
import sys

spawned = []
SPAWNS = {
    "subprocess.Popen", "os.posix_spawn", "os.spawn", "os.system", "os.fork", "os.forkpty",
    "os.exec",
}

def audit(event, arguments):
    if event in SPAWNS:
        spawned.append(event)

sys.addaudithook(audit)
command, request = sys.argv[1], sys.argv[2]
sys.stdin = open(request)
from entrypoints.launcher import main

status = main(["host", command])
print(json.dumps({"status": status, "modules": sorted(sys.modules), "spawned": spawned}),
      file=sys.stderr)
"""
"""Run one read as the console launcher does and report, on stderr, what it loaded and started.

The audit hook is an observer the interpreter offers for this, so no code is replaced. Reads the
request from the file named by the second argument and writes the reply to stdout as the
gateway receives it."""

_TASK = "default"
_RUN = "20261001-000000-00000000-proj-20261001-000000"
_WINDOW_END = datetime(2026, 10, 30, tzinfo=UTC)

# Modules no read has a reason to load: the run engine and its core.
_ENGINE = ("vs_runtime", "vs_core")
# The agent library and what it brings. Only a read about harnesses, logins or the whole host
# (describe-machine, preflight-run) loads it.
_AGENT_LIBRARY = ("vs_agent", "agentshim", "vs_prompts", "vs_sandbox", "jinja2")
# The helper channel's whole host: every collaborator of a run start, built for one read.
_WHOLE_HOST = "entrypoints.host_helper"


@dataclass(frozen=True)
class _Read:
    """One registered read: its command, the request that exercises its answer, its limits."""

    command: str
    build: Callable[[Path], ReadRequest | WatchRequest]
    reply: str
    """The reply slot a successful answer fills (``failure`` where the request is refused). A
    stream's is the slot of its last frame."""
    modules: int
    """The most modules the command may load, counted in a fresh interpreter."""
    agent_library: bool = False
    """Whether the read is about the agent library or the whole host, so loads it."""
    whole_host: bool = False
    """Whether the read needs the helper's whole host."""
    catalog: bool = False
    """Whether the read is a catalog the app asks for at every launch: it answers from files."""
    stream: bool = False
    """Whether the command answers with framed responses, not one envelope."""


def _request(slot: str, message: object) -> ReadRequest:
    request = ReadRequest(checkout="/src/checkout", method=Oneof(slot, message))
    validate(request)
    return request


def _watch(project: Path) -> WatchRequest:
    request = WatchRequest(
        checkout="/src/checkout",
        method=Oneof(
            "stream_saved_runs", pb.StreamSavedRunsRequest(request_id="r", project=str(project))
        ),
    )
    validate(request)
    return request


def _window(project: Path) -> pb.UsageWindowRequest:
    return pb.UsageWindowRequest(
        request_id="r",
        project=str(project),
        run_ids=[_RUN],
        since=Timestamp.from_datetime(datetime(2026, 10, 29, tzinfo=UTC)),
        until=Timestamp.from_datetime(_WINDOW_END),
    )


def _unvetted_preflight(project: Path) -> pb.PreflightRunRequest:
    """A start the host refuses before it launches the engine: its harness is not vetted."""
    return pb.PreflightRunRequest(
        request_id="r",
        project=str(project),
        task=_TASK,
        harness=pb.HarnessChoice(provider="not-a-provider", model="not-a-model"),
    )


READS: tuple[_Read, ...] = (
    _Read(
        "inspect-candidate",
        lambda project: _request(
            "inspect_candidate",
            pb.InspectCandidateRequest(request_id="r", path=str(project), selection_revision=1),
        ),
        "inspect_candidate",
        modules=520,
        catalog=True,
    ),
    _Read(
        "list-tasks",
        lambda project: _request(
            "list_tasks", pb.ListTasksRequest(request_id="r", project=str(project))
        ),
        "list_tasks",
        modules=540,
        catalog=True,
    ),
    _Read(
        "get-task-goal",
        lambda project: _request(
            "get_task_goal",
            pb.GetTaskGoalRequest(request_id="r", project=str(project), task=_TASK),
        ),
        "get_task_goal",
        modules=540,
        catalog=True,
    ),
    _Read(
        "list-harnesses",
        lambda _project: _request("list_harnesses", pb.ListHarnessesRequest(request_id="r")),
        "list_harnesses",
        modules=740,
        catalog=True,
        agent_library=True,
    ),
    _Read(
        "list-examples",
        lambda _project: _request("list_examples", pb.ListExamplesRequest(request_id="r")),
        "list_examples",
        modules=540,
        catalog=True,
    ),
    _Read(
        "list-compute",
        lambda _project: _request("list_compute", pb.ListComputeRequest(request_id="r")),
        "list_compute",
        modules=480,
        catalog=True,
    ),
    _Read(
        "inspect-destination",
        lambda project: _request(
            "inspect_destination",
            pb.InspectDestinationRequest(
                request_id="r", example="kvstore-default", destination=str(project / "copy")
            ),
        ),
        "inspect_destination",
        modules=560,
        catalog=True,
    ),
    _Read(
        "stream-saved-runs",
        _watch,
        "stream_saved_runs",
        modules=580,
        stream=True,
    ),
    _Read(
        "list-run-digests",
        lambda project: _request(
            "list_run_digests",
            pb.ListRunDigestsRequest(request_id="r", project=str(project), run_ids=[_RUN]),
        ),
        "list_run_digests",
        modules=580,
    ),
    _Read(
        "usage-window",
        lambda project: _request("usage_window", _window(project)),
        "usage_window",
        modules=590,
    ),
    _Read(
        "list-agent-logins",
        lambda _project: _request("list_agent_logins", pb.ListAgentLoginsRequest(request_id="r")),
        "list_agent_logins",
        modules=740,
        agent_library=True,
    ),
    _Read(
        "describe-machine",
        lambda _project: _request("describe_machine", pb.DescribeMachineRequest(request_id="r")),
        "describe_machine",
        modules=760,
        agent_library=True,
        whole_host=True,
    ),
    _Read(
        "preflight-run",
        lambda project: _request("preflight_run", _unvetted_preflight(project)),
        "failure",
        modules=760,
        agent_library=True,
        whole_host=True,
    ),
)


@dataclass(frozen=True)
class _Probe:
    """What one fresh interpreter reported for one read."""

    reply: str
    """The slot the reply (a stream's last frame) fills."""
    modules: frozenset[str]
    spawned: tuple[str, ...]


@pytest.fixture(scope="module")
def host_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A project, an empty executable search path and the host's private directories."""
    root = tmp_path_factory.mktemp("host-read")
    (root / "bin").mkdir()
    (root / "run").mkdir(mode=0o700)
    project = root / "project"
    project.mkdir()
    Project.open(project).state.create_project("example", now=datetime(2026, 1, 1, tzinfo=UTC))
    write_task(project, "", task=_TASK)
    return root


def _probe(read: _Read, root: Path) -> _Probe:
    """Run ``read`` in a fresh interpreter whose host holds nothing but ``root``."""
    request = root / f"{read.command}.bin"
    request.write_bytes(read.build(root / "project").to_binary())
    environment = {
        # No program is found by name: the machine's Docker, GPUs and agent CLIs are not
        # this test's subject, and a read's answer must not depend on them.
        "PATH": str(root / "bin"),
        "HOME": str(root),
        "XDG_RUNTIME_DIR": str(root / "run"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "VIBESYS_STATE_HOME": str(root / "state"),
    }
    completed = run_test_command(
        [sys.executable, "-I", "-c", _PROBE, read.command, str(request)],
        capture_output=True,
        env=environment,
        cwd=root,
        check=True,
    )
    report = json.loads(completed.stderr.decode().strip().splitlines()[-1])
    assert report["status"] == 0
    return _Probe(
        _reply_slot(read, completed.stdout), frozenset(report["modules"]), tuple(report["spawned"])
    )


def _reply_slot(read: _Read, written: bytes) -> str:
    """The slot the reply fills: a read's one envelope, or the last frame of a stream."""
    if not read.stream:
        reply = ReadResponse.from_binary(written)
        validate(reply)
        assert reply.result is not None
        return reply.result.field
    frame = b""
    while written:
        length = int.from_bytes(written[:4], "big")
        frame, written = written[4 : 4 + length], written[4 + length :]
    last = WatchResponse.from_binary(frame)
    validate(last)
    assert last.result is not None
    return last.result.field


def _loaded(probe: _Probe, roots: tuple[str, ...]) -> list[str]:
    return sorted(
        name
        for name in probe.modules
        if any(name == root or name.startswith(f"{root}.") for root in roots)
    )


@pytest.mark.parametrize("read", READS, ids=lambda read: read.command)
def test_a_read_loads_only_the_modules_it_uses(read: _Read, host_directory: Path) -> None:
    """Fails on a read that imports the run engine, the agent library or the whole host it
    does not touch, or whose closure grows past its budget."""
    probe = _probe(read, host_directory)

    assert probe.reply == read.reply
    assert _loaded(probe, _ENGINE) == []
    if not read.agent_library:
        assert _loaded(probe, _AGENT_LIBRARY) == []
    if not read.whole_host:
        assert _WHOLE_HOST not in probe.modules
    assert len(probe.modules) <= read.modules


@pytest.mark.parametrize(
    "read", [read for read in READS if read.catalog], ids=lambda read: read.command
)
def test_a_catalog_read_starts_no_process(read: _Read, host_directory: Path) -> None:
    """The catalog reads, which the app asks for at every launch, answer from files and the
    interpreter alone. A shell probe of the user's login environment is the failure this
    catches: it is useless to a catalog and costs hundreds of milliseconds."""
    assert _probe(read, host_directory).spawned == ()


def test_every_registered_read_has_a_probe() -> None:
    registered = {operation.cli for operation in host_operations.READ_CALLS}
    watches = {slot.replace("_", "-") for slot in host_operations.WATCHES} - {"login"}
    assert registered | watches | {"inspect-candidate"} == {read.command for read in READS}
