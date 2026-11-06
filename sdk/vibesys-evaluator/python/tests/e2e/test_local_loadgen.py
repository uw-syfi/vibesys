"""A whole sample with the real loadgen: ``benchmark`` in a child process, phases on this machine.

The service is ``KvService`` over HTTP on localhost; loadgen is built from
``sdk/vibesys-evaluator/go`` with go (or taken from
``VIBESYS_EVALUATOR_LOADGEN``), and each phase runs in the core
(``vibesys-evaluator phase pod``). The sample must end with a valid result
record, with the Python generator and with the kv-store example's Go
generator alike; under capture it records a history that the kv-store
example's oracle passes, and fails when the service loses writes; the
accuracy check (``bench.py accuracy``, the core's ``accuracy run``) comes to the
same verdicts. The two generators must write the same request stream.
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

from vibesys_evaluator._private.files import read_results, read_summary
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb

# test-isolation: the dataset file is what the phase pod writes for a generator; the
# PhaseInput and the stream's messages are the schema's.
from vibesys_evaluator.microservices._private import dataset as dataset_file
from vs_evaluator_protocol.api import parse_records, read_measurement

from ..vse_support import kv

if TYPE_CHECKING:
    from collections.abc import Iterator

TESTS = Path(__file__).resolve().parents[1]
LOADGEN_SOURCE = TESTS.parents[1] / "go"
GO_GENERATOR_SOURCE = TESTS.parents[1] / "examples" / "kv-store" / "generator"
ORACLE_SOURCE = TESTS.parents[1] / "examples" / "kv-store" / "oracle"
SCRIPT = Path(__file__).with_name("bench_local.py")
# Bounds that only guard against hangs: a build, a sample of two short phases, a stream read.
BUILD_TIMEOUT_SECS = 600
SAMPLE_TIMEOUT_SECS = 600
STREAM_TIMEOUT_SECS = 120
#: How much of each generator's stream the equality test compares.
STREAM_BYTES = 4 << 20


def _run(
    argv: list[str], *, cwd: Path | None = None, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    # lint-waiver: LW-150404 [S603]; argv is go or this test's own script, with its own arguments.
    return subprocess.run(  # noqa: S603
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _go_build(source: Path, package: str, out: Path) -> Path:
    go = shutil.which("go")
    if go is None:
        pytest.skip(f"go is not on PATH: cannot build {source.name}")
    built = _run(
        [go, "build", "-o", str(out), package],
        cwd=source,
        env=dict(os.environ),
        timeout=BUILD_TIMEOUT_SECS,
    )
    if built.returncode != 0:
        pytest.skip(f"{source.name} does not build here:\n{built.stderr}")
    return out


@pytest.fixture(scope="session")
def built_loadgen(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The loadgen binary: ``VIBESYS_EVALUATOR_LOADGEN``, else built with go."""
    given = os.environ.get("VIBESYS_EVALUATOR_LOADGEN")
    if given:
        return Path(given)
    return _go_build(
        LOADGEN_SOURCE, "./cmd/loadgen", tmp_path_factory.mktemp("loadgen") / "loadgen"
    )


@pytest.fixture(scope="session")
def oracle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The kv-store example's accuracy oracle, built with go."""
    return _go_build(ORACLE_SOURCE, ".", tmp_path_factory.mktemp("oracle") / "kv-oracle")


