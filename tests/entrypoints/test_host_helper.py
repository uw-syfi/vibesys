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
    InstanceList,
    InstancesListReply,
    InstancesListRequest,
    PathsCompleteReply,
    PathsCompleteRequest,
    RunStopReply,
    RunStopRequest,
    StopOutcome,
)
from server.host_helper import answer
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path


def test_the_real_host_reads_this_users_registry_and_filesystem(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    (tmp_path / "projects" / "alpha").mkdir(parents=True)
    host = real_host({"XDG_RUNTIME_DIR": str(runtime)}, os.getuid(), SimThreads())

    listing = answer(InstancesListRequest(request_id="l", deadline_ms=1), host)
    prefix = f"{tmp_path}/projects/a"
    completed = answer(PathsCompleteRequest(request_id="c", deadline_ms=1, prefix=prefix), host)
    stopped = answer(RunStopRequest(request_id="s", deadline_ms=1, instance_id="0" * 12), host)

    assert listing == InstancesListReply(request_id="l", listing=InstanceList(instances=()))
    assert isinstance(completed, PathsCompleteReply)
    assert [entry.name for entry in completed.entries] == ["alpha"]
    assert isinstance(stopped, RunStopReply)
    assert stopped.result.outcome is StopOutcome.NOT_RUNNING


def test_an_unsafe_registry_root_is_refused(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o777)
    (runtime / "vibesys").mkdir(mode=0o777)
    (runtime / "vibesys").chmod(0o777)

    with pytest.raises(PermissionError):
        real_host({"XDG_RUNTIME_DIR": str(runtime)}, os.getuid(), SimThreads())
