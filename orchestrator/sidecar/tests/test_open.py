"""The session contract before a session exists: the bootstrap, ``Hello`` and ``Open``."""

from __future__ import annotations

import io
import os
import signal
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from agentshim.testing import CodexScript, FakeExecutor
from hypothesis import given
from hypothesis import strategies as st

from tests import helpers as h
from tests.fakes import ScriptedDriver
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.holder import (
    PROTOCOL,
    HolderLock,
    InvalidRequestError,
    OtherSessionError,
    SessionHolder,
)
from vs_sidecar.main import BootstrapError, read_bootstrap, real_drivers, serve_holder
from vs_sidecar.store import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

HANG_GUARD_SECONDS = 30
HOST = orch.BuildVersions(
    binary_version="t", protocol_version=PROTOCOL, min_protocol_version=PROTOCOL
)


class _Counted:
    """An ``open_session`` that counts the agents it would start."""

    def __init__(self) -> None:
        self.opened: list[sc.OpenSessionRequest] = []

    def __call__(self, request: sc.OpenSessionRequest) -> Engine:
        self.opened.append(request)
        return Engine(request.session, MemoryStore(), lambda _sink: ScriptedDriver())


def test_a_process_nobody_opened_says_so_and_then_who_it_holds() -> None:
    holder = SessionHolder(_Counted())
    hello = holder.hello(sc.SessionHelloRequest(client=HOST))
    assert not hello.HasField("session")
    assert hello.compatibility == orch.COMPATIBILITY_COMPATIBLE
    assert hello.versions.protocol_version == PROTOCOL
    holder.open(h.open_request())
    assert holder.hello(sc.SessionHelloRequest(client=HOST)).session == h.REF


@given(repeats=st.integers(1, 5))
def test_a_repeated_open_answers_the_same_and_starts_no_second_agent(repeats: int) -> None:
    count = _Counted()
    holder = SessionHolder(count)
    first = holder.open(h.open_request())
    for _ in range(repeats):
        assert holder.open(h.open_request()) == first
    assert len(count.opened) == 1
    assert first.session == h.REF


def test_an_open_for_another_session_is_refused_and_starts_nothing() -> None:
    count = _Counted()
    holder = SessionHolder(count)
    holder.open(h.open_request())
    other = fx_session("agent-2", 1)
    with pytest.raises(OtherSessionError, match="agent-2"):
        holder.open(h.open_request(session=other))
    assert len(count.opened) == 1


def fx_session(agent: str, number: int) -> Any:
    return h.REF.__class__(agent_id=agent, session=number)


text = st.text(max_size=20)
relative = st.text(min_size=1, max_size=20).filter(lambda p: not p.startswith("/"))
bad_name = st.text(min_size=1, max_size=20).filter(
    lambda n: not n.replace("_", "a").isalnum() or not n.isascii() or n[0].isdigit()
)


def _set(field: str, value: Any) -> Callable[[sc.OpenSessionRequest], None]:
    def apply(request: sc.OpenSessionRequest) -> None:
        if value is None:
            request.ClearField(field)
        else:
            setattr(request, field, value)

    return apply


def _tool(**fields: Any) -> Callable[[sc.OpenSessionRequest], None]:
    def apply(request: sc.OpenSessionRequest) -> None:
        base: dict[str, Any] = {"name": "vibesys", "argv": ["/bin/vibesys", "mcp"]}
        request.tools.add(**(base | fields))

    return apply


def _set_key(field: str, name: str) -> Callable[[sc.OpenSessionRequest], None]:
    def apply(request: sc.OpenSessionRequest) -> None:
        getattr(request.environment, field)[name] = "x"

    return apply


def _unset(name: str) -> Callable[[sc.OpenSessionRequest], None]:
    def apply(request: sc.OpenSessionRequest) -> None:
        request.environment.unset.append(name)

    return apply


def _both(request: sc.OpenSessionRequest) -> None:
    request.environment.set["BOTH"] = "x"
    request.environment.unset.append("BOTH")


