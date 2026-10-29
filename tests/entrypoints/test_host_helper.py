"""The host helper's wiring: the real host it composes answers requests in process.

The channel itself is covered in ``tests/server/test_host_helper.py``; the real
process over stdio in ``tests/e2e/test_host_helper.py``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from entrypoints.host_helper import real_host
from server.host_api import (
    HarnessesListReply,
    HarnessesListRequest,
    InstanceList,
    InstancesListReply,
    InstancesListRequest,
    InstanceStatus,
    LiveInstanceRecord,
    PathsCompleteReply,
    PathsCompleteRequest,
    RunPhase,
    RunStatus,
    RunStopReply,
    RunStopRequest,
    StopOutcome,
)
from server.host_helper_requests import answer
from server.registry import FileInstanceStore, instance_root
from vibesys.api import harnesses
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path


def test_the_real_host_reads_this_users_registry_and_filesystem(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    (tmp_path / "projects" / "alpha").mkdir(parents=True)
    threads = SimThreads()
    host = real_host({"XDG_RUNTIME_DIR": str(runtime)}, os.getuid(), threads)

    listing = answer(InstancesListRequest(request_id="l", deadline_ms=1), host, threads.event())
    prefix = f"{tmp_path}/projects/a"
    completed = answer(
        PathsCompleteRequest(request_id="c", deadline_ms=1, prefix=prefix), host, threads.event()
    )
    stopped = answer(
        RunStopRequest(request_id="s", deadline_ms=1, instance_id="0" * 12), host, threads.event()
    )

    assert listing == InstancesListReply(request_id="l", listing=InstanceList(instances=()))
    assert isinstance(completed, PathsCompleteReply)
    assert [entry.name for entry in completed.entries] == ["alpha"]
    assert isinstance(stopped, RunStopReply)
    assert stopped.result.outcome is StopOutcome.NOT_RUNNING


def test_the_real_host_offers_the_vetted_harnesses_with_one_default_per_provider(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    threads = SimThreads()
    host = real_host({"XDG_RUNTIME_DIR": str(runtime)}, os.getuid(), threads)

    reply = answer(HarnessesListRequest(request_id="h", deadline_ms=1), host, threads.event())

    assert isinstance(reply, HarnessesListReply)
    offered = reply.harnesses
    assert [(o.provider, o.model) for o in offered] == [(h.provider, h.model) for h in harnesses()]
    for provider in {option.provider for option in offered}:
        models = [option for option in offered if option.provider == provider]
        assert [option.default for option in models] == [True] + [False] * (len(models) - 1)
    # Every offered pair says how it reads, and the wire form round-trips with it.
    assert all(option.display is not None for option in offered)
    assert HarnessesListReply.model_validate_json(reply.model_dump_json()) == reply


def test_the_real_host_names_each_vetted_pairs_maker_to_a_client_that_asks(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    threads = SimThreads()
    host = real_host({"XDG_RUNTIME_DIR": str(runtime)}, os.getuid(), threads)

    asked = answer(
        HarnessesListRequest(request_id="h", deadline_ms=1, with_generations=True),
        host,
        threads.event(),
    )
    plain = answer(HarnessesListRequest(request_id="h", deadline_ms=1), host, threads.event())

    assert isinstance(asked, HarnessesListReply)
    assert isinstance(plain, HarnessesListReply)
    # A vendor outside the closed set of makers would read as no maker: say so here.
    assert all(
        option.display is not None and option.display.maker is not None
        for option in asked.harnesses
    )
    # A client that did not ask sees neither field on the wire.
    assert "maker" not in plain.model_dump_json()
    assert "generation" not in plain.model_dump_json()


def test_an_unsafe_registry_root_is_refused(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o777)
    (runtime / "vibesys").mkdir(mode=0o777)
    (runtime / "vibesys").chmod(0o777)

    with pytest.raises(PermissionError):
        real_host({"XDG_RUNTIME_DIR": str(runtime)}, os.getuid(), SimThreads())


# 2001-09-09, as an epoch time: days old on the epoch clock records are stamped
# with, but far in the future of any monotonic clock (seconds since boot).
_OLD_EPOCH = 1_000_000_000.0


def test_the_real_host_ages_tombstones_on_the_epoch_clock(tmp_path: Path) -> None:
    """A tombstone days old is pruned by the next listing.

    The real host once aged records on its threads' monotonic clock. Records are
    stamped with epoch times, so every tombstone looked younger than its
    retention and stayed listed for ever, and a holder wedged after its
    tombstone never outlived its exit grace.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    store = FileInstanceStore(instance_root({"XDG_RUNTIME_DIR": str(runtime)}, os.getuid()))
    hold = store.hold("0123456789ab")
    hold.publish(
        LiveInstanceRecord(
            id="0123456789ab",
            status=InstanceStatus.SERVING,
            phase=RunPhase(status=RunStatus.STOPPED),
            socket_path=str(runtime / "control.sock"),
            log_path=str(runtime / "server.log"),
            project_root=str(tmp_path),
            run_id="run-1",
            pid=4242,
            hostname="node",
            vibesys_version="0+test",
            started_at=_OLD_EPOCH - 60,
            ended_at=_OLD_EPOCH,
        )
    )
    hold.release()
    host = real_host({"XDG_RUNTIME_DIR": str(runtime)}, os.getuid(), SimThreads())

    host.registry.list()

    assert store.ids() == ()
