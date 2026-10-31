"""A real host helper process over stdio: conformance, start-up, and a relayed run channel.

Each channel is one helper process whose stdin and stdout stand in for an ssh exec
channel. ``VIBESYS_HOST_HELPER_COMMAND`` (a shell-style command line) points the runner
at another helper implementation; the default is ``python -m entrypoints.host_helper``
from this checkout. Start-up time is reported against the 300 ms budget as a test
property and on stdout, never asserted: a test's result must not depend on timing.
The deterministic tests of the helper are ``tests/server/test_host_helper.py`` and
``tests/conformance/test_host_helper.py``.

What a ``hello`` may load is stated by name, in groups of modules (``tests/support/light_imports.py``
for the heavy layers, ``_UNUSED_BY_HELLO`` below for the helper's own): a module of any group
fails the test with the module and its group, whatever else the helper loads. The probe reads
``sys.modules`` in the helper process, so it sees a module loaded through ``importlib`` (a
``lazy_module`` handle, Pydantic's and ``vs_sim``'s on-use submodules) that ``-X importtime`` does
not report. The total stays as a loose backstop for what no group names; it is not a budget a pull
request spends, and it is never raised to make a check pass.
"""

from __future__ import annotations

import ast
import itertools
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from enum import StrEnum
from typing import IO, TYPE_CHECKING, Any, cast

import pytest
from tests.conformance.replay import load_scenario, run_steps, runner_group
from tests.server.support import DEADLOCK_GUARD_S, build_server_parts
from tests.support.light_imports import BUDGET_MS, HEAVY, describe, missing_modules, offenders

from server.host_api import InstanceStatus, LiveInstanceRecord, RunPhase, RunStatus
from server.registry import FileInstanceStore, instance_root
from server.transport.unix_jsonl import UnixJsonlServer
from vs_project.api import create_operator_key
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
_PROBE = """
import os
import sys

started = set(sys.modules)
from entrypoints.host_helper import serve_process

status = serve_process()
sys.stderr.write(repr({"started": sorted(started), "modules": sorted(sys.modules)}) + "\\n")
sys.stderr.flush()
os._exit(status)
"""
"""The helper as ``python -m entrypoints.host_helper`` runs it, then, on stderr, the modules loaded.

``started`` is what the interpreter had before the helper imported anything (the standard library
start-up, the virtual environment's own ``.pth`` hooks), ``modules`` everything at the end. Like
``main``, it leaves the process at once, since a blocked request worker would hold a normal exit.
It imports nothing itself (``json`` would be a module the helper may not have loaded), so the
report is a ``repr``. The probe replaces no code: it calls the function ``main`` calls."""


class Unused(StrEnum):
    """A group of modules the helper loads on a request that needs them, never for ``hello``."""

    EXAMPLES = "the example catalog and project maker, loaded on the first examples request"
    HARNESSES = "the agent library's harness list, loaded on the first harness request"
    READ_ADAPTER = "the registered-read adapter, which serves `vibesys host <read>`, not the helper"
    PROJECT_STATE = "the project's on-use state modules"
    ID_MINTING = "minting an instance id, which needs no secrets or hmac (#2351)"
    LOGS = "the logs reader, loaded on the first logs request"
    RUN_TRANSPORT = "the run log and protobuf transport, loaded when a run publishes or reads a log"
    SLURM = "the Slurm ledger of the launcher's `evals` command"
    SAVED_RUNS = "saved-run import and paging, loaded on a saved-run request"
    RUN_SUMMARY = "the run summary's fold and cache, loaded on the first summary request"