def _two_tools(request: sc.OpenSessionRequest) -> None:
    for _ in range(2):
        request.tools.add(name="vibesys", argv=["/bin/vibesys"])


#: Each way to break a request, and what the refusal must name.
BREAKS = st.one_of(
    st.just((_set("session", None), "session")),
    st.just((_set("harness", 0), "harness")),
    st.just((_set("working_directory", ""), "working_directory")),
    relative.map(lambda p: (_set("working_directory", p), "working_directory")),
    st.just((_set("agent_data_directory", ""), "agent_data_directory")),
    relative.map(lambda p: (_set("agent_data_directory", p), "agent_data_directory")),
    st.just((_set("skills", None), "skills")),
    relative.map(lambda p: (lambda r, p=p: setattr(r.skills, "directory", p), "skills.directory")),
    st.just((lambda r: setattr(r.skills, "name", ""), "skills.name")),
    st.just((_set("environment", None), "environment")),
    bad_name.map(lambda n: (_set_key("set", n), "environment.set")),
    bad_name.map(lambda n: (_unset(n), "environment.unset")),
    st.just((_both, "environment")),
    st.just((_tool(name=""), "tools[0].name")),
    st.just((_tool(argv=[]), "tools[0].argv")),
    relative.map(lambda p: (_tool(argv=[p]), "tools[0].argv")),
    bad_name.map(lambda n: (_tool(env={n: "x"}), "tools[0].env")),
    st.just((_two_tools, "tools")),
    st.just((_set("resume_token", b"x" * 4097), "resume_token")),
)


@given(breaking=BREAKS)
def test_a_bad_field_is_refused_by_name_before_anything_starts(breaking: tuple[Any, str]) -> None:
    breaker, field = breaking
    count = _Counted()
    holder = SessionHolder(count)
    request = h.open_request()
    breaker(request)
    with pytest.raises(InvalidRequestError, match=field.replace("[", r"\[").replace("]", r"\]")):
        holder.open(request)
    assert count.opened == []
    assert not holder.hello(sc.SessionHelloRequest(client=HOST)).HasField("session")


@given(
    protocol=st.integers(0, 2**32 - 1),
    oldest=st.integers(0, 2**32 - 1),
)
def test_compatibility_is_whether_the_two_share_a_protocol(protocol: int, oldest: int) -> None:
    holder = SessionHolder(_Counted())
    client = orch.BuildVersions(protocol_version=protocol, min_protocol_version=oldest)
    answer = holder.hello(sc.SessionHelloRequest(client=client)).compatibility
    if protocol < PROTOCOL:
        assert answer == orch.COMPATIBILITY_CLIENT_TOO_OLD
    elif oldest > PROTOCOL:
        assert answer == orch.COMPATIBILITY_DAEMON_TOO_OLD
    else:
        assert answer == orch.COMPATIBILITY_COMPATIBLE


def test_a_hello_with_no_versions_is_refused_by_name() -> None:
    with pytest.raises(InvalidRequestError, match="client"):
        SessionHolder(_Counted()).hello(sc.SessionHelloRequest())


# -- the bootstrap


def _bootstrap(root: str = "/s") -> sc.SessionBootstrap:
    return sc.SessionBootstrap(
        listen_socket=f"{root}/sock",
        state_directory=root,
        lock_file=f"{root}/holder.lock",
        processes_file=f"{root}/processes.pb",
    )


def test_a_bootstrap_is_read_from_the_whole_stream() -> None:
    wanted = _bootstrap()
    assert read_bootstrap(io.BytesIO(wanted.SerializeToString())) == wanted


@pytest.mark.parametrize(
    "field", ["listen_socket", "state_directory", "lock_file", "processes_file"]
)
@given(path=st.one_of(st.just(""), relative))
def test_a_bad_bootstrap_is_refused_by_name(field: str, path: str) -> None:
    good = _bootstrap()
    bad = _bootstrap()
    setattr(bad, field, path)
    with pytest.raises(BootstrapError, match=field):
        read_bootstrap(io.BytesIO(bad.SerializeToString()))
    assert read_bootstrap(io.BytesIO(good.SerializeToString())) == good


