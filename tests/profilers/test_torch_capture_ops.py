"""``capture_ops`` (``resources/profilers/torch/capture_ops.py``): ``profile_ops`` and its helpers.

Guarantees under test:

* Trace discovery finds exactly the ``*.pt.trace.json.gz`` files under a capture; the primary
  trace is the one with the most GPU kernels, unreadable files are skipped, and none is
  ``None``.
* A window is unfinished exactly while its process is alive and has neither exported nor
  failed; ``wait_for_started_windows`` returns once every window is resolved and otherwise
  names what is missing; a zero grace is a no-op.
* The capture environment arms the injection if and only if ``inject`` is set, always keeps
  the caller's ``PYTHONPATH`` and lets the caller's env win on conflicts.
* ``profile_ops`` on a bounded script or a server under load yields a certified summary and
  a manifest naming the primary trace and the load window; with a sibling process still
  exporting after the launched one exits it waits for the export; with ``inject=False`` it
  arms nothing and still finds a trace the command wrote; with no GPU it fails with a typed
  ``no_trace`` error.

Processes are scripted on a virtual clock (``tests/support/profiler_process_world.py``); the
real injection's behaviour in real processes is checked in
``tests/e2e/test_torch_inject_processes_e2e.py``.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.profiler_process_world import (
    ScriptedHost,
    always,
    exits,
    injected_process,
)

from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from tests.support.profiler_process_world import FakeProcess

_REPO = Path(__file__).resolve().parents[2]
_TORCH_DIR = _REPO / "resources" / "profilers" / "torch"

FAST = settings(
    max_examples=examples(pr=6, nightly=100),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
FAST_SHORT = FAST


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


WORKLOAD = "run-workload"
SERVER = "serve-model"
LOAD = "drive-load"
READY = "check-ready"
_TRACE_JSON = (
    Path(__file__).parent / "fixtures" / "torch" / "eager_trimmed.pt.trace.json"
).read_bytes()


@pytest.fixture
def world(
    capture_ops: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ScriptedHost]:
    """A scripted host with an isolated capture store, installed for the test."""
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(tmp_path / "profiles"))
    host = ScriptedHost()
    with capture_ops.capture_runtime.use_host(host):
        yield host


def _captures(world: ScriptedHost) -> list[Path]:
    del world
    return sorted(Path(os.environ["VIBESYS_PROFILE_DIR"]).iterdir())


def _manifest(capture: Path) -> dict:
    return json.loads((capture / "manifest.json").read_text())


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
    capture_ops: ModuleType, world: ScriptedHost, tmp_path: Path
) -> None:
    """The wait ends on the export itself; grace_s is only a failure bound."""
    _write_window(tmp_path, 4242, 1, "pending")
    world.clock.at(3.0, lambda: _write_trace(tmp_path / "4242-1.pt.trace.json.gz", n_kernels=2))

    pending = capture_ops.wait_for_started_windows(tmp_path, grace_s=60, pid_alive=lambda _: True)

    assert pending == []
    assert 3.0 <= world.clock.now < 4.0
    assert capture_ops.discover_traces(tmp_path) == [tmp_path / "4242-1.pt.trace.json.gz"]


def test_wait_for_started_windows_reports_what_is_still_missing_after_the_grace(
    capture_ops: ModuleType, world: ScriptedHost, tmp_path: Path
) -> None:
    _write_window(tmp_path, 4242, 3, "pending")

    unfinished = capture_ops.wait_for_started_windows(
        tmp_path, grace_s=5, pid_alive=lambda _pid: True
    )

    assert unfinished == ["pid 4242 window 3"]
    assert world.clock.now >= 5


def test_wait_for_additional_traces_zero_grace_is_a_no_op(
    capture_ops: ModuleType, tmp_path: Path
) -> None:
    """The inject=False fallback (no handshake with a foreign profiler) still honors grace 0."""
    capture_ops.wait_for_additional_traces(tmp_path, grace_s=0.0)


def test_profile_ops_waits_for_a_sibling_whose_export_outlasts_the_launched_process(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    """The launched process (like a serving API server) exits first; its sibling (like an engine
    core) exports only afterwards, slower than any "unchanged for one poll" heuristic tolerates.
    The capture waits on the sibling's window marker, not on a guessed delay."""

    def workload(proc: FakeProcess) -> None:
        proc.spawn_child(injected_process(_TRACE_JSON, export_delay_s=2.5, run_s=1.0))
        injected_process(_TRACE_JSON, run_s=0.5)(proc)

    world.scripts[WORKLOAD] = workload
    output = capture_ops.profile_ops(command=WORKLOAD, delay_s=0.0, timeout_s=60, grace_s=60)

    assert ": ok" in output, output
    assert "had not exported" not in output, output
    (capture,) = _captures(world)
    assert len(_manifest(capture)["trace_files"]) == 2


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
# profile_ops end to end (scripted processes)
# ---------------------------------------------------------------------------


