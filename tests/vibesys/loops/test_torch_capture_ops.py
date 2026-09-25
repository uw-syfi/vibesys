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
import os
import socket
import sys
import tempfile
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
# Waiting for sibling processes' traces
#
# Regression for a real MI210 finding (vllm-serve topology, multi-process
# target): capture_runtime.run_capture only waits for the *one* process it
# directly launched. A sibling process that received the same stop signal
# via the process-group broadcast (e.g. vLLM V1's EngineCore worker, the
# one actually doing GPU work) runs its own independent stop/export on its
# own schedule and can still be writing its trace file after run_capture
# has already returned. The first fix waited until the trace set stopped
# changing for one poll interval, which still missed any export slower than
# that; the injection's per-process window markers now say exactly which
# processes still owe a trace.
# ---------------------------------------------------------------------------

_WINDOW_STATES = ("exported", "failed", "exited", "pending")


def _write_window(out_dir: Path, pid: int, window: int, state: str) -> None:
    (out_dir / f".window-{pid}-{window}.started").write_text("")
    if state == "exported":
        _write_trace(out_dir / f"{pid}-{window}.pt.trace.json.gz", n_kernels=1)
    elif state == "failed":
        (out_dir / f".window-{pid}-{window}.failed").write_text("export: boom")


@FAST
@given(states=st.lists(st.sampled_from(_WINDOW_STATES), max_size=8))
def test_unfinished_windows_are_exactly_the_live_unresolved_ones(
    capture_ops: ModuleType, states: list[str]
) -> None:
    """A started window is finished iff it exported, failed, or its process exited."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        alive: set[int] = set()
        expected = []
        for i, state in enumerate(states):
            pid = 1000 + i
            _write_window(out_dir, pid, 1, state)
            if state != "exited":
                alive.add(pid)
            if state == "pending":
                expected.append(f"pid {pid} window 1")

        unfinished = capture_ops.unfinished_windows(out_dir, pid_alive=alive.__contains__)
        assert unfinished == expected


def test_wait_for_started_windows_returns_once_the_last_window_exports(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    """The wait ends on the export itself; grace_s is only a failure bound."""
    _write_window(tmp_path, 4242, 1, "pending")
    polling = threading.Event()

    def pid_alive(_pid: int) -> bool:
        polling.set()  # the wait has observed the window as pending
        return True

    def export_later() -> None:
        polling.wait()
        _write_trace(tmp_path / "4242-1.pt.trace.json.gz", n_kernels=2)

    writer = threading.Thread(target=export_later)
    writer.start()
    try:
        assert capture_ops.wait_for_started_windows(tmp_path, grace_s=60, pid_alive=pid_alive) == []
    finally:
        writer.join()
    assert capture_ops.discover_traces(tmp_path) == [tmp_path / "4242-1.pt.trace.json.gz"]


def test_wait_for_started_windows_reports_what_is_still_missing(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    _write_window(tmp_path, 4242, 3, "pending")
    unfinished = capture_ops.wait_for_started_windows(
        tmp_path, grace_s=0, pid_alive=lambda _pid: True
    )
    assert unfinished == ["pid 4242 window 3"]


def test_wait_for_additional_traces_zero_grace_is_a_no_op(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    """The inject=False fallback (no handshake with a foreign profiler) still honors grace 0."""
    t0 = time.monotonic()
    capture_ops.wait_for_additional_traces(tmp_path, grace_s=0.0)
    assert time.monotonic() - t0 < 5.0


def test_profile_ops_waits_for_a_sibling_whose_export_outlasts_the_launched_process(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression with real armed processes.

    The launched process (like the vllm-serve API server) exits first; its
    sibling (like EngineCore) is still recording and exports only after
    that, slowly (the fake export takes 2.5s, longer than the old
    "unchanged for one 1s poll" heuristic tolerated). Ordering comes from
    state, not sleeps: the parent exits only once both window markers
    exist, and the sibling exits only once the parent is gone.
    """
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(profiles_dir))

    sibling_script = tmp_path / "sibling.py"
    sibling_script.write_text(
        textwrap.dedent(
            """
            import os, sys, time
            import torch  # noqa: F401  (arms this process's injection)

            parent = int(sys.argv[1])
            while True:  # exit (and so export) only after the parent is gone
                try:
                    os.kill(parent, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            """
        )
    )
    script = tmp_path / "workload.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import glob, os, subprocess, sys, time
            import torch  # noqa: F401  (arms this process's own injection too)

            env = dict(os.environ, FAKE_TORCH_EXPORT_DELAY_S="2.5")
            subprocess.Popen([sys.executable, {str(sibling_script)!r}, str(os.getpid())], env=env)
            out_dir = os.environ["VIBESYS_TORCH_PROFILE_OUT_DIR"]
            while len(glob.glob(os.path.join(out_dir, ".window-*.started"))) < 2:
                time.sleep(0.01)
            """
        )
    )

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {script}",
        env={"PYTHONPATH": str(fake_torch)},
        delay_s=0.0,
        timeout_s=60,
        grace_s=60,
    )

    assert ": ok" in output, output
    assert "had not exported" not in output, output
    captures = list(profiles_dir.iterdir())
    assert len(captures) == 1
    manifest = json.loads((captures[0] / "manifest.json").read_text())
    assert len(manifest["trace_files"]) == 2, manifest


# ---------------------------------------------------------------------------
# _build_capture_env
# ---------------------------------------------------------------------------


def test_build_capture_env_prepends_inject_dir_to_pythonpath(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    env = capture_ops._build_capture_env(  # noqa: SLF001  # LW-920389; this reaches a sibling profiler module's underscore-prefixed name directly; these standalone scripts have no public API surface to expose it through
        user_env={"PYTHONPATH": "/existing/path"},
        out_dir=tmp_path,
        delay_s=1.5,
        duration_s=None,
        record_shapes=True,
    )
    parts = env["PYTHONPATH"].split(":")
    assert parts[0] == str(capture_ops._INJECT_DIR)  # noqa: SLF001  # LW-920390; this reaches a sibling profiler module's underscore-prefixed name directly; these standalone scripts have no public API surface to expose it through
    assert "/existing/path" in parts
    assert env["VIBESYS_TORCH_PROFILE"] == "1"
    assert env["VIBESYS_TORCH_PROFILE_DELAY_S"] == "1.5"
    assert "VIBESYS_TORCH_PROFILE_DURATION_S" not in env
    assert env["VIBESYS_TORCH_PROFILE_RECORD_SHAPES"] == "1"


def test_build_capture_env_sets_duration_when_given(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    env = capture_ops._build_capture_env(  # noqa: SLF001  # LW-920391; this reaches a sibling profiler module's underscore-prefixed name directly; these standalone scripts have no public API surface to expose it through
        user_env=None, out_dir=tmp_path, delay_s=0.0, duration_s=30.0, record_shapes=False
    )
    assert env["VIBESYS_TORCH_PROFILE_DURATION_S"] == "30.0"
    assert env["VIBESYS_TORCH_PROFILE_RECORD_SHAPES"] == "0"


def test_build_capture_env_user_env_wins_on_conflict(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    env = capture_ops._build_capture_env(  # noqa: SLF001  # LW-920392; this reaches a sibling profiler module's underscore-prefixed name directly; these standalone scripts have no public API surface to expose it through
        user_env={"VIBESYS_TORCH_PROFILE": "0"},
        out_dir=tmp_path,
        delay_s=0.0,
        duration_s=None,
        record_shapes=True,
    )
    assert env["VIBESYS_TORCH_PROFILE"] == "0"


# ---------------------------------------------------------------------------
# _build_capture_env(inject=False): regression test for a real MI210 SIGSEGV
# (target_rc=139, empty trace) hit when a command that already opens its own
# torch.profiler.profile() session (e.g. vLLM's profiler_config +
# start_profile()/stop_profile()) was also profiled by this module's own
# signal-based injection -- two independent profiler sessions in one process
# crash the CUPTI/roctracer/kineto backend outright, not catchably. Fixed by
# threading an inject=False path all the way through profile_ops that skips
# arming sitecustomize.py entirely while still exporting
# VIBESYS_TORCH_PROFILE_OUT_DIR for the command's own profiler to use.
#
# This fails against pre-fix code (git show HEAD~N -- the ``inject`` keyword
# did not exist there at all): calling _build_capture_env(inject=False, ...)
# raises TypeError('_build_capture_env() got an unexpected keyword argument
# \'inject\''), not the assertions below.
# ---------------------------------------------------------------------------


def test_build_capture_env_inject_false_skips_injection_but_keeps_out_dir(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    env = capture_ops._build_capture_env(  # noqa: SLF001  # LW-920393; this reaches a sibling profiler module's underscore-prefixed name directly; these standalone scripts have no public API surface to expose it through
        user_env=None,
        out_dir=tmp_path,
        delay_s=5.0,
        duration_s=30.0,
        record_shapes=True,
        inject=False,
    )
    assert env["VIBESYS_TORCH_PROFILE_OUT_DIR"] == str(tmp_path)
    assert "VIBESYS_TORCH_PROFILE" not in env
    assert "VIBESYS_TORCH_PROFILE_DELAY_S" not in env
    assert "VIBESYS_TORCH_PROFILE_DURATION_S" not in env
    assert "VIBESYS_TORCH_PROFILE_RECORD_SHAPES" not in env
    assert "PYTHONPATH" not in env


def test_build_capture_env_inject_false_preserves_user_pythonpath(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    env = capture_ops._build_capture_env(  # noqa: SLF001  # LW-920394; this reaches a sibling profiler module's underscore-prefixed name directly; these standalone scripts have no public API surface to expose it through
        user_env={"PYTHONPATH": "/existing/path"},
        out_dir=tmp_path,
        delay_s=0.0,
        duration_s=None,
        record_shapes=True,
        inject=False,
    )
    # inject=False never prepends _INJECT_DIR: the caller's PYTHONPATH must
    # pass through completely untouched, since sitecustomize.py must never
    # load into this process at all.
    assert env["PYTHONPATH"] == "/existing/path"


@given(
    delay_s=st.floats(min_value=0.0, max_value=60.0, allow_nan=False),
    duration_s=st.one_of(st.none(), st.floats(min_value=0.0, max_value=60.0, allow_nan=False)),
    record_shapes=st.booleans(),
    inject=st.booleans(),
)
@FAST
def test_build_capture_env_injection_keys_gated_exactly_by_inject(  # noqa: PLR0913  # LW-910309; this function's parameters mirror an external tool's CLI/API surface and are not grouped further
    capture_ops: ModuleType,
    tmp_path: Path,
    delay_s: float,
    duration_s: float | None,
    record_shapes: bool,  # noqa: FBT001  # LW-920395; tracked migration debt from the pre-manifest ratchet scheme
    inject: bool,  # noqa: FBT001  # LW-920396; tracked migration debt from the pre-manifest ratchet scheme
) -> None:
    """Property: every VIBESYS_TORCH_PROFILE* injection key, and the
    PYTHONPATH prepend, appear if and only if inject=True -- regardless of
    delay_s/duration_s/record_shapes -- while VIBESYS_TORCH_PROFILE_OUT_DIR
    is set unconditionally either way."""
    env = capture_ops._build_capture_env(  # noqa: SLF001  # LW-920397; this reaches a sibling profiler module's underscore-prefixed name directly; these standalone scripts have no public API surface to expose it through
        user_env=None,
        out_dir=tmp_path,
        delay_s=delay_s,
        duration_s=duration_s,
        record_shapes=record_shapes,
        inject=inject,
    )
    assert env["VIBESYS_TORCH_PROFILE_OUT_DIR"] == str(tmp_path)
    injection_keys = {
        "VIBESYS_TORCH_PROFILE",
        "VIBESYS_TORCH_PROFILE_DELAY_S",
        "VIBESYS_TORCH_PROFILE_RECORD_SHAPES",
        "PYTHONPATH",
    }
    present = injection_keys & env.keys()
    if inject:
        assert present == injection_keys
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(capture_ops._INJECT_DIR)  # noqa: SLF001  # LW-920398; this reaches a sibling profiler module's underscore-prefixed name directly; these standalone scripts have no public API surface to expose it through
    else:
        assert present == set()
        assert ("VIBESYS_TORCH_PROFILE_DURATION_S" in env) is False


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
    # Exits once its window is exported, not after a guessed sleep.
    script.write_text(
        "import torch\nfrom torch._log import wait_for_event\n"
        "wait_for_event('profile.export_chrome_trace')\n"
    )

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {script}",
        env={"PYTHONPATH": str(fake_torch), "FAKE_TORCH_CALL_LOG": str(tmp_path / "calls.log")},
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


def test_profile_ops_inject_false_finds_a_trace_the_command_writes_itself(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the real MI210 SIGSEGV: with inject=False,
    sitecustomize.py must never be armed (no VIBESYS_TORCH_PROFILE=1, no
    PYTHONPATH prepend of the inject dir) even though profile_ops still
    finds and certifies a trace -- one the command wrote itself, simulating
    a serving engine's own profiler_config + start_profile()/stop_profile()
    path (see resources/skills/serving-systems/references/engines/
    vllm-profiling.md). Uses the fake torch package directly from within
    the workload script (not via sitecustomize's injection) to write a
    real, readable *.pt.trace.json.gz that this module's own discovery
    then picks up."""
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(profiles_dir))
    call_log = tmp_path / "calls.log"

    script = tmp_path / "native_profile_workload.py"
    script.write_text(
        textwrap.dedent(
            """
            import gzip
            import os
            import shutil

            import torch

            out_dir = os.environ["VIBESYS_TORCH_PROFILE_OUT_DIR"]
            os.makedirs(out_dir, exist_ok=True)

            prof = torch.profiler.profile(
                activities=[torch.profiler.ProfilerActivity.CPU, torch.profiler.ProfilerActivity.CUDA],
                record_shapes=True,
            )
            prof.start()
            prof.stop()
            raw_path = os.path.join(out_dir, f"{os.getpid()}-native.pt.trace.json")
            gz_path = raw_path + ".gz"
            prof.export_chrome_trace(raw_path)
            with open(raw_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            """
        )
    )

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {script}",
        env={"PYTHONPATH": str(fake_torch), "FAKE_TORCH_CALL_LOG": str(call_log)},
        inject=False,
        timeout_s=20,
        grace_s=5,
    )

    assert ": ok" in output
    assert "primary trace:" in output
    assert "--- certify ---" in output

    # sitecustomize.py was never armed: exactly one profile.start (the
    # script's own explicit call), not two -- proving inject=False did not
    # layer a second, competing torch.profiler session on top of the
    # command's own.
    events = call_log.read_text().splitlines() if call_log.exists() else []
    starts = [line for line in events if line.endswith("\tprofile.start")]
    assert len(starts) == 1

    captures2 = list(profiles_dir.iterdir())
    assert len(captures2) == 1
    manifest2 = json.loads((captures2[0] / "manifest.json").read_text())
    assert manifest2["meta"]["inject"] is False
    assert manifest2["primary_trace"] is not None
    assert (captures2[0] / manifest2["primary_trace"]).is_file()


