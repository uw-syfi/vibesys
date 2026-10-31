"""Every registered read starts with only what it uses, and a catalog read starts nothing.

A real-process test, so it lives with ``test_light_host_commands.py``: the deterministic tiers
use no real processes.

``vibesys host <read>`` is a new process for each read, and a desktop launch asks for about
twenty-five at once, so what a read imports is paid twenty-five times on a busy machine. A
catalog read does a few milliseconds of work, which makes the import of what it never uses the
whole cost. The probe below runs each read through the console launcher in a fresh interpreter
(the import side effects a pytest session has already used up) and reports the modules loaded and
the processes started, never a time, so the verdict is the same on a busy or an idle machine.

What a read may load is stated by name, in groups of modules (``Group``): each read lists the
groups it uses and every other group is forbidden to it, so a read that starts importing the
run-listing stream, the helper's OS adapters or the admission modules fails with the module's name,
whatever else it loads. The module count of each read stays as a backstop for what no group names;
it is loose on purpose, because the count moves with the interpreter and the lock file. A read that
needs a group it does not list has a reason to name in the PR; a list is not widened to make a
check pass, and a group a read no longer loads is removed from its list.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command
from tests.support.light_imports import under
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
from vs_sim.api.testing import HANG_GUARD_S

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

_OPERATIONS = "vibesys.api.host_operations"


class Group(StrEnum):
    """A named set of modules that only some reads have a reason to load."""

    ENGINE = "the run engine and the facades that load it"
    ADMISSION = "the admission, login and start modules of the host operations"
    CANDIDATE = "candidate inspection"
    READ_ADAPTER = "the registered-read adapter and the host facts it builds"
    RUN_STREAM = "the run-listing stream"
    RUN_READS = "the readers of a run's digest and usage"
    USAGE = "the usage window's fold"
    PROJECT_HOST = "the helper's project adapters and registry"
    WHOLE_HOST = "the helper's whole host"
    EXAMPLES = "the example catalog and project maker"
    COMPUTE = "the compute settings' adapters"
    AGENT_LIBRARY = "the agent library and what it brings"
    LOGINS = "the agent logins' models"
    HOST_MODELS = "the helper's pydantic request and example models"


GROUPS: dict[Group, tuple[str, ...]] = {
    Group.ENGINE: (
        "vs_runtime",
        "vs_core",
        "vibesys.run",
        "vibesys.orchestration",
        "vibesys.api.runs",
        "vibesys.api.session",
        "vibesys.api.host",
        "vibesys.api.transport",
    ),
    Group.ADMISSION: (
        f"{_OPERATIONS}.api",
        f"{_OPERATIONS}._start",
        f"{_OPERATIONS}._login",
        f"{_OPERATIONS}._project",
        f"{_OPERATIONS}._compute",
    ),
    Group.CANDIDATE: (f"{_OPERATIONS}.candidate", f"{_OPERATIONS}._candidate"),
    Group.READ_ADAPTER: (
        "entrypoints.host_reads",
        "entrypoints.host_facts",
        "server.host_operation_reads",
    ),
    Group.RUN_STREAM: (f"{_OPERATIONS}.saved_runs", "server.host_saved_runs"),
    Group.RUN_READS: ("server.host_run_reads", "server.host_run_wire"),
    Group.USAGE: ("server.usage_window", "server.run_usage", "server.run_summary"),
    Group.PROJECT_HOST: ("server.host_helper_os", "server.host_helper_requests", "server.registry"),
    Group.WHOLE_HOST: (
        "entrypoints.host_helper",
        "server.host_helper",
        "server.host_helper_readers",
        "server.instances",
    ),
    Group.EXAMPLES: (
        "entrypoints.examples",
        "entrypoints.example_catalog",
        "entrypoints.host_example_projects",
    ),
    Group.COMPUTE: ("server.host_compute_runtime",),
    Group.AGENT_LIBRARY: ("vs_agent", "agentshim", "vs_prompts", "vs_sandbox", "jinja2"),
    Group.LOGINS: ("server.host_agent_logins",),
    Group.HOST_MODELS: ("server.host_api", "server.host_examples", "server.host_run_summary"),
}
"""Each group's modules, by name; a package name stands for everything under it."""

_ALWAYS_FORBIDDEN = frozenset({Group.ENGINE, Group.ADMISSION})
"""No read has a reason to load these: reads go through ``reads``, ``saved_runs`` or ``candidate``,
never through the facade of the admitted workflows, and never through the engine."""