def test_profile_ops_bounded_script_produces_certified_summary(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    world.scripts[WORKLOAD] = injected_process(_TRACE_JSON, run_s=0.2)

    output = capture_ops.profile_ops(
        command=WORKLOAD, delay_s=0.0, duration_s=0.2, record_shapes=True, timeout_s=20, grace_s=5
    )

    assert "capture ops-" in output
    assert ": ok" in output
    assert "primary trace:" in output
    assert "--- certify ---" in output
    assert "--- top kernels ---" in output
    assert "--- gemm shapes ---" in output
    (capture,) = _captures(world)
    manifest = _manifest(capture)
    assert manifest["primary_trace"] is not None
    assert manifest["trace_files"] == [manifest["primary_trace"]]
    assert (capture / manifest["primary_trace"]).is_file()


def test_profile_ops_inject_false_arms_nothing_and_still_finds_a_trace_the_command_wrote(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    """Regression for the real MI210 SIGSEGV: with ``inject=False`` the injection is never armed
    (no ``VIBESYS_TORCH_PROFILE=1``, no inject dir on ``PYTHONPATH``), so a command with its own
    profiler session (a serving engine's native profiler) is not given a second, competing one,
    yet a trace it wrote itself is discovered and certified."""

    def native_profiler(proc: FakeProcess) -> None:
        out_dir = Path(proc.env["VIBESYS_TORCH_PROFILE_OUT_DIR"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{proc.pid}-native.pt.trace.json.gz").write_bytes(gzip.compress(_TRACE_JSON))
        proc.after(0.1, lambda: proc.exit(0))

    world.scripts[WORKLOAD] = native_profiler

    output = capture_ops.profile_ops(command=WORKLOAD, inject=False, timeout_s=20, grace_s=5)

    assert ": ok" in output
    assert "primary trace:" in output
    assert "--- certify ---" in output
    (spawn,) = world.spawned
    assert "VIBESYS_TORCH_PROFILE" not in spawn.env
    assert str(_TORCH_DIR / "inject") not in spawn.env.get("PYTHONPATH", "")
    (capture,) = _captures(world)
    manifest = _manifest(capture)
    assert manifest["meta"]["inject"] is False
    assert (capture / manifest["primary_trace"]).is_file()


def test_profile_ops_with_injection_arms_exactly_the_documented_environment(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    world.scripts[WORKLOAD] = injected_process(_TRACE_JSON, run_s=0.1)

    capture_ops.profile_ops(command=WORKLOAD, delay_s=0.0, timeout_s=20, grace_s=5)

    (spawn,) = world.spawned
    assert spawn.env["VIBESYS_TORCH_PROFILE"] == "1"
    assert spawn.env["PYTHONPATH"].split(os.pathsep)[0] == str(_TORCH_DIR / "inject")


def test_profile_ops_reports_no_traces_when_no_gpu(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    world.scripts[WORKLOAD] = injected_process(_TRACE_JSON, gpu=False, run_s=0.1)

    # A capture without a trace is a typed failure, not a normal result.
    with pytest.raises(RuntimeError, match="no_trace") as failed:
        capture_ops.profile_ops(
            command=WORKLOAD, delay_s=0.0, duration_s=0.1, timeout_s=20, grace_s=5
        )

    assert type(failed.value).__name__ == "CaptureFailedError"
    assert "no *.pt.trace.json.gz trace files were produced" in str(failed.value)


def test_profile_ops_server_load_sigint_lifecycle(
    capture_ops: ModuleType, world: ScriptedHost
) -> None:
    """A server armed on its first request is loaded, then stopped with SIGINT, which exports."""
    world.scripts[SERVER] = injected_process(_TRACE_JSON, export_delay_s=0.3, run_s=None)
    world.scripts[LOAD] = exits(0.5)
    world.checks[READY] = always(0)

    output = capture_ops.profile_ops(
        command=SERVER,
        ready_command=READY,
        ready_timeout_s=10,
        load_command=LOAD,
        stop_signal="SIGINT",
        grace_s=10,
        timeout_s=30,
        delay_s=0.0,
    )

    assert ": ok" in output, output
    assert "primary trace:" in output
    assert "--- certify ---" in output
    (capture,) = _captures(world)
    manifest = _manifest(capture)
    assert manifest["primary_trace"] is not None
    assert manifest["status"] == "ok"
    start, end = manifest["capture_start"], manifest["capture_end"]
    window = manifest["load_window"]
    assert 0 < start["clock_monotonic_ns"] <= end["clock_monotonic_ns"]
    assert 0 < window["start"]["clock_monotonic_ns"] <= window["end"]["clock_monotonic_ns"]