def test_standard_input_that_ended_before_any_bootstrap_is_refused() -> None:
    with pytest.raises(BootstrapError):
        read_bootstrap(io.BytesIO(b""))


def test_standard_input_that_is_not_a_bootstrap_is_refused() -> None:
    with pytest.raises(BootstrapError, match="not a SessionBootstrap"):
        read_bootstrap(io.BytesIO(b"\xff\xff\xff"))


# -- over a real socket


class _LeftError(Exception):
    pass


def _leave() -> NoReturn:
    raise _LeftError


def test_a_process_nobody_opens_ends_after_its_wait_and_leaves_no_state() -> None:
    with tempfile.TemporaryDirectory(prefix="vs") as directory:
        root = Path(directory)
        bootstrap = _bootstrap(str(root / "state"))
        with pytest.raises(_LeftError):
            serve_holder(
                bootstrap,
                lambda _request, _state: pytest.fail("nobody opened it"),
                open_wait=0,
                leave=_leave,
            )
        assert not (root / "state" / "events.log").exists()
        record = sc.SessionProcesses.FromString((root / "state" / "processes.pb").read_bytes())
        assert record == sc.SessionProcesses(holder_pid=os.getpid())
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)


def test_a_second_process_for_a_session_ends_at_once_and_touches_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="vs") as directory:
        root = Path(directory)
        bootstrap = _bootstrap(directory)
        first = HolderLock.take(Path(bootstrap.lock_file))
        try:
            with pytest.raises(_LeftError):
                serve_holder(
                    bootstrap,
                    lambda _request, _state: pytest.fail("nobody opened it"),
                    leave=_leave,
                )
        finally:
            first.release()
        assert sorted(p.name for p in root.iterdir()) == ["holder.lock"]


def test_a_process_whose_predecessor_died_takes_the_lock_whatever_files_are_left() -> None:
    with tempfile.TemporaryDirectory(prefix="vs") as directory:
        root = Path(directory)
        bootstrap = _bootstrap(directory)
        (root / "holder.lock").write_text("left over")
        (root / "processes.pb").write_bytes(
            sc.SessionProcesses(
                holder_pid=1234, agent_process_groups=[sc.AgentProcessGroup(id=1234)]
            ).SerializeToString()
        )
        with pytest.raises(_LeftError):
            serve_holder(
                bootstrap,
                lambda _request, _state: pytest.fail("nobody opened it"),
                open_wait=0,
                leave=_leave,
            )
        record = sc.SessionProcesses.FromString((root / "processes.pb").read_bytes())
        assert record == sc.SessionProcesses(holder_pid=os.getpid())
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)


# -- a bundle that cannot be loaded


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        "{",
        '{"name": "vibesys", "plugins": []}',
    ],
    ids=["missing", "not json", "names no such plugin"],
)
def test_a_bundle_that_cannot_be_loaded_comes_back_as_the_start_failure(
    tmp_path: Path, manifest: str | None
) -> None:
    root = tmp_path / "market"
    (root / ".claude-plugin").mkdir(parents=True)
    if manifest is not None:
        (root / ".claude-plugin" / "marketplace.json").write_text(manifest)
    request = h.open_request(
        orch.HARNESS_CODEX,
        skills=sc.SessionSkills(directory=str(root), name="vibesys"),
        working_directory=str(tmp_path),
    )
    executor = FakeExecutor([], peers=CodexScript().peer)
    factory = h.driver_factory_over(
        "codex",
        executor,
        request_fields={
            "skills": request.skills,
            "working_directory": request.working_directory,
        },
    )
    store = MemoryStore()
    engine = Engine(request.session, store, factory)
    engine.start()
    events = store.events_after(0)
    assert h.kinds(events) == ["failed"]
    assert events[0].failed.start_failure_reason == orch.START_FAILURE_REASON_PLUGIN_UNAVAILABLE
    assert executor.spawns == []


def test_the_real_drivers_are_made_for_the_request_without_starting_anything() -> None:
    request = h.open_request()
    assert callable(real_drivers(request, Path("/nowhere")))
