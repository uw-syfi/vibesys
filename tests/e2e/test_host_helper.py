"""A real host helper process over stdio: conformance, start-up, and a relayed run channel.

Each channel is one helper process whose stdin and stdout stand in for an ssh exec
channel. ``VIBESYS_HOST_HELPER_COMMAND`` (a shell-style command line) points the runner
at another helper implementation; the default is ``python -m entrypoints.host_helper``
from this checkout. Start-up time is reported against the 300 ms budget as a test
property and on stdout, never asserted: a test's result must not depend on timing.
The deterministic tests of the helper are ``tests/server/test_host_helper.py`` and
``tests/conformance/test_host_helper.py``.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from typing import IO, TYPE_CHECKING, Any, cast

import pytest
from tests.conformance.replay import load_scenario, run_steps, runner_group
from tests.server.support import DEADLOCK_GUARD_S, build_server_parts
from tests.support.light_imports import BUDGET_MS, is_heavy, loaded_modules

from server.host_api import InstanceStatus, LiveInstanceRecord, RunPhase, RunStatus
from server.registry import FileInstanceStore, instance_root
from server.transport.unix_jsonl import UnixJsonlServer
from vs_sim.api import OsThreads, UnixNetwork
from vs_sim.api.testing import swept

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    RecordProperty = Callable[[str, object], None]

_RUNNER = "host-helper"
# The corpus is replayed in process by `tests/conformance/test_host_helper.py`; a pull request
# replays one scenario of each group through a real process (its wiring), the nightly all.
_SCENARIOS = tuple(
    itertools.chain.from_iterable(
        [group[i] for i in swept(len(group), 1)]
        for group in (
            runner_group(_RUNNER, name) for name in ("handshake", "registry", "host-paths")
        )
    )
)
# vs_decode (the bounded JSON decode lib vs_project reads state through) adds 3 modules;
# vs_project._workspace_tree (GitTracker restores preserved memory by descriptor) adds 1;
# vs_project._nested_repositories (host Git refuses a work tree holding a nested repository)
# adds 1.
_MODULE_CEILING = 335
"""The most modules a helper may load to answer ``hello``; lower it as the helper sheds imports."""
_HELLO = {
    "type": "hello",
    "request_id": "h",
    "deadline_ms": 10000,
    "client_versions": [1, 1],
    "features": ["instances_watch", "paths_complete"],
    "app_version": "e2e",
}


def _command() -> list[str]:
    configured = os.environ.get("VIBESYS_HOST_HELPER_COMMAND")
    if configured:
        return shlex.split(configured)
    return [sys.executable, "-m", "entrypoints.host_helper"]


class _HelperChannel:
    """One channel: a helper process, its stdin and stdout."""

    def __init__(self, env: Mapping[str, str], argv: list[str] | None = None) -> None:
        # lint-waiver: LW-188707 [S603]; the helper must be its own process, as over ssh.
        # > The argv is this checkout's helper or the command the runner is pointed at; an
        # > in-process call would skip the entrypoint's stdio and exit status under test.
        self.process = subprocess.Popen(  # noqa: S603
            argv or _command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
        )
        self._lines: queue.Queue[bytes] = queue.Queue()
        stdout = cast("IO[bytes]", self.process.stdout)
        threading.Thread(target=self._read, args=(stdout,), daemon=True).start()

    def _read(self, stdout: IO[bytes]) -> None:
        for line in stdout:
            self._lines.put(line)
        self._lines.put(b"")

    def send(self, frame: Mapping[str, Any]) -> None:
        stdin = cast("IO[bytes]", self.process.stdin)
        stdin.write(json.dumps(frame).encode() + b"\n")
        stdin.flush()

    def receive(self) -> dict[str, Any]:
        line = self._lines.get(timeout=DEADLOCK_GUARD_S)
        assert line, f"the helper ended before the expected message: {self.finish()}"
        return cast("dict[str, Any]", json.loads(line))

    def close(self) -> None:
        stdin = cast("IO[bytes]", self.process.stdin)
        if not stdin.closed:
            stdin.close()

    def finish(self) -> tuple[int, bytes]:
        """Close stdin (the client going away) and wait; the exit status and stderr."""
        self.close()
        status = self.process.wait(timeout=DEADLOCK_GUARD_S)
        return status, cast("IO[bytes]", self.process.stderr).read()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": str(runtime),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }


@pytest.mark.parametrize("name", _SCENARIOS)
def test_scenario_replays_against_a_real_helper(env: dict[str, str], name: str) -> None:
    channel = _HelperChannel(env)
    run_steps(channel, load_scenario(name))
    assert channel.finish() == (0, b"")


def test_hello_loads_no_engine_and_reports_its_start_up_time(
    env: dict[str, str], record_property: RecordProperty
) -> None:
    started = time.monotonic()
    channel = _HelperChannel(
        env, [sys.executable, "-X", "importtime", "-m", "entrypoints.host_helper"]
    )
    channel.send(_HELLO)
    reply = channel.receive()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    status, stderr = channel.finish()

    record_property("hello_wall_ms", elapsed_ms)
    sys.stdout.write(f"host helper hello: {elapsed_ms} ms (budget {BUDGET_MS} ms)\n")
    assert (reply["type"], reply["protocol_version"], status) == ("hello", 1, 0)
    loaded = loaded_modules(stderr.decode())
    record_property("hello_modules", len(loaded))
    assert "pydantic" in loaded, "importtime reported no modules; the check would be vacuous"
    assert [name for name in loaded if is_heavy(name)] == []
    assert len(loaded) <= _MODULE_CEILING
    # #2351: minting an instance id with ``secrets`` loaded ``secrets``, ``hmac``
    # and ``base64`` on every hello and pushed the count over the ceiling.
    assert {"secrets", "hmac"}.isdisjoint(loaded)


def test_run_connect_makes_the_channel_a_relay_to_the_run(
    env: dict[str, str], tmp_path: Path, socket_dir: Path
) -> None:
    socket_path = socket_dir / "helper.sock"
    instance_id = "0123456789ab"
    parts = build_server_parts(tmp_path / "logs")
    store = FileInstanceStore(instance_root(env, os.getuid()))
    hold = store.hold(instance_id)
    try:
        hold.publish(
            LiveInstanceRecord(
                id=instance_id,
                status=InstanceStatus.SERVING,
                phase=RunPhase(status=RunStatus.RUNNING),
                socket_path=str(socket_path),
                log_path=str(tmp_path / "server.log"),
                project_root=str(tmp_path),
                run_id="run-e2e",
                pid=os.getpid(),
                started_at=0.0,
                hostname="e2e",
                vibesys_version="0+e2e",
            )
        )
        with UnixJsonlServer(
            socket_path, parts.api, subscriptions=None, threads=OsThreads(), network=UnixNetwork()
        ):
            channel = _HelperChannel(env)
            channel.send(_HELLO)
            assert channel.receive()["type"] == "hello"
            channel.send(
                {
                    "type": "run.connect",
                    "request_id": "c",
                    "deadline_ms": 10000,
                    "instance_id": instance_id,
                    "role": "control",
                }
            )
            assert channel.receive() == {
                "type": "run.connect",
                "request_id": "c",
                "run_id": "run-e2e",
                "protocol_version": 1,
            }
            for name in runner_group("relay", "control")[:2]:
                assert len(run_steps(channel, load_scenario(name))) == 1
            assert channel.finish() == (0, b"")
    finally:
        hold.release()
        parts.close()
