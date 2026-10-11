"""The host helper: its pure channel core, its requests, and its shell on simulated I/O.

The core (:func:`step`) is checked over generated sequences of lines, answers,
observations and ticks, driven the way the shell drives it. The requests run through
:func:`answer` over Fakes. The shell (:func:`serve_channel`) runs on ``SimThreads`` and
``SimNetwork``, so deadlines, watches and the relay are virtual time. The real helper
process is exercised in ``tests/e2e/test_host_helper.py``.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.host_helper_support import (
    HOME,
    VIBESYS_VERSION,
    ByteBuffer,
    ChannelClient,
    FakeDirectories,
    FakeRunLauncher,
    fake_host,
    record,
    serve_on,
)

from server.host_api import (
    HELPER_PROTOCOL_VERSIONS,
    PATHS_COMPLETE_CAP,
    HelloRequest,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    HelperRequest,
    InstanceList,
    InstancesChanged,
    LiveInstanceRecord,
    PathsCompleteReply,
    PathsCompleteRequest,
    PingReply,
    ResumeLatest,
    ResumeRun,
    RunConnectReply,
    RunPhase,
    RunStartReply,
    RunStartRequest,
    RunStatus,
    parse_helper_request,
)
from server.host_helper import (
    Answered,
    ChannelOutput,
    ChannelState,
    Close,
    Execute,
    InputEnded,
    Observed,
    PathHints,
    ProjectRun,
    Received,
    Relay,
    Release,
    ScannedEntry,
    Send,
    Tick,
    Unwatch,
    Watch,
    accepts_input,
    answer,
    launch_arguments,
    negotiate,
    run_summaries,
    select_completions,
    split_prefix,
    step,
)
from server.registry import FakeInstanceStore
from server.stdio_bridge import EXIT_STATUS, BridgeOutcome
from server.stdio_bridge_report import BridgeReport
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sim.api import Listener

ALL_FEATURES = tuple(feature.value for feature in HelperFeature)


def _line(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode()


def _hello(request_id: str = "h", **overrides: object) -> dict[str, Any]:
    return {
        "type": "hello",
        "request_id": request_id,
        "deadline_ms": 5000,
        "client_versions": [1, 1],
        "features": list(ALL_FEATURES),
        "app_version": "test",
        **overrides,
    }


def _request(
    kind: str, request_id: str, deadline_ms: int = 5000, **fields: object
) -> dict[str, Any]:
    return {"type": kind, "request_id": request_id, "deadline_ms": deadline_ms, **fields}


# ----- negotiation -----


@given(st.integers(1, 5), st.integers(1, 5), st.lists(st.text(max_size=20), max_size=4))
def test_hello_picks_the_highest_shared_version_and_the_known_features(
    low: int, high: int, extra: list[str]
) -> None:
    request = HelloRequest(
        request_id="h",
        deadline_ms=1,
        client_versions=(low, high),
        features=(*extra, *ALL_FEATURES),
        app_version="a",
    )
    helper_low, helper_high = HELPER_PROTOCOL_VERSIONS
    outcome = negotiate(request, VIBESYS_VERSION)
    if low <= high and max(low, helper_low) <= min(high, helper_high):
        assert not isinstance(outcome, HelperError)
        assert outcome.version == min(high, helper_high)
        assert outcome.features == frozenset(HelperFeature)
    else:
        assert isinstance(outcome, HelperError)
        assert outcome.code is HelperErrorCode.INCOMPATIBLE
        assert outcome.supported_versions == HELPER_PROTOCOL_VERSIONS
        assert outcome.vibesys_version == VIBESYS_VERSION


# ----- the channel core, over generated sequences -----

_IDS = st.sampled_from(["a", "b", "c", "d"])
_KINDS = st.sampled_from(
    ["ping", "instances.list", "instances.watch", "instances.unwatch", "run.connect", "hello"]
)


@st.composite
def _client_line(draw: st.DrawFn) -> bytes:
    kind = draw(st.sampled_from(["request", "request", "request", "garbage", "hello"]))
    request_id = draw(_IDS)
    if kind == "garbage":
        return draw(st.sampled_from([b"not json", b"{}", _line({"request_id": request_id})]))
    if kind == "hello":
        versions = draw(st.sampled_from([[1, 1], [2, 3], [1, 9]]))
        return _line(_hello(request_id, client_versions=versions))
    method = draw(_KINDS)
    fields: dict[str, Any] = {}
    if method == "instances.unwatch":
        fields["watch_id"] = draw(_IDS)
    if method == "run.connect":
        fields.update(instance_id="0123456789ab", role="control")
    if method == "hello":
        return _line(_hello(request_id))
    return _line(_request(method, request_id, draw(st.integers(1, 3000)), **fields))


@dataclass
class _Driver:
    """Drives the core as the shell does, and records what it sent."""

    state: ChannelState = field(default_factory=ChannelState)
    now: float = 0.0
    executing: list[Execute] = field(default_factory=list)
    sent: list[Send] = field(default_factory=list)
    outputs: list[ChannelOutput] = field(default_factory=list)
    lines: int = 0

    def feed(self, event: Received | Answered | Observed | Tick | InputEnded) -> None:
        self.state, outputs = step(self.state, event, VIBESYS_VERSION)
        self.outputs.extend(outputs)
        for output in outputs:
            if isinstance(output, Send):
                self.sent.append(output)
            elif isinstance(output, Execute):
                self.executing.append(output)


@st.composite
def _actions(draw: st.DrawFn) -> list[tuple[str, Any]]:
    actions: list[tuple[str, Any]] = [("line", _line(_hello("h0")))]
    for _ in range(draw(st.integers(0, 25))):
        kind = draw(st.sampled_from(["line", "line", "answer", "tick", "observe", "end"]))
        if kind == "line":
            actions.append(("line", draw(_client_line())))
        elif kind == "answer":
            actions.append(("answer", (draw(st.integers(0, 10)), draw(st.booleans()))))
        elif kind == "tick":
            actions.append(("tick", draw(st.floats(0, 2))))
        elif kind == "observe":
            actions.append(("observe", (draw(_IDS), draw(st.booleans()))))
        else:
            actions.append(("end", None))
    return actions


_LISTINGS = (InstanceList(instances=()), InstanceList(instances=(record("0123456789ab"),)))


def _reply_for(execute: Execute, *, success: bool) -> RunConnectReply | PingReply | HelperError:
    request = execute.request
    if not success:
        return HelperError(
            request_id=request.request_id, code=HelperErrorCode.INTERNAL, message="x"
        )
    if request.type == "run.connect":
        return RunConnectReply(request_id=request.request_id, run_id="r", protocol_version=1)
    return PingReply(request_id=request.request_id)


def _drive(actions: Sequence[tuple[str, Any]]) -> _Driver:
    driver = _Driver()
    for kind, value in actions:
        if driver.state.relaying:
            break
        if kind == "line" and accepts_input(driver.state):
            driver.lines += 1
            driver.feed(Received(value, driver.now))
        elif kind == "answer" and driver.executing:
            index, success = value
            driver.feed(
                Answered(
                    driver.executing[index % len(driver.executing)].request.request_id,
                    _reply_for(
                        driver.executing.pop(index % len(driver.executing)), success=success
                    ),
                )
            )
        elif kind == "tick":
            driver.now += value
            driver.feed(Tick(driver.now))
        elif kind == "observe":
            watch_id, which = value
            driver.feed(Observed(watch_id, _LISTINGS[which]))
        elif kind == "end" and accepts_input(driver.state):
            driver.feed(InputEnded())
    if not driver.state.relaying:
        driver.feed(Tick(driver.now + 10_000))
    return driver


@settings(max_examples=300)
@given(_actions())
def test_every_line_gets_exactly_one_reply_naming_its_request(
    actions: list[tuple[str, Any]],
) -> None:
    driver = _drive(actions)
    replies = [
        send.message for send in driver.sent if not isinstance(send.message, InstancesChanged)
    ]
    if driver.state.relaying:
        return
    # Each line read is answered once: by its reply, or by an error naming its id
    # (or no id, for a line that has none), and nothing is answered twice.
    assert len(replies) == driver.lines
    assert not driver.state.pending


@settings(max_examples=300)
@given(_actions())
def test_a_run_channel_is_handed_over_at_most_once_and_then_does_nothing(
    actions: list[tuple[str, Any]],
) -> None:
    driver = _drive(actions)
    relays = [
        (index, output) for index, output in enumerate(driver.outputs) if isinstance(output, Relay)
    ]
    assert len(relays) <= 1
    if relays:
        ((index, relay),) = relays
        connected = RunConnectReply(request_id=relay.request_id, run_id="r", protocol_version=1)
        assert driver.outputs[index - 1] == Send(connected)
        assert index == len(driver.outputs) - 1
        assert step(driver.state, Received(_line(_hello()), 0.0), VIBESYS_VERSION)[1] == ()


@settings(max_examples=300)
@given(_actions())
def test_a_watch_pushes_only_what_changed_and_only_while_open(
    actions: list[tuple[str, Any]],
) -> None:
    driver = _drive(actions)
    open_watches: set[str] = set()
    last: dict[str, InstanceList] = {}
    for output in driver.outputs:
        match output:
            case Watch(watch_id=watch_id):
                open_watches.add(watch_id)
                last.pop(watch_id, None)
            case Unwatch(watch_id=watch_id):
                open_watches.discard(watch_id)
            case Send(message=InstancesChanged(watch_id=watch_id, listing=listing)):
                assert watch_id in open_watches
                assert last.get(watch_id) != listing
                last[watch_id] = listing
            case _:
                pass


def test_the_first_request_must_be_hello() -> None:
    state, outputs = step(ChannelState(), Received(_line(_request("ping", "p")), 0.0), "v")
    assert state == ChannelState()
    (sent,) = outputs
    assert isinstance(sent, Send)
    assert isinstance(sent.message, HelperError)
    assert (sent.message.request_id, sent.message.code) == ("p", HelperErrorCode.HANDSHAKE_REQUIRED)


def test_a_gated_method_needs_its_feature() -> None:
    state, _ = step(ChannelState(), Received(_line(_hello(features=[])), 0.0), "v")
    _, outputs = step(state, Received(_line(_request("paths.complete", "c", prefix="/")), 0.0), "v")
    (sent,) = outputs
    assert isinstance(sent, Send)
    assert isinstance(sent.message, HelperError)
    assert sent.message.code is HelperErrorCode.FEATURE_NOT_NEGOTIATED


def test_a_reply_after_its_deadline_is_dropped_and_its_connection_released() -> None:
    state, _ = step(ChannelState(), Received(_line(_hello()), 0.0), "v")
    state, _ = step(state, Answered("h", PingReply(request_id="h")), "v")
    connect = _request("run.connect", "c", 1000, instance_id="0123456789ab", role="subscribe")
    state, outputs = step(state, Received(_line(connect), 0.0), "v")
    assert not accepts_input(state)
    state, outputs = step(state, Tick(1.0), "v")
    (sent,) = outputs
    assert isinstance(sent, Send)
    assert isinstance(sent.message, HelperError)
    assert sent.message.code is HelperErrorCode.DEADLINE_EXCEEDED
    assert accepts_input(state)
    late = RunConnectReply(request_id="c", run_id="r", protocol_version=1)
    state, outputs = step(state, Answered("c", late), "v")
    assert outputs == (Release("c"),)
    assert not state.relaying


def test_input_end_closes_once_nothing_is_in_flight() -> None:
    state, _ = step(ChannelState(), Received(_line(_hello()), 0.0), "v")
    state, outputs = step(state, InputEnded(), "v")
    assert Close() not in outputs
    _, outputs = step(state, Answered("h", PingReply(request_id="h")), "v")
    assert outputs[-1] == Close()


# ----- paths.complete -----

_NAMES = st.text(alphabet="ab.c", min_size=1, max_size=4)


@given(
    st.dictionaries(_NAMES, st.booleans(), max_size=30),
    st.text(alphabet="ab.c", max_size=3),
    st.integers(0, 10),
)
def test_completions_are_exactly_the_matching_directories_sorted_and_capped(
    entries: dict[str, bool], partial: str, cap: int
) -> None:
    scanned = [ScannedEntry(name, is_dir) for name, is_dir in entries.items()]
    names, more = select_completions(scanned, partial, cap)
    matching = sorted(
        name for name, is_dir in entries.items() if is_dir and name.startswith(partial)
    )
    assert names == tuple(matching[:cap])
    assert more == (len(matching) > cap)
    assert len(names) <= cap
    assert all(entries[name] for name in names)


@given(st.text(alphabet="ab/~.", max_size=8))
def test_a_prefix_splits_into_the_directory_listed_and_the_name_matched(prefix: str) -> None:
    expanded = FakeDirectories(tree={}).expand(prefix)
    directory, partial = split_prefix(expanded, HOME)
    assert directory.startswith("/")
    assert "/" not in partial
    absolute = expanded if expanded.startswith("/") else f"{HOME}/{expanded}"
    assert posixpath.normpath(posixpath.join(directory, partial)) == posixpath.normpath(absolute)
    if expanded == "" or expanded.endswith("/"):
        assert partial == ""


def _complete(directories: FakeDirectories, prefix: str) -> PathsCompleteReply | HelperError:
    host = fake_host(SimThreads(), SimNetwork(SimThreads()), directories=directories)
    reply = answer(PathsCompleteRequest(request_id="c", deadline_ms=1, prefix=prefix), host)
    assert isinstance(reply, PathsCompleteReply | HelperError)
    return reply


def test_tilde_expands_on_the_host_and_hints_come_with_each_entry() -> None:
    hinted = {
        f"{HOME}/proj": PathHints(vibesys_checkout=False, vibesys_project=True, git_repository=True)
    }
    directories = FakeDirectories(
        tree={HOME: {"proj": True, "pics": True, "notes.txt": False}}, hinted=hinted
    )
    reply = _complete(directories, "~/p")
    assert isinstance(reply, PathsCompleteReply)
    assert reply.directory == HOME
    assert [(entry.name, entry.path) for entry in reply.entries] == [
        ("pics", f"{HOME}/pics"),
        ("proj", f"{HOME}/proj"),
    ]
    assert reply.entries[1].vibesys_project
    assert reply.entries[1].git_repository
    assert not reply.truncated


def test_a_huge_directory_is_capped_and_marked_truncated() -> None:
    names = {f"d{index:04}": True for index in range(PATHS_COMPLETE_CAP + 5)}
    reply = _complete(FakeDirectories(tree={"/big": names}), "/big/")
    assert isinstance(reply, PathsCompleteReply)
    assert len(reply.entries) == PATHS_COMPLETE_CAP
    assert reply.truncated


@pytest.mark.parametrize(
    ("prefix", "code"),
    [
        ("/missing/x", HelperErrorCode.PATH_MISSING),
        ("/home/user/notes.txt/", HelperErrorCode.PATH_NOT_DIRECTORY),
        ("/secret/", HelperErrorCode.PERMISSION_DENIED),
    ],
)
def test_unlistable_directories_are_typed_errors_naming_the_path(
    prefix: str, code: HelperErrorCode
) -> None:
    directories = FakeDirectories(
        tree={HOME: {"notes.txt": False}, "/secret": {}}, denied=frozenset({"/secret"})
    )
    reply = _complete(directories, prefix)
    assert isinstance(reply, HelperError)
    assert reply.code is code
    assert reply.path is not None
    assert prefix.startswith(reply.path.rstrip("/"))


# ----- runs, launches -----


def test_a_runs_phase_is_its_live_servers_else_its_latest_tombstone() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    runs = [ProjectRun(run_id, None, run_id, created) for run_id in ("live", "ended", "never")]
    stopped = RunPhase(status=RunStatus.STOPPED)
    failed = RunPhase(status=RunStatus.FAILED, reason={"code": "x", "stage": "s", "message": "m"})
    listing = InstanceList(
        instances=(record("aaaaaaaaaaaa", run_id="live"),),
        ended=(
            record("bbbbbbbbbbbb", run_id="ended", phase=failed, ended_at=1.0),
            record("cccccccccccc", run_id="ended", phase=stopped, ended_at=2.0),
            record("dddddddddddd", run_id="live", phase=stopped, ended_at=3.0),
            record(
                "eeeeeeeeeeee", run_id="never", phase=stopped, ended_at=3.0, project_root="/other"
            ),
        ),
    )
    summaries = run_summaries(Path("/project"), runs, listing)
    assert [(run.run_id, run.phase, run.instance_id) for run in summaries.runs] == [
        ("live", RunPhase(status=RunStatus.RUNNING), "aaaaaaaaaaaa"),
        ("ended", stopped, None),
        ("never", None, None),
    ]


@pytest.mark.parametrize(
    ("task", "args", "resume", "expected"),
    [
        (None, (), None, ("--detach",)),
        ("spsc", ("--max-rounds", "3"), None, ("--detach", "--task", "spsc", "--max-rounds", "3")),
        (None, (), ResumeRun(run_id="r1"), ("--detach", "--resume", "r1")),
        ("t", ("--x",), ResumeLatest(latest=True), ("--detach", "--task", "t", "--x", "--resume")),
    ],
)
def test_a_start_runs_vibesys_detach_with_its_resume_target_last(
    task: str | None,
    args: tuple[str, ...],
    resume: ResumeRun | ResumeLatest | None,
    expected: tuple[str, ...],
) -> None:
    request = RunStartRequest(
        request_id="s", deadline_ms=1, project="/p", task=task, args=args, resume=resume
    )
    assert launch_arguments(request) == expected


def _project(root: Path) -> Path:
    task = root / ".vibesys" / "tasks" / "spsc"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")
    return root


def test_a_start_launches_in_the_resolved_project_and_returns_the_launch(tmp_path: Path) -> None:
    project = _project(tmp_path / "proj")
    started = record("0123456789ab", project_root=str(project))
    launcher = FakeRunLauncher(lambda _project, _arguments: started)
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads), launcher=launcher)
    request = RunStartRequest(request_id="s", deadline_ms=1, project=str(project), task="spsc")
    reply = answer(request, host)
    assert reply == RunStartReply(request_id="s", launch=started)
    assert launcher.calls == [(project.resolve(), ("--detach", "--task", "spsc"))]


@pytest.mark.parametrize(
    ("method", "make", "code"),
    [
        ("tasks.list", lambda root: root / "missing", HelperErrorCode.PROJECT_MISSING),
        ("tasks.list", lambda root: root, HelperErrorCode.PROJECT_UNINITIALIZED),
        ("run.start", lambda root: root, HelperErrorCode.PROJECT_UNINITIALIZED),
        ("runs.list", lambda root: root / "missing", HelperErrorCode.PROJECT_MISSING),
    ],
)
def test_project_errors_are_typed_and_name_the_path(
    tmp_path: Path, method: str, make: Callable[[Path], Path], code: HelperErrorCode
) -> None:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    launcher = host.launcher
    project = make(tmp_path)
    request = parse_helper_request_dict(_request(method, "r", project=str(project)))
    reply = answer(request, host)
    assert isinstance(reply, HelperError)
    assert reply.code is code
    assert reply.path is not None
    assert reply.path.endswith(project.name)
    assert isinstance(launcher, FakeRunLauncher)
    assert launcher.calls == []


def parse_helper_request_dict(message: dict[str, Any]) -> HelperRequest:
    return parse_helper_request(_line(message))


def test_tasks_and_runs_of_a_project(tmp_path: Path) -> None:
    project = _project(tmp_path / "proj")
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    tasks = answer(
        parse_helper_request_dict(_request("tasks.list", "t", project=str(project))), host
    )
    runs = answer(parse_helper_request_dict(_request("runs.list", "r", project=str(project))), host)
    assert tasks.model_dump(mode="json")["tasks"]["tasks"] == [{"name": "spsc"}]
    assert runs.model_dump(mode="json")["runs"] == {
        "project_root": str(project.resolve()),
        "runs": [],
    }


# ----- the shell, on simulated threads and network -----


@pytest.mark.parametrize("seed", range(8))
def test_a_slow_request_does_not_hold_back_others(seed: int, tmp_path: Path) -> None:
    project = _project(tmp_path / f"p{seed}")
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    gate = threads.event()
    started = record("0123456789ab")

    def slow(_project: Path, _arguments: tuple[str, ...]) -> LiveInstanceRecord:
        gate.wait(HANG_GUARD_S * 2)
        return started

    def main() -> list[dict[str, Any]]:
        client = ChannelClient(threads)
        host = fake_host(threads, network, launcher=FakeRunLauncher(slow))
        worker = serve_on(threads, client, ByteBuffer(), host)
        client.send(_hello())
        client.receive()
        client.send(_request("run.start", "s", 600_000, project=str(project)))
        client.send(_request("ping", "p"))
        received = [client.receive()]
        gate.set()
        received.append(client.receive())
        client.close()
        worker.join(HANG_GUARD_S)
        return received

    first, second = threads.run(main)
    assert first == {"type": "ping", "request_id": "p"}
    assert (second["type"], second["request_id"]) == ("run.start", "s")
    assert second["launch"]["id"] == started.id


@pytest.mark.parametrize("seed", range(8))
def test_a_request_past_its_deadline_is_answered_deadline_exceeded(
    seed: int, tmp_path: Path
) -> None:
    project = _project(tmp_path / f"p{seed}")
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    gate = threads.event()

    def slow(_project: Path, _arguments: tuple[str, ...]) -> LiveInstanceRecord:
        gate.wait(HANG_GUARD_S * 2)
        return record("0123456789ab")

    def main() -> tuple[dict[str, Any], float]:
        client = ChannelClient(threads)
        host = fake_host(threads, network, launcher=FakeRunLauncher(slow))
        worker = serve_on(threads, client, ByteBuffer(), host)
        client.send(_hello())
        client.receive()
        started = threads.now()
        client.send(_request("run.start", "s", 1500, project=str(project)))
        reply = client.receive()
        waited = threads.now() - started
        gate.set()
        client.close()
        worker.join(HANG_GUARD_S)
        return reply, waited

    reply, waited = threads.run(main)
    assert (reply["request_id"], reply["code"]) == ("s", "deadline_exceeded")
    assert waited == pytest.approx(1.5)


@pytest.mark.parametrize("seed", range(6))
def test_a_watch_pushes_the_listing_and_each_change(seed: int) -> None:
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    store = FakeInstanceStore()

    def main() -> list[dict[str, Any]]:
        client = ChannelClient(threads)
        worker = serve_on(threads, client, ByteBuffer(), fake_host(threads, network, store=store))
        client.send(_hello())
        client.receive()
        client.send(_request("instances.watch", "w"))
        received = [client.receive(), client.receive()]
        store.hold("0123456789ab").publish(record("0123456789ab"))
        received.append(client.receive())
        client.send(_request("instances.unwatch", "u", watch_id="w"))
        received.append(client.receive())
        client.close()
        worker.join(HANG_GUARD_S)
        return received

    watched, first, changed, unwatched = threads.run(main)
    assert watched == {"type": "instances.watch", "request_id": "w", "watch_id": "w"}
    assert (first["type"], first["listing"]["instances"]) == ("instances.changed", [])
    assert [record["id"] for record in changed["listing"]["instances"]] == ["0123456789ab"]
    assert unwatched["type"] == "instances.unwatch"


_FRAMES = st.lists(
    st.binary(max_size=30).map(lambda body: body.replace(b"\n", b" ") + b"\n"),
    min_size=1,
    max_size=4,
).map(b"".join)


@settings(max_examples=30, deadline=None)
@given(upstream=_FRAMES, downstream=_FRAMES, seed=st.integers(0, 2**16), early=st.booleans())
def test_run_connect_relays_bytes_both_ways_including_ones_sent_early(
    upstream: bytes, downstream: bytes, seed: int, *, early: bool
) -> None:
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    store = FakeInstanceStore()
    store.hold("0123456789ab").publish(record("0123456789ab", run_id="run-7"))
    seen: dict[str, bytes] = {}

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        received = bytearray()
        while len(received) < len(upstream):
            chunk = connection.recv(len(upstream) - len(received), HANG_GUARD_S)
            assert chunk
            received += chunk
        seen["upstream"] = bytes(received)
        connection.send(downstream)
        connection.close()

    def main() -> tuple[dict[str, Any], int | None, bytes, bytes]:
        listener = network.listen("run.sock")
        server = threads.spawn(lambda: serve(listener), name="server")
        client = ChannelClient(threads)
        errors = ByteBuffer()
        worker = serve_on(threads, client, errors, fake_host(threads, network, store=store))
        client.send(_hello())
        client.receive()
        connect = _line(_request("run.connect", "c", instance_id="0123456789ab", role="chat"))
        client.send(connect + b"\n" + (upstream if early else b""))
        connected = client.receive()
        if not early:
            client.send(upstream)
        worker.join(HANG_GUARD_S)
        server.join(HANG_GUARD_S)
        client.close()
        return connected, client.status, bytes(client.data), bytes(errors.data)

    connected, status, relayed, errors = threads.run(main)
    assert connected == {
        "type": "run.connect",
        "request_id": "c",
        "run_id": "run-7",
        "protocol_version": 1,
    }
    assert seen["upstream"] == upstream
    assert relayed == downstream
    assert status == EXIT_STATUS[BridgeOutcome.SERVER_CLOSED]
    assert BridgeReport.model_validate_json(errors).outcome is BridgeOutcome.SERVER_CLOSED


def test_run_connect_to_a_server_that_is_gone_is_a_typed_error_and_the_channel_lives_on() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)

    def main() -> list[dict[str, Any]]:
        client = ChannelClient(threads)
        worker = serve_on(threads, client, ByteBuffer(), fake_host(threads, network))
        client.send(_hello())
        client.receive()
        client.send(_request("run.connect", "c", instance_id="0123456789ab", role="control"))
        received = [client.receive()]
        client.send(_request("ping", "p"))
        received.append(client.receive())
        client.close()
        worker.join(HANG_GUARD_S)
        return received

    gone, pong = threads.run(main)
    assert (gone["code"], pong["type"]) == ("run_gone", "ping")


def test_ids_in_a_burst_of_requests_each_come_back_once() -> None:
    threads = SimThreads(schedule_seed=3)
    network = SimNetwork(threads)
    ids = [f"r{index}" for index in range(12)]

    def main() -> list[str]:
        client = ChannelClient(threads)
        worker = serve_on(threads, client, ByteBuffer(), fake_host(threads, network))
        client.send(_hello())
        client.receive()
        client.send(b"".join(_line(_request("ping", rid)) + b"\n" for rid in ids))
        received = [client.receive()["request_id"] for _ in ids]
        client.close()
        worker.join(HANG_GUARD_S)
        assert client.status == 0
        return received

    assert sorted(threads.run(main)) == sorted(ids)