_PROJECT_READ = frozenset({Group.READ_ADAPTER, Group.PROJECT_HOST, Group.HOST_MODELS, Group.LOGINS})
"""What a read of a project's files loads: the helper's project adapters and, with them, the
models and login facts they import (the floor named in the PR, not a use)."""
_WHOLE_HOST_READ = frozenset(
    {
        Group.READ_ADAPTER,
        Group.PROJECT_HOST,
        Group.WHOLE_HOST,
        Group.COMPUTE,
        Group.AGENT_LIBRARY,
        Group.LOGINS,
        Group.HOST_MODELS,
    }
)


@dataclass(frozen=True)
class _Read:
    """One registered read: its command, the request that exercises its answer, its limits."""

    command: str
    build: Callable[[Path], ReadRequest | WatchRequest]
    reply: str
    """The reply slot a successful answer fills (``failure`` where the request is refused). A
    stream's is the slot of its last frame."""
    modules: int
    """The most modules the command may load, counted in a fresh interpreter: a backstop."""
    allowed: frozenset[Group] = frozenset()
    """The groups the read has a reason to load; every other group is forbidden to it."""
    catalog: bool = False
    """Whether the read is a catalog the app asks for at every launch: it answers from files."""
    stream: bool = False
    """Whether the command answers with framed responses, not one envelope."""


_CHECKOUT = "/src/checkout"