_UNUSED_BY_HELLO: dict[str, tuple[str, ...]] = {
    Unused.EXAMPLES: (
        "entrypoints.examples",
        "entrypoints.example_catalog",
        "entrypoints.host_example_projects",
    ),
    Unused.HARNESSES: ("entrypoints.host_harnesses",),
    Unused.READ_ADAPTER: (
        "entrypoints.host_reads",
        "entrypoints.host_facts",
        "server.host_operation_reads",
    ),
    Unused.PROJECT_STATE: (
        # The fingerprint of a workspace tree, publication of a revision and the retained
        # files a fact import or a semantic restart reads.
        "vs_project._fingerprint",
        "vs_project._revision_publication",
        "vs_project._retained",
    ),
    Unused.ID_MINTING: ("secrets", "hmac"),
    Unused.LOGS: ("vs_logs",),
    Unused.RUN_TRANSPORT: ("vs_runlog", "vs_proto", "google_crc32c", "google.protobuf"),
    Unused.SLURM: ("vs_slurm",),
    Unused.SAVED_RUNS: ("server.host_saved_runs",),
    Unused.RUN_SUMMARY: ("server.run_summary", "server.run_summary_cache"),
}
"""The helper's own groups; ``HEAVY`` names the layers every light host command leaves out."""

_FORBIDDEN: dict[str, tuple[str, ...]] = {**HEAVY, **_UNUSED_BY_HELLO}

_MODULE_CEILING = 330
"""A backstop on the modules the helper loads beyond the interpreter's own start-up (298 when it
was set, on one platform). The groups above are the check; this catches what none names, so it is
loose enough that a platform's own few modules (macOS loads ``_osx_support``) and one pull request's
light module do not reach it. Lower it as the helper sheds imports; never raise it."""
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
def env(socket_dir: Path) -> dict[str, str]:
    """A user environment whose runtime root holds the run sockets.

    The helper dials a run's control socket only inside the user's runtime
    root, so the runtime directory is the shallow ``socket_dir`` and every run
    socket in these tests sits under its ``vibesys`` directory.
    """
    socket_dir.chmod(0o700)
    (socket_dir / "vibesys").mkdir(mode=0o700)
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": str(socket_dir),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }


@pytest.mark.parametrize("name", _SCENARIOS)
def test_scenario_replays_against_a_real_helper(env: dict[str, str], name: str) -> None:
    channel = _HelperChannel(env)
    run_steps(channel, load_scenario(name))
    assert channel.finish() == (0, b"")


def _loaded_by_the_helper(stderr: bytes) -> frozenset[str]:
    """The modules the probe's helper imported, from the report on the last line of ``stderr``."""
    report = ast.literal_eval(stderr.decode().strip().splitlines()[-1])
    return frozenset(report["modules"]) - frozenset(report["started"])


def test_hello_loads_no_engine_and_reports_its_start_up_time(
    env: dict[str, str], record_property: RecordProperty
) -> None:
    started = time.monotonic()
    channel = _HelperChannel(env, [sys.executable, "-c", _PROBE])
    channel.send(_HELLO)
    reply = channel.receive()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    status, stderr = channel.finish()

    record_property("hello_wall_ms", elapsed_ms)
    sys.stdout.write(f"host helper hello: {elapsed_ms} ms (budget {BUDGET_MS} ms)\n")
    assert (reply["type"], reply["protocol_version"], status) == ("hello", 1, 0)
    loaded = _loaded_by_the_helper(stderr)
    record_property("hello_modules", len(loaded))
    assert {"pydantic", "server.host_helper"} <= loaded, (
        "the probe reported no module the helper imports; the check would be vacuous"
    )
    forbidden = offenders(loaded, _FORBIDDEN)
    assert forbidden == {}, (
        f"hello loaded modules it has no reason to: {describe(forbidden)}. Import them where the "
        "request that needs them uses them (a lazy module, or a module of its own), not at load."
    )
    assert len(loaded) <= _MODULE_CEILING


def test_every_module_a_group_names_exists() -> None:
    """A group naming a module that was renamed or removed bans nothing: say which one."""
    missing = missing_modules(_UNUSED_BY_HELLO)
    assert missing == [], f"groups name modules that do not exist (renamed?): {missing}"


def test_run_connect_makes_the_channel_a_relay_to_the_run(
    env: dict[str, str], tmp_path: Path, socket_dir: Path
) -> None:
    socket_path = socket_dir / "vibesys" / "helper.sock"
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
            socket_path,
            parts.api,
            subscriptions=None,
            threads=OsThreads(),
            network=UnixNetwork(),
            # The key the helper subprocess finds in the runtime root it is given.
            key=create_operator_key(env, os.getuid()),
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
