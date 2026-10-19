"""``run.reader``: Chat on a run with no live server reads it through a read-only one.

The helper starts the run server in its read-only mode for the run on demand, hands every client
that asks while it lives the same one, and starts none when the run's own server is live. It
launches nothing for a run the project does not have, and a launch that failed leaves the next
ask free to try again.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.host_helper_support import (
    FakeRunLauncher,
    accept_operator,
    failed_launch,
    fake_host,
    project_with_run,
    record,
)

from server.host_api import (
    RUN_EVENTS_FILE,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    InstanceStopResult,
    LiveInstanceRecord,
    RunConnectReply,
    RunConnectRequest,
    RunRole,
    StopOutcome,
    request_features,
)
from server.host_helper_readers import Readers
from server.host_helper_requests import answer, connect, reader_arguments
from server.host_run_reader import RunReaderReply, RunReaderRequest
from server.registry import FakeInstanceStore, LiveRegistry
from vs_project.api import Project
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from server.host_helper_requests import HelperHost
    from server.registry import InstanceHold

_RUN = "20261010-120000-11111111-dynamic"
_OTHER_RUN = "20261010-130000-22222222-dynamic"
_SEEDS = settings(deadline=None, max_examples=examples(pr=8, nightly=200))


def _ended_project(directory: Path, run_ids: tuple[str, ...] = (_RUN,)) -> Path:
    """A project whose runs each recorded a journal and have no live server."""
    root = project_with_run(directory, run_ids[0])
    for run_id in run_ids:
        journal = Project.log_directory_for(root.resolve(), run_id) / RUN_EVENTS_FILE
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_bytes(b"")
    return root


class _Readers:
    """A host's reader registry over a Fake store, and a launcher that starts a reader into it.

    A launch registers a reader the way a started read-only server does, under the next id.
    """

    def __init__(self, threads: SimThreads, network: SimNetwork, launch_time: float = 0.0) -> None:
        self.store = FakeInstanceStore()
        self.threads = threads
        self.network = network
        self.launch_time = launch_time
        self.holds: dict[str, InstanceHold] = {}
        self.launcher = FakeRunLauncher(self._launch)

    def _launch(self, _project: Path, arguments: tuple[str, ...]) -> LiveInstanceRecord:
        if self.launch_time:
            self.threads.sleep(self.launch_time)  # a launch takes time; others may ask meanwhile
        run_id = arguments[arguments.index("--run-id") + 1]
        started = record(f"{len(self.holds) + 10:012x}", run_id=run_id)
        hold = self.store.hold(started.id)
        hold.publish(started)
        self.holds[started.id] = hold
        return started

    def host(self, store: FakeInstanceStore | None = None) -> HelperHost:
        base = fake_host(self.threads, self.network, store=store, launcher=self.launcher)
        return replace(base, readers=Readers(LiveRegistry(self.store, self.threads), self.threads))


def _ask(host: HelperHost, threads: SimThreads, project: Path, run_id: str = _RUN) -> object:
    request = RunReaderRequest(
        request_id="r", deadline_ms=5000, project=str(project), run_id=run_id
    )
    return answer(request, host, threads.event())


def test_opening_chat_on_an_ended_run_starts_exactly_one_read_only_server(tmp_path: Path) -> None:
    threads = SimThreads()
    project = _ended_project(tmp_path / "p")
    readers = _Readers(threads, SimNetwork(threads))

    reply = _ask(readers.host(), threads, project)

    assert isinstance(reply, RunReaderReply)
    assert reply.read_only
    assert list(readers.holds) == [reply.instance_id]
    ((launched_in, arguments),) = readers.launcher.calls
    journal = Project.log_directory_for(project.resolve(), _RUN) / RUN_EVENTS_FILE
    assert launched_in == project.resolve()
    assert arguments == reader_arguments(journal, _RUN)
    assert "--reopen" in arguments


@_SEEDS
@given(opens=st.integers(2, 6), seed=st.integers(0, 2**32), launch_time=st.sampled_from([0.0, 2.0]))
def test_every_open_of_a_run_while_its_reader_lives_shares_one_server(
    tmp_path_factory: pytest.TempPathFactory, opens: int, seed: int, launch_time: float
) -> None:
    """Opens that arrive one after another, or while the first is still launching, share a reader."""
    threads = SimThreads(schedule_seed=seed)
    project = _ended_project(tmp_path_factory.mktemp("p") / "p")
    readers = _Readers(threads, SimNetwork(threads), launch_time)
    host = readers.host()
    replies: list[object] = []

    def open_chat() -> None:
        replies.append(_ask(host, threads, project))

    def main() -> None:
        workers = [threads.spawn(open_chat, name=f"open-{n}", daemon=False) for n in range(opens)]
        for worker in workers:
            worker.join(HANG_GUARD_S)

    threads.run(main)

    assert len(readers.launcher.calls) == 1
    assert {reply.instance_id for reply in replies if isinstance(reply, RunReaderReply)} == set(
        readers.holds
    )
    assert len(replies) == opens


def test_each_run_has_its_own_reader(tmp_path: Path) -> None:
    threads = SimThreads()
    project = _ended_project(tmp_path / "p", (_RUN, _OTHER_RUN))
    readers = _Readers(threads, SimNetwork(threads))
    host = readers.host()

    first = _ask(host, threads, project, _RUN)
    second = _ask(host, threads, project, _OTHER_RUN)
    again = _ask(host, threads, project, _RUN)

    assert isinstance(first, RunReaderReply)
    assert isinstance(second, RunReaderReply)
    assert first.instance_id != second.instance_id
    assert again == first
    assert len(readers.launcher.calls) == 2


def test_a_reader_that_ended_is_replaced_by_the_next_open(tmp_path: Path) -> None:
    """A reader ends by itself once nothing holds it; asking again starts a new one."""
    threads = SimThreads()
    project = _ended_project(tmp_path / "p")
    readers = _Readers(threads, SimNetwork(threads))
    host = readers.host()
    first = _ask(host, threads, project)
    assert isinstance(first, RunReaderReply)

    readers.holds[first.instance_id].release()
    second = _ask(host, threads, project)

    assert isinstance(second, RunReaderReply)
    assert second.instance_id != first.instance_id
    assert len(readers.launcher.calls) == 2


def test_a_run_with_a_live_server_is_read_through_it_and_starts_no_reader(
    tmp_path: Path,
) -> None:
    threads = SimThreads()
    project = _ended_project(tmp_path / "p")
    live_store = FakeInstanceStore()
    live = record("0123456789ab", run_id=_RUN)
    live_store.hold(live.id).publish(live)
    readers = _Readers(threads, SimNetwork(threads))

    reply = _ask(readers.host(live_store), threads, project)

    assert reply == RunReaderReply(request_id="r", instance_id=live.id, read_only=False)
    assert readers.launcher.calls == []


def test_a_run_the_project_does_not_have_starts_no_reader(tmp_path: Path) -> None:
    threads = SimThreads()
    project = _ended_project(tmp_path / "p")
    readers = _Readers(threads, SimNetwork(threads))

    reply = _ask(readers.host(), threads, project, "no-such-run")

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNKNOWN_RUN
    assert readers.launcher.calls == []


def test_a_reader_that_failed_to_start_is_reported_and_tried_again(tmp_path: Path) -> None:
    threads = SimThreads()
    project = _ended_project(tmp_path / "p")
    failing = FakeRunLauncher(failed_launch)
    host = replace(
        fake_host(threads, SimNetwork(threads), launcher=failing),
        readers=Readers(LiveRegistry(FakeInstanceStore(), threads), threads),
    )

    first = _ask(host, threads, project)
    second = _ask(host, threads, project)

    assert isinstance(first, HelperError)
    assert first.code is HelperErrorCode.READER_FAILED
    assert isinstance(second, HelperError)
    assert len(failing.calls) == 2


def test_run_connect_reaches_a_reader_as_it_reaches_a_live_run(tmp_path: Path) -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    project = _ended_project(tmp_path / "p")
    readers = _Readers(threads, network)
    host = readers.host()
    reply = _ask(host, threads, project)
    assert isinstance(reply, RunReaderReply)
    listener = network.listen("/run.sock")

    def serve() -> None:
        accept_operator(listener).close()

    def main() -> object:
        server = threads.spawn(serve, name="reader-socket")
        opened = connect(
            RunConnectRequest(
                request_id="c", deadline_ms=5000, instance_id=reply.instance_id, role=RunRole.CHAT
            ),
            host,
        )
        server.join(HANG_GUARD_S)
        return opened

    opened = threads.run(main)

    assert isinstance(opened, tuple)
    connected, connection = opened
    connection.close()
    assert connected == RunConnectReply(request_id="c", run_id=_RUN, protocol_version=1)


def test_a_reader_is_not_a_run_the_helper_stops_or_lists(tmp_path: Path) -> None:
    """A reader registers apart from the runs, so no listing of runs shows it and no stop reaches it."""
    threads = SimThreads()
    project = _ended_project(tmp_path / "p")
    readers = _Readers(threads, SimNetwork(threads))
    host = readers.host()
    reply = _ask(host, threads, project)
    assert isinstance(reply, RunReaderReply)

    assert host.registry.list().instances == ()
    stopped = host.registry.stop(reply.instance_id, host.stop_effects)
    assert stopped == InstanceStopResult(id=reply.instance_id, outcome=StopOutcome.NOT_RUNNING)


def test_the_reader_request_needs_its_feature_and_hello_offers_it() -> None:
    request = RunReaderRequest(request_id="r", deadline_ms=1000, project="/p", run_id=_RUN)
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))

    assert request_features(request) == (HelperFeature.CHAT_ENDED_RUNS,)
    assert HelperFeature.CHAT_ENDED_RUNS in host.offered_features()


def test_a_client_that_asks_is_told_the_reader_answers_chat(tmp_path: Path) -> None:
    threads = SimThreads()
    project = _ended_project(tmp_path / "p")
    host = _Readers(threads, SimNetwork(threads)).host()

    def ask(*, with_chat: bool | None) -> object:
        request = RunReaderRequest(
            request_id="r",
            deadline_ms=5000,
            project=str(project),
            run_id=_RUN,
            with_chat=with_chat,
        )
        return answer(request, host, threads.event())

    asked, unasked = ask(with_chat=True), ask(with_chat=None)

    assert isinstance(asked, RunReaderReply)
    assert asked.chat is True
    assert isinstance(unasked, RunReaderReply)
    assert unasked.chat is None
    request = RunReaderRequest(
        request_id="r", deadline_ms=1000, project="/p", run_id=_RUN, with_chat=True
    )
    assert request_features(request) == (
        HelperFeature.CHAT_ENDED_RUNS,
        HelperFeature.CHAT_ENDED_RUN_QUESTIONS,
    )