def _watch(project: Path) -> WatchRequest:
    return WatchRequest(
        checkout=_CHECKOUT,
        method=Oneof(
            "stream_saved_runs", pb.StreamSavedRunsRequest(request_id="r", project=str(project))
        ),
    )


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
        lambda project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof(
                "inspect_candidate",
                pb.InspectCandidateRequest(request_id="r", path=str(project), selection_revision=1),
            ),
        ),
        "inspect_candidate",
        modules=520,
        catalog=True,
        allowed=frozenset({Group.CANDIDATE}),
    ),
    _Read(
        "list-tasks",
        lambda project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof("list_tasks", pb.ListTasksRequest(request_id="r", project=str(project))),
        ),
        "list_tasks",
        modules=540,
        catalog=True,
        allowed=_PROJECT_READ,
    ),
    _Read(
        "get-task-goal",
        lambda project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof(
                "get_task_goal",
                pb.GetTaskGoalRequest(request_id="r", project=str(project), task=_TASK),
            ),
        ),
        "get_task_goal",
        modules=540,
        catalog=True,
        allowed=_PROJECT_READ,
    ),
    _Read(
        "list-harnesses",
        lambda _project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof("list_harnesses", pb.ListHarnessesRequest(request_id="r")),
        ),
        "list_harnesses",
        modules=740,
        catalog=True,
        allowed=frozenset(
            {Group.READ_ADAPTER, Group.AGENT_LIBRARY, Group.HOST_MODELS, Group.LOGINS}
        ),
    ),
    _Read(
        "list-examples",
        lambda _project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof("list_examples", pb.ListExamplesRequest(request_id="r")),
        ),
        "list_examples",
        modules=540,
        catalog=True,
        allowed=frozenset({Group.READ_ADAPTER, Group.EXAMPLES, Group.HOST_MODELS, Group.LOGINS}),
    ),
    _Read(
        "list-compute",
        lambda _project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof("list_compute", pb.ListComputeRequest(request_id="r")),
        ),
        "list_compute",
        modules=480,
        catalog=True,
        allowed=frozenset({Group.READ_ADAPTER, Group.COMPUTE}),
    ),
    _Read(
        "inspect-destination",
        lambda project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof(
                "inspect_destination",
                pb.InspectDestinationRequest(
                    request_id="r", example="kvstore-default", destination=str(project / "copy")
                ),
            ),
        ),
        "inspect_destination",
        modules=560,
        catalog=True,
        allowed=_PROJECT_READ | {Group.EXAMPLES},
    ),
    _Read(
        "stream-saved-runs",
        _watch,
        "stream_saved_runs",
        modules=580,
        stream=True,
        allowed=_PROJECT_READ | {Group.RUN_STREAM},
    ),
    _Read(
        "list-run-digests",
        lambda project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof(
                "list_run_digests",
                pb.ListRunDigestsRequest(request_id="r", project=str(project), run_ids=[_RUN]),
            ),
        ),
        "list_run_digests",
        modules=580,
        allowed=_PROJECT_READ | {Group.RUN_READS},
    ),
    _Read(
        "usage-window",
        lambda project: ReadRequest(
            checkout=_CHECKOUT, method=Oneof("usage_window", _window(project))
        ),
        "usage_window",
        modules=590,
        allowed=_PROJECT_READ | {Group.RUN_READS, Group.USAGE},
    ),
    _Read(
        "list-agent-logins",
        lambda _project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof("list_agent_logins", pb.ListAgentLoginsRequest(request_id="r")),
        ),
        "list_agent_logins",
        modules=740,
        allowed=frozenset({Group.READ_ADAPTER, Group.AGENT_LIBRARY, Group.LOGINS}),
    ),
    _Read(
        "describe-machine",
        lambda _project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof("describe_machine", pb.DescribeMachineRequest(request_id="r")),
        ),
        "describe_machine",
        modules=760,
        allowed=_WHOLE_HOST_READ,
    ),
    _Read(
        "preflight-run",
        lambda project: ReadRequest(
            checkout=_CHECKOUT,
            method=Oneof("preflight_run", _unvetted_preflight(project)),
        ),
        "failure",
        modules=760,
        allowed=_WHOLE_HOST_READ,
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


@pytest.fixture(scope="module")
def probed(host_directory: Path) -> Callable[[_Read], _Probe]:
    """Each read's probe, run once: the checks of one read share its fresh interpreter."""
    results: dict[str, _Probe] = {}

    def probe(read: _Read) -> _Probe:
        if read.command not in results:
            results[read.command] = _probe(read, host_directory)
        return results[read.command]

    return probe


def _probe(read: _Read, root: Path) -> _Probe:
    """Run ``read`` in a fresh interpreter whose host holds nothing but ``root``."""
    request = root / f"{read.command}.bin"
    message = read.build(root / "project")
    validate(message)
    request.write_bytes(message.to_binary())
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
        timeout=HANG_GUARD_S,
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
    return sorted(name for name in probe.modules if under(name, roots))


@pytest.mark.parametrize("read", READS, ids=lambda read: read.command)
def test_a_read_loads_only_the_modules_it_uses(
    read: _Read, probed: Callable[[_Read], _Probe]
) -> None:
    """Fails on a read that loads a module of a group it does not list, that grows past its
    backstop count, or, for a catalog, that starts a process.

    The catalogs the app asks for at every launch answer from files and the interpreter alone. A
    shell probe of the user's login environment is the failure the last check catches: it is
    useless to a catalog and costs hundreds of milliseconds."""
    probe = probed(read)

    assert probe.reply == read.reply
    forbidden = {
        group.name: modules
        for group in Group
        if group in _ALWAYS_FORBIDDEN or group not in read.allowed
        if (modules := _loaded(probe, GROUPS[group]))
    }
    assert forbidden == {}, (
        f"{read.command} loaded modules it has no reason to: {forbidden}. Import them where the "
        "read that needs them uses them (a lazy module, or a module of its own), not at load."
    )
    assert len(probe.modules) <= read.modules
    if read.catalog:
        assert probe.spawned == ()


@pytest.mark.parametrize("read", READS, ids=lambda read: read.command)
def test_a_read_lists_only_the_groups_it_loads(
    read: _Read, probed: Callable[[_Read], _Probe]
) -> None:
    """A group the read lists and does not load is room for a regression: remove it from the list.

    The list is a ban on every other group, so it is only as strong as it is tight."""
    probe = probed(read)

    unused = sorted(group.name for group in read.allowed if not _loaded(probe, GROUPS[group]))
    assert unused == [], f"{read.command} no longer loads {unused}: remove them from `allowed`"
    assert not read.allowed & _ALWAYS_FORBIDDEN


def test_every_module_a_group_names_exists() -> None:
    """A group naming a module that was renamed bans nothing: say which one."""
    missing = sorted(
        module
        for modules in GROUPS.values()
        for module in modules
        if importlib.util.find_spec(module) is None
    )
    assert missing == [], f"groups name modules that do not exist (renamed?): {missing}"


def test_every_registered_read_has_a_probe() -> None:
    registered = {operation.cli for operation in host_operations.READ_CALLS}
    watches = {slot.replace("_", "-") for slot in host_operations.WATCHES} - {"login"}
    assert registered | watches | {"inspect-candidate"} == {read.command for read in READS}