def test_profile_ops_reports_no_traces_when_no_gpu(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_torch = write_fake_torch(tmp_path / "fake_torch")
    profiles_dir = tmp_path / "profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(profiles_dir))

    script = tmp_path / "workload.py"
    script.write_text(
        "import torch\nfrom torch._log import wait_for_event\nwait_for_event('cuda.is_available')\n"
    )

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {script}",
        env={
            "PYTHONPATH": str(fake_torch),
            "FAKE_TORCH_GPU": "0",
            "FAKE_TORCH_CALL_LOG": str(tmp_path / "calls.log"),
        },
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
    call_log = tmp_path / "calls.log"
    # The first request makes the server import torch (arming the
    # injection); the load then waits for recording to start before the
    # rest, so the stop signal can never beat the window's start.
    load_script = tmp_path / "load.py"
    load_script.write_text(
        textwrap.dedent(
            f"""
            import time
            import urllib.request as u

            url = "http://127.0.0.1:{port}/"
            u.urlopen(url, timeout=10)
            log = {str(call_log)!r}
            while True:
                try:
                    with open(log, encoding="utf-8") as handle:
                        if any(line.rstrip().endswith("profile.start") for line in handle):
                            break
                except FileNotFoundError:
                    pass
                time.sleep(0.01)
            for _ in range(2):
                u.urlopen(url, timeout=10)
            """
        )
    )

    output = capture_ops.profile_ops(
        command=f"{sys.executable} {server_script} {port}",
        env={"PYTHONPATH": str(fake_torch), "FAKE_TORCH_CALL_LOG": str(call_log)},
        ready_command=(
            f'{sys.executable} -c "import urllib.request as u; '
            f'u.urlopen(\\"http://127.0.0.1:{port}/\\", timeout=2)"'
        ),
        ready_timeout_s=10,
        load_command=f"{sys.executable} {load_script}",
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
    # capture_runtime.run_capture records these generically for every
    # profile_* tool, not just rocprof's -- profile_ops inherits them "for
    # free" through the same lifecycle, even though its own op-level
    # analyses don't slice by them yet (see capture_ops.py's profile_ops
    # docstring).
    assert manifest["capture_start"]["clock_monotonic_ns"] > 0
    assert (
        manifest["capture_end"]["clock_monotonic_ns"]
        >= manifest["capture_start"]["clock_monotonic_ns"]
    )
    assert manifest["load_window"]["start"]["clock_monotonic_ns"] > 0
    assert (
        manifest["load_window"]["end"]["clock_monotonic_ns"]
        >= manifest["load_window"]["start"]["clock_monotonic_ns"]
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
