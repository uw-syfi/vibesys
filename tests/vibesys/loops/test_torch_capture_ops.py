"""Tests for ``resources/profilers/torch/capture_ops.py`` (``profile_ops``).

``capture_ops.py`` is a standalone script (loaded by file path, mirroring
``tests/vibesys/loops/test_profiler_mcp.py``/``test_capture_runtime.py``),
not imported as a package. Trace discovery and primary-trace selection are
exercised as pure unit/property tests against small synthetic Kineto-shaped
trace files (fast, no subprocess). ``profile_ops`` itself is exercised end to
end via real subprocesses using the fake ``torch`` package from
``torch_inject_fixtures.py``, including a server+load+SIGINT lifecycle with
a small stdlib HTTP server that imports torch per request.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.loops.torch_inject_fixtures import write_fake_torch

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).resolve().parents[3]
_TORCH_DIR = _REPO / "resources" / "profilers" / "torch"

FAST = settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
# For property tests whose body sleeps real wall-clock time (bounded waits):
# fewer examples keeps the suite fast without weakening the property itself.
FAST_SHORT = settings(
    max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    parent = str(path.parent)
    inserted = parent not in sys.path
    if inserted:
        sys.path.insert(0, parent)
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(parent)
    return module


@pytest.fixture(scope="module")
def capture_ops() -> ModuleType:
    return _load_module("_capture_ops", _TORCH_DIR / "capture_ops.py")


def _write_trace(path: Path, *, n_kernels: int, readable: bool = True) -> None:
    if not readable:
        path.write_bytes(b"not gzip json at all")
        return
    events = [
        {"ph": "X", "cat": "kernel", "name": f"k{i}", "ts": i * 10, "dur": 5, "args": {}}
        for i in range(n_kernels)
    ]
    data = {"traceEvents": events, "deviceProperties": []}
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(data, handle)


# ---------------------------------------------------------------------------
# discover_traces / pick_primary_trace
# ---------------------------------------------------------------------------


def test_discover_traces_finds_only_pt_trace_files(capture_ops: ModuleType, tmp_path: Path) -> None:
    _write_trace(tmp_path / "111.pt.trace.json.gz", n_kernels=1)
    sub = tmp_path / "nested"
    sub.mkdir()
    _write_trace(sub / "222.pt.trace.json.gz", n_kernels=2)
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "target.log").write_text("hello")

    found = capture_ops.discover_traces(tmp_path)
    assert [p.name for p in found] == ["111.pt.trace.json.gz", "222.pt.trace.json.gz"]


def test_pick_primary_trace_empty_list_is_none(capture_ops: ModuleType) -> None:
    assert capture_ops.pick_primary_trace([]) is None


def test_pick_primary_trace_skips_unreadable_files(capture_ops: ModuleType, tmp_path: Path) -> None:
    bad = tmp_path / "1.pt.trace.json.gz"
    good = tmp_path / "2.pt.trace.json.gz"
    _write_trace(bad, n_kernels=0, readable=False)
    _write_trace(good, n_kernels=3)
    assert capture_ops.pick_primary_trace([bad, good]) == good


@FAST
@given(
    counts=st.lists(st.integers(min_value=0, max_value=6), min_size=1, max_size=6, unique=False),
)
def test_pick_primary_trace_is_the_max_kernel_trace(
    capture_ops: ModuleType, tmp_path: Path, counts: list[int]
) -> None:
    """For any set of per-pid traces, the primary is whichever has the most kernels.

    Regression shape for the intended real-world case: a multi-process
    capture where only one process actually did GPU work.
    """
    case_dir = tmp_path / f"case-{len(counts)}-{sum(counts)}-{'-'.join(map(str, counts))}"
    case_dir.mkdir(exist_ok=True)
    paths = []
    for i, count in enumerate(counts):
        path = case_dir / f"{1000 + i}.pt.trace.json.gz"
        _write_trace(path, n_kernels=count)
        paths.append(path)

    primary = capture_ops.pick_primary_trace(paths)
    max_count = max(counts)
    expected_candidates = [p for p, c in zip(paths, counts, strict=True) if c == max_count]
    assert primary in expected_candidates
    # Deterministic: the lexicographically smallest path among the tied max.
    assert primary == min(expected_candidates)

    # Order independence: shuffling the input must not change the winner.
    assert capture_ops.pick_primary_trace(list(reversed(paths))) == primary


# ---------------------------------------------------------------------------
# wait_for_additional_traces
#
# Regression for a real MI210 finding (vllm-serve topology, multi-process
# target): capture_runtime.run_capture only waits for the *one* process it
# directly launched. A sibling process that received the same stop signal
# via the process-group broadcast (e.g. vLLM V1's EngineCore worker, the
# one actually doing GPU work) runs its own independent stop/export on its
# own schedule and can still be writing its trace file after run_capture
# has already returned. Without a bounded wait, primary-trace selection ran
# immediately and silently missed it, falling back to a near-empty
# driver-process trace with no error.
# ---------------------------------------------------------------------------


def test_wait_for_additional_traces_returns_immediately_with_nothing_pending(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    t0 = time.monotonic()
    capture_ops.wait_for_additional_traces(tmp_path, grace_s=5.0)
    assert time.monotonic() - t0 < 2.0


def test_wait_for_additional_traces_zero_grace_is_a_no_op(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    t0 = time.monotonic()
    capture_ops.wait_for_additional_traces(tmp_path, grace_s=0.0)
    assert time.monotonic() - t0 < 0.1


@FAST_SHORT
@given(
    delay_s=st.floats(min_value=0.0, max_value=0.4, allow_nan=False),
    n_kernels=st.integers(min_value=0, max_value=5),
)
def test_wait_for_additional_traces_eventually_sees_a_delayed_file(
    capture_ops: ModuleType, tmp_path: Path, delay_s: float, n_kernels: int
) -> None:
    """For any arrival delay comfortably inside grace_s, the delayed file is
    visible via discover_traces once the wait returns -- regardless of how
    late (within budget) a sibling process finishes exporting.
    """
    case_dir = tmp_path / f"case-{delay_s}-{n_kernels}"
    case_dir.mkdir()
    path = case_dir / "555555.pt.trace.json.gz"

    def _delayed_writer() -> None:
        time.sleep(delay_s)
        _write_trace(path, n_kernels=n_kernels)

    writer = threading.Thread(target=_delayed_writer)
    writer.start()
    try:
        capture_ops.wait_for_additional_traces(case_dir, grace_s=1.5)
        assert path.is_file()
        assert capture_ops.discover_traces(case_dir) == [path]
    finally:
        writer.join(timeout=5.0)


def test_profile_ops_finds_a_sibling_trace_that_exports_after_the_tracked_process_exits(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression: ``command`` exits almost immediately (like the
    vllm-serve API server), but spawns a detached sibling process that keeps
    running and writes its own trace ~0.6s later (like EngineCore's own,
    independently-scheduled stop/export). ``profile_ops`` must still find
    it, because a multi-process target's traces are not all produced by the
    time the one directly-launched process exits.
    """
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(profiles_dir))

    sibling_script = tmp_path / "sibling.py"
    sibling_script.write_text(
        textwrap.dedent(
            """
            import gzip, json, os, sys, time

            time.sleep(0.6)
            out_dir = os.environ["VIBESYS_TORCH_PROFILE_OUT_DIR"]
            events = [
                {"ph": "X", "cat": "kernel", "name": "late_sibling_kernel", "ts": 0, "dur": 5, "args": {}}
            ]
            data = {"traceEvents": events, "deviceProperties": []}
            path = os.path.join(out_dir, "999999999.pt.trace.json.gz")
            with gzip.open(path, "wt", encoding="utf-8") as f:
                json.dump(data, f)
            """
        )
    )
    script = tmp_path / "workload.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys
            import torch  # noqa: F401  (arms this process's own injection too)

            subprocess.Popen([sys.executable, {str(sibling_script)!r}])
            """
        )
    )

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {script}",
        env={"PYTHONPATH": str(fake_torch)},
        delay_s=0.0,
        duration_s=0.05,
        timeout_s=20,
        grace_s=3,
    )

    assert ": ok" in output, output
    captures = list(profiles_dir.iterdir())
    assert len(captures) == 1
    manifest = json.loads((captures[0] / "manifest.json").read_text())
    assert "999999999.pt.trace.json.gz" in manifest["trace_files"], manifest


# ---------------------------------------------------------------------------
# _build_capture_env
# ---------------------------------------------------------------------------


def test_build_capture_env_prepends_inject_dir_to_pythonpath(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    env = capture_ops._build_capture_env(  # noqa: SLF001
        user_env={"PYTHONPATH": "/existing/path"},
        out_dir=tmp_path,
        delay_s=1.5,
        duration_s=None,
        record_shapes=True,
    )
    parts = env["PYTHONPATH"].split(":")
    assert parts[0] == str(capture_ops._INJECT_DIR)  # noqa: SLF001
    assert "/existing/path" in parts
    assert env["VIBESYS_TORCH_PROFILE"] == "1"
    assert env["VIBESYS_TORCH_PROFILE_DELAY_S"] == "1.5"
    assert "VIBESYS_TORCH_PROFILE_DURATION_S" not in env
    assert env["VIBESYS_TORCH_PROFILE_RECORD_SHAPES"] == "1"


def test_build_capture_env_sets_duration_when_given(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    env = capture_ops._build_capture_env(  # noqa: SLF001
        user_env=None, out_dir=tmp_path, delay_s=0.0, duration_s=30.0, record_shapes=False
    )
    assert env["VIBESYS_TORCH_PROFILE_DURATION_S"] == "30.0"
    assert env["VIBESYS_TORCH_PROFILE_RECORD_SHAPES"] == "0"


def test_build_capture_env_user_env_wins_on_conflict(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    env = capture_ops._build_capture_env(  # noqa: SLF001
        user_env={"VIBESYS_TORCH_PROFILE": "0"},
        out_dir=tmp_path,
        delay_s=0.0,
        duration_s=None,
        record_shapes=True,
    )
    assert env["VIBESYS_TORCH_PROFILE"] == "0"


# ---------------------------------------------------------------------------
# profile_ops end to end (bounded script)
# ---------------------------------------------------------------------------


def test_profile_ops_bounded_script_produces_certified_summary(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(profiles_dir))

    script = tmp_path / "workload.py"
    script.write_text("import torch\nimport time\ntime.sleep(0.5)\n")

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {script}",
        env={"PYTHONPATH": str(fake_torch)},
        delay_s=0.0,
        duration_s=0.2,
        record_shapes=True,
        timeout_s=20,
        grace_s=5,
    )

    assert "capture ops-" in output
    assert ": ok" in output
    assert "primary trace:" in output
    assert "--- certify ---" in output
    assert "--- top kernels ---" in output
    assert "--- gemm shapes ---" in output

    captures = list(profiles_dir.iterdir())
    assert len(captures) == 1
    manifest = json.loads((captures[0] / "manifest.json").read_text())
    assert manifest["primary_trace"] is not None
    assert manifest["trace_files"] == [manifest["primary_trace"]]
    assert (captures[0] / manifest["primary_trace"]).is_file()


def test_profile_ops_reports_no_traces_when_no_gpu(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(profiles_dir))

    script = tmp_path / "workload.py"
    script.write_text("import torch\nimport time\ntime.sleep(0.2)\n")

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {script}",
        env={"PYTHONPATH": str(fake_torch), "FAKE_TORCH_GPU": "0"},
        delay_s=0.0,
        duration_s=0.1,
        timeout_s=20,
        grace_s=5,
    )
    assert "no *.pt.trace.json.gz trace files were produced" in output


# ---------------------------------------------------------------------------
# profile_ops: server + load + SIGINT lifecycle
# ---------------------------------------------------------------------------

_SERVER_SOURCE = textwrap.dedent(
    """
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = int(sys.argv[1])


    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            import torch  # noqa: F401  (arms the injection for this process)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass


    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    """
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_profile_ops_server_load_sigint_lifecycle(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(profiles_dir))

    server_script = tmp_path / "server.py"
    server_script.write_text(_SERVER_SOURCE)
    port = _free_port()

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {server_script} {port}",
        env={"PYTHONPATH": str(fake_torch)},
        ready_command=(
            f'{sys.executable} -c "import urllib.request as u; '
            f'u.urlopen(\\"http://127.0.0.1:{port}/\\", timeout=2)"'
        ),
        ready_timeout_s=10,
        load_command=(
            f'{sys.executable} -c "import urllib.request as u\n'
            f"for _ in range(3):\n"
            f'    u.urlopen(\\"http://127.0.0.1:{port}/\\", timeout=2)"'
        ),
        stop_signal="SIGINT",
        grace_s=10,
        timeout_s=30,
        delay_s=0.0,
    )

    assert ": ok" in output, output
    assert "primary trace:" in output
    assert "--- certify ---" in output

    captures = list(profiles_dir.iterdir())
    assert len(captures) == 1
    manifest = json.loads((captures[0] / "manifest.json").read_text())
    assert manifest["primary_trace"] is not None
    assert manifest["status"] == "ok"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
