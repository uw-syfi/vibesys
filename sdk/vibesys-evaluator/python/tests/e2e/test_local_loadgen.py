"""A whole sample with the real loadgen: ``benchmark`` in a child process, phases on this machine.

The service is ``KvService`` over HTTP on localhost; loadgen is built from
``sdk/vibesys-evaluator/loadgen`` with go (or taken from
``VIBESYS_EVALUATOR_LOADGEN``). The sample must end with a valid result
record, and a service that loses writes must fail it by its checks.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_evaluator_protocol.api import parse_records, read_measurement

from ..vse_support import kv

if TYPE_CHECKING:
    from collections.abc import Iterator

TESTS = Path(__file__).resolve().parents[1]
LOADGEN_SOURCE = TESTS.parents[1] / "loadgen"
SCRIPT = Path(__file__).with_name("bench_local.py")
# Bounds that only guard against hangs: a build, and a sample of two short phases (and a retry).
BUILD_TIMEOUT_SECS = 600
SAMPLE_TIMEOUT_SECS = 600


def _run(
    argv: list[str], *, cwd: Path | None = None, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    # lint-waiver: LW-150404 [S603]; argv is go or this test's own script, with its own arguments.
    return subprocess.run(  # noqa: S603
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


@pytest.fixture(scope="session")
def built_loadgen(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The loadgen binary: ``VIBESYS_EVALUATOR_LOADGEN``, else built with go."""
    given = os.environ.get("VIBESYS_EVALUATOR_LOADGEN")
    if given:
        return Path(given)
    go = shutil.which("go")
    if go is None:
        pytest.skip("go is not on PATH: cannot build loadgen (or set VIBESYS_EVALUATOR_LOADGEN)")
    binary = tmp_path_factory.mktemp("loadgen") / "loadgen"
    built = _run(
        [go, "build", "-o", str(binary), "./cmd/loadgen"],
        cwd=LOADGEN_SOURCE,
        env=dict(os.environ),
        timeout=BUILD_TIMEOUT_SECS,
    )
    if built.returncode != 0:
        pytest.skip(f"loadgen does not build here (set VIBESYS_EVALUATOR_LOADGEN):\n{built.stderr}")
    return binary


@contextmanager
def _serving(service: kv.KvService) -> Iterator[str]:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            # Headers and body go out in two writes; without this, Nagle's algorithm and the
            # client's delayed ACK hold every response ~40 ms.
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def do_GET(self) -> None:
            self._answer()

        def do_POST(self) -> None:
            self._answer()

        def do_PUT(self) -> None:
            self._answer()

        def _answer(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {name.lower(): value for name, value in self.headers.items()}
            with lock:
                status, response = service.handle(self.command, self.path, headers, body)
            self.send_response(status)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # LW-150405 [A002]; the base class names it so
            del format, args  # a test's server is quiet

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _sample(service: kv.KvService, loadgen: Path, tmp_path: Path) -> tuple[int, str, str]:
    """Run one sample; its exit status, its records and its log."""
    kv.seed_service(service, kv.Data())
    output, artifacts = tmp_path / "records.jsonl", tmp_path / "artifacts"
    with _serving(service) as target:
        env = {
            **os.environ,
            "KV_TARGET": target,
            "VIBESYS_EVALUATOR_LOADGEN": str(loadgen),
            "VIBESYS_ARTIFACTS_DIR": str(artifacts),
            "PYTHONPATH": os.pathsep.join([str(TESTS), os.environ.get("PYTHONPATH", "")]),
        }
        done = _run(
            [
                sys.executable,
                str(SCRIPT),
                "benchmark",
                "--project-root",
                str(tmp_path),
                "--vs-output",
                str(output),
                "--seed",
                "7",
            ],
            env=env,
            timeout=SAMPLE_TIMEOUT_SECS,
        )
    return done.returncode, output.read_text(), done.stderr


def test_a_sample_against_a_real_service_reports_a_valid_result(
    built_loadgen: Path, tmp_path: Path
) -> None:
    code, records, log = _sample(kv.KvService(), built_loadgen, tmp_path)
    measurement = read_measurement(parse_records(records))
    assert code == 0, records + log
    assert measurement.failure is None
    assert measurement.values is not None
    assert measurement.values["operations_per_second"] > 0
    assert measurement.values["latency_ms.p99"] > 0
    assert (tmp_path / "artifacts" / "phase-0" / "summary.binpb").is_file()


def test_lost_writes_fail_a_real_sample_by_its_checks(built_loadgen: Path, tmp_path: Path) -> None:
    code, records, log = _sample(kv.KvService(lose_writes=True), built_loadgen, tmp_path)
    failure = read_measurement(parse_records(records)).failure
    assert code == 1, records + log
    assert failure is not None
    assert "failed checks" in failure