@pytest.fixture(scope="session")
def go_generator(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The kv-store example's Go generator, built with go."""
    return _go_build(
        GO_GENERATOR_SOURCE, ".", tmp_path_factory.mktemp("generator") / "kv-generator"
    )


# Each answer takes this long, as a real service's would. The open loop's send-lag guard
# allows the larger of 10% of latency p99 and 1 ms; at ~1 ms answers the 1 ms floor is all
# a busy CI runner's scheduling jitter gets, and the [go] sample failed on 2.7 ms of lag
# with nothing wrong. At 50 ms the guard allows about 5 ms and still fails a loadgen that
# is really late.
SERVICE_SECS = 0.05


@contextmanager
def _serving(service: kv.KvService) -> Iterator[str]:
    lock = threading.Lock()
    service_time = threading.Event()  # never set: wait() is the service time

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

        def do_DELETE(self) -> None:
            self._answer()

        def _answer(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {name.lower(): value for name, value in self.headers.items()}
            service_time.wait(SERVICE_SECS)
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


def _sample(
    service: kv.KvService,
    loadgen: Path,
    tmp_path: Path,
    generator: Path | None = None,
    *,
    capture: bool = False,
) -> tuple[int, str, str]:
    """Run one sample (with the Go ``generator``, or the Python one); its status, records, log."""
    kv.seed_service(service, kv.Data())
    output, artifacts = tmp_path / "records.jsonl", tmp_path / "artifacts"
    with _serving(service) as target:
        env = {
            **os.environ,
            "KV_TARGET": target,
            "VIBESYS_EVALUATOR_LOADGEN": str(loadgen),
            "VIBESYS_ARTIFACTS_DIR": str(artifacts),
            "KV_GO_GENERATOR": str(generator) if generator else "",
            "KV_CAPTURE": "1" if capture else "",
            "KV_STORE_SCALE": "test",
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


@pytest.fixture(params=["python", "go"])
def generator(request: pytest.FixtureRequest) -> Path | None:
    """The sample's generator: the Python workload (None) or the Go one."""
    if request.param == "python":
        return None
    return request.getfixturevalue("go_generator")


def test_a_sample_against_a_real_service_reports_a_valid_result(
    built_loadgen: Path, generator: Path | None, tmp_path: Path
) -> None:
    code, records, log = _sample(kv.KvService(), built_loadgen, tmp_path, generator)
    measurement = read_measurement(parse_records(records))
    assert code == 0, records + log
    assert measurement.failure is None
    assert measurement.values is not None
    assert measurement.values["operations_per_second"] > 0
    assert measurement.values["latency_ms.p99"] > 0
    summary = read_summary(tmp_path / "artifacts" / "phase-0" / "summary.binpb")
    assert summary.generator is not None
    assert summary.generator.read >= summary.completed > 0


#: The service, and the oracle's verdict on its history: PASS (0), FAIL (1).
_JUDGED = {"right": (kv.KvService, 0), "losing-writes": (lambda: kv.KvService(lose_writes=True), 1)}


@pytest.mark.parametrize("case", sorted(_JUDGED))
def test_a_captured_history_is_judged_by_the_oracle(
    built_loadgen: Path, oracle: Path, generator: Path | None, tmp_path: Path, case: str
) -> None:
    make, verdict = _JUDGED[case]
    service = make()
    code, records, log = _sample(service, built_loadgen, tmp_path, generator, capture=True)
    assert code == 0, records + log  # a benchmark only measures
    artifacts = tmp_path / "artifacts"
    manifest = phase_pb.HistoryManifest.from_binary((artifacts / "history.binpb").read_bytes())
    assert manifest.phases == 2
    assert [(f.kind, f.after_phase) for f in manifest.faults] == [("pause", 0)]
    results = read_results(artifacts / "phase-0" / "results.binpb")
    assert results
    assert all(r.client is not None for r in results)
    judged = _run(
        [str(oracle), "--history", str(artifacts), "--out", str(tmp_path / "accuracy")],
        env={**os.environ, "KV_STORE_SCALE": "test"},
        timeout=SAMPLE_TIMEOUT_SECS,
    )
    assert judged.returncode == verdict, judged.stdout + judged.stderr


#: The service, the check's faults, and its verdict: PASS (0), FAIL (1), UNKNOWN (2).
_CHECKED = {
    "right": (kv.KvService, "", 0),
    "losing-writes": (lambda: kv.KvService(lose_writes=True), "", 1),
    "a-restart-on-a-local-system": (kv.KvService, "restart", 2),
}


@pytest.mark.parametrize("case", sorted(_CHECKED))
def test_the_accuracy_check_runs_its_phases_and_the_oracle(
    built_loadgen: Path, oracle: Path, tmp_path: Path, case: str
) -> None:
    make, faults, verdict = _CHECKED[case]
    service = make()
    kv.seed_service(service, kv.Data())
    out = tmp_path / "accuracy"
    with _serving(service) as target:
        env = {
            **os.environ,
            "KV_TARGET": target,
            "KV_ACCURACY": faults,
            "KV_ORACLE": str(oracle),
            "KV_STORE_SCALE": "test",
            "VIBESYS_EVALUATOR_LOADGEN": str(built_loadgen),
            "PYTHONPATH": os.pathsep.join([str(TESTS), os.environ.get("PYTHONPATH", "")]),
        }
        done = _run(
            [sys.executable, str(SCRIPT), "accuracy", "--out", str(out), "--seed", "7"],
            env=env,
            timeout=SAMPLE_TIMEOUT_SECS,
        )
    assert done.returncode == verdict, done.stdout + done.stderr
    if faults:
        assert "a local system cannot restart kv" in done.stderr
        return
    manifest = phase_pb.HistoryManifest.from_binary(
        (out / "history" / "history.binpb").read_bytes()
    )
    assert manifest.phases == 2
    assert (out / "history" / "dataset.json").is_file()
    assert (out / "report.txt").is_file(), sorted(p.name for p in out.iterdir())
    results = read_results(out / "history" / "phase-1" / "results.binpb")
    assert results
    assert all(r.client is not None for r in results)


def test_the_accuracy_check_runs_its_own_workload(
    built_loadgen: Path, oracle: Path, tmp_path: Path
) -> None:
    service = kv.KvService()
    kv.seed_service(service, kv.Data())
    out = tmp_path / "accuracy"
    with _serving(service) as target:
        env = {
            **os.environ,
            "KV_TARGET": target,
            "KV_ACCURACY_WORKLOAD": "writes",
            "KV_ORACLE": str(oracle),
            "KV_STORE_SCALE": "test",
            "VIBESYS_EVALUATOR_LOADGEN": str(built_loadgen),
            "PYTHONPATH": os.pathsep.join([str(TESTS), os.environ.get("PYTHONPATH", "")]),
        }
        done = _run(
            [sys.executable, str(SCRIPT), "accuracy", "--out", str(out), "--seed", "7"],
            env=env,
            timeout=SAMPLE_TIMEOUT_SECS,
        )
    assert done.returncode == 0, done.stdout + done.stderr
    # The phases ran the check's workload (bench.py accuracy-workload generate), unjudged.
    for index in range(2):
        summary = read_summary(out / "history" / f"phase-{index}" / "summary.binpb")
        assert list(summary.operation_names) == ["write", "read"]
    assert "operations sent" in done.stderr
    assert "not a measurement" not in done.stderr


def test_the_go_example_is_a_whole_benchmark(
    built_loadgen: Path, go_generator: Path, oracle: Path, tmp_path: Path
) -> None:
    # The kv-store example's Go binary as the task script (go/microservices): it seeds the
    # service itself, runs its phases through the same phase runner, and under capture records
    # a history the oracle passes.
    output, artifacts = tmp_path / "records.jsonl", tmp_path / "artifacts"
    tools = tmp_path / "bin"  # a Go task finds loadgen on PATH, as in its task image
    tools.mkdir()
    (tools / "loadgen").symlink_to(built_loadgen)
    with _serving(kv.KvService()) as target:
        env = {
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}",
            "KV_TARGET": target,
            "KV_CAPTURE": "1",
            "KV_STORE_SCALE": "test",
            "VIBESYS_ARTIFACTS_DIR": str(artifacts),
        }
        done = _run(
            [str(go_generator), "benchmark", "--vs-output", str(output), "--seed", "7"],
            env=env,
            timeout=SAMPLE_TIMEOUT_SECS,
        )
    measurement = read_measurement(parse_records(output.read_text()))
    assert done.returncode == 0, output.read_text() + done.stderr
    assert measurement.values is not None
    assert measurement.values["operations_per_second"] > 0
    judged = _run(
        [str(oracle), "--history", str(artifacts), "--out", str(tmp_path / "accuracy")],
        env={**os.environ, "KV_STORE_SCALE": "test"},
        timeout=SAMPLE_TIMEOUT_SECS,
    )
    assert judged.returncode == 0, judged.stdout + judged.stderr


@pytest.mark.parametrize("case", ["right", "losing-writes"])
def test_the_go_examples_accuracy_check_judges_its_history_in_process(
    built_loadgen: Path, go_generator: Path, tmp_path: Path, case: str
) -> None:
    # microservices.Main's accuracy command: the phases through the same runner, the pause
    # between them, then the oracle's specs (kvcheck) on the history, in the same process.
    out = tmp_path / "accuracy"
    tools = tmp_path / "bin"  # a Go task finds loadgen on PATH, as in its task image
    tools.mkdir()
    (tools / "loadgen").symlink_to(built_loadgen)
    with _serving(kv.KvService(lose_writes=case == "losing-writes")) as target:
        env = {
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}",
            "KV_TARGET": target,
            "KV_STORE_SCALE": "test",
        }
        done = _run(
            [str(go_generator), "accuracy", "--out", str(out), "--seed", "7"],
            env=env,
            timeout=SAMPLE_TIMEOUT_SECS,
        )
    assert done.returncode == (1 if case == "losing-writes" else 0), done.stdout + done.stderr
    manifest = phase_pb.HistoryManifest.from_binary(
        (out / "history" / "history.binpb").read_bytes()
    )
    assert manifest.phases == 2
    assert [(f.kind, f.after_phase) for f in manifest.faults] == [("pause", 0)]
    assert (out / "report.txt").is_file()


def _phase_input(path: Path, *, capture: bool) -> Path:
    """A closed-loop phase of a sample (a later one: its index is not 0)."""
    phase = phase_pb.PhaseInput(
        sample_seed=424242,
        phase_index=2,
        model=pb.LoadModel.CLOSED,
        clients=4,
        secs=1.0,
        warmup_secs=0.5,
        capture=capture,
    )
    path.write_bytes(phase.to_binary())
    return path


def _stream(argv: list[str], env: dict[str, str]) -> bytes:
    """The first ``STREAM_BYTES`` of a generator's stream; then the stream is closed, as loadgen does."""
    # lint-waiver: LW-150407 [S603]; argv is this test's script or the Go generator it built.
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as child:  # noqa: S603
        assert child.stdout is not None
        data = child.stdout.read(STREAM_BYTES)
        child.stdout.close()
        _, stderr = child.communicate(timeout=STREAM_TIMEOUT_SECS)
    assert child.returncode == 0, stderr.decode(errors="replace")
    return data


def _messages(data: bytes) -> list[bytes]:
    """The stream's whole messages after its FileHeader (which names the writer)."""
    frames, position = [], 0
    while True:
        length, shift = 0, 0
        while position < len(data) and data[position] & 0x80:
            length |= (data[position] & 0x7F) << shift
            shift += 7
            position += 1
        if position >= len(data):
            break
        length |= data[position] << shift
        position += 1
        if position + length > len(data):
            break
        frames.append(data[position : position + length])
        position += length
    return frames[1:]


@pytest.mark.parametrize("capture", [False, True])
def test_the_python_and_the_go_generator_write_the_same_stream(
    go_generator: Path,
    tmp_path: Path,
    capture: bool,  # noqa: FBT001  # LW-731405 [FBT001]; a pytest parameter, not an API flag
) -> None:
    phase = _phase_input(tmp_path / "input.binpb", capture=capture)
    dataset = tmp_path / "dataset.json"
    dataset.write_bytes(dataset_file.encode(kv.Data()))
    env = {
        **os.environ,
        "KV_STORE_SCALE": "test",
        "PYTHONPATH": os.pathsep.join([str(TESTS), os.environ.get("PYTHONPATH", "")]),
    }
    streams = []
    for name, command in [("python", [sys.executable, str(SCRIPT)]), ("go", [str(go_generator)])]:
        state = tmp_path / name
        state.mkdir()
        flags = ["--phase-input", str(phase), "--dataset", str(dataset), "--state", str(state)]
        streams.append(_messages(_stream([*command, "generate", *flags], env)))
    python, go = streams
    shared = min(len(python), len(go))
    assert shared > 10_000
    # The same messages (each side writes the fields of a message in its own order).
    assert pb.RequestsHeader.from_binary(python[0]) == pb.RequestsHeader.from_binary(go[0])
    for n in range(1, shared):
        assert pb.PlannedOperation.from_binary(python[n]) == pb.PlannedOperation.from_binary(
            go[n]
        ), f"operation {n - 1}"
