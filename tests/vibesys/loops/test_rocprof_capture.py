"""Tests for the rocprof profiler plugin's capture tools (``capture.py``).

``resources/profilers/rocprof/capture.py`` is a standalone module (stdlib
plus its sibling analyzer CLIs) staged alongside every profiler plugin, so it
is loaded by file path here rather than imported as a package, mirroring
``tests/vibesys/loops/test_capture_runtime.py`` and
``tests/vibesys/loops/test_profiler_mcp.py``.

No GPU/ROCm is available in this dev sandbox, so every ``rocprofv3``/
``rocminfo``/``rocprof-compute`` invocation is faked with small, real
subprocesses (never mocks): a fake executable on ``$PATH`` (or pointed at
directly via ``$VIBESYS_ROCPROF_COMPUTE_BIN``) that mimics the real tool's
argv contract closely enough for ``capture.py`` to drive it end to end,
including SIGINT-based graceful stop (mirroring
``test_capture_runtime.py``'s own fake-profiler harness).
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import socket
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_REPO = Path(__file__).resolve().parents[3]
_ROCPROF_DIR = _REPO / "resources" / "profilers" / "rocprof"
_COMMON_DIR = _REPO / "resources" / "profilers" / "_common"
_ROCPROF_FIXTURES = Path(__file__).parent / "fixtures" / "rocprof"

# Real subprocesses (some with signal round-trips): keep example counts small
# so the property-test slice stays fast, per test_capture_runtime.py's
# PROC_SETTINGS precedent.
PROC_SETTINGS = settings(max_examples=10, deadline=None)
PURE_SETTINGS = settings(max_examples=20, deadline=None)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations (from `from __future__ import
    # annotations`) via sys.modules[cls.__module__], so the module must be
    # registered there before exec_module runs its class bodies.
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cr = _load_module("capture_runtime", _COMMON_DIR / "capture_runtime.py")
capture = _load_module("capture", _ROCPROF_DIR / "capture.py")


# ---------------------------------------------------------------------------
# fixtures: capture store + fake executables
# ---------------------------------------------------------------------------


@pytest.fixture
def profiles_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / ".profiles"
    monkeypatch.setenv("VIBESYS_PROFILE_DIR", str(directory))
    return directory


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "fakebin"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    return directory


def _write_script(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(0o755)


def _fake_rocprofv3_source(*, fixtures_dir: Path, version: str, att_skip: bool) -> str:
    # Mirrors test_capture_runtime.py's _FAKE_PROFILER_SOURCE SIGINT harness:
    # register the handler before any other import, spawn the wrapped
    # command (after "--") as a real child sharing our own process group
    # (no start_new_session), and only write output on a clean SIGINT-ed
    # stop or a zero exit -- never on a crash.
    return textwrap.dedent(
        f"""
        import signal

        state = {{"got_sigint": False}}

        def _on_sigint(signum, frame):
            state["got_sigint"] = True

        signal.signal(signal.SIGINT, _on_sigint)

        import shutil
        import subprocess
        import sys
        from pathlib import Path

        FIXTURES = Path({str(fixtures_dir)!r})
        VERSION = {version!r}
        ATT_SKIP = {att_skip!r}

        argv = sys.argv[1:]

        if argv == ["--version"]:
            print(f"rocprofv3 (ROCm Profiler v3) version {{VERSION}}")
            sys.exit(0)

        out_dir = None
        for i, tok in enumerate(argv):
            if tok == "-d" and i + 1 < len(argv):
                out_dir = argv[i + 1]
                break

        mode = "timeline"
        if "--pmc" in argv:
            mode = "pmc"
        elif "--att" in argv:
            mode = "att"

        wrapped = argv[argv.index("--") + 1 :] if "--" in argv else []

        rc = 0
        if wrapped:
            child = subprocess.Popen(wrapped)
            rc = child.wait()

        write_output = state["got_sigint"] or rc == 0

        if write_output and out_dir:
            out_path = Path(out_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            if mode == "timeline":
                src = FIXTURES / "kernel_trace" / "out_kernel_trace.csv"
                if src.is_file():
                    shutil.copy(src, out_path / "out_kernel_trace.csv")
            elif mode == "pmc":
                set_name = out_path.name
                src = FIXTURES / "pmc" / set_name / "pass_1" / f"{{set_name}}_counter_collection.csv"
                if src.is_file():
                    shutil.copy(src, out_path / f"{{set_name}}_counter_collection.csv")
            elif mode == "att" and not ATT_SKIP:
                src = FIXTURES / "att" / "ui_output_agent_123_dispatch_1"
                if src.is_dir():
                    dst = out_path / "ui_output_agent_123_dispatch_1"
                    shutil.copytree(src, dst, dirs_exist_ok=True)

        sys.exit(rc)
        """
    )


def _install_fake_rocprofv3(
    bin_dir: Path, *, version: str = "7.2.0", att_skip: bool = False
) -> Path:
    path = bin_dir / "rocprofv3"
    _write_script(
        path,
        _fake_rocprofv3_source(fixtures_dir=_ROCPROF_FIXTURES, version=version, att_skip=att_skip),
    )
    return path


def _fake_rocminfo_source(*, gfx: str, marketing_name: str, compute_units: str) -> str:
    text = (
        "Agent 1\n"
        "  Name:                    x86_64\n"
        "  Marketing Name:          AMD EPYC 7742\n"
        "Agent 2\n"
        f"  Name:                    {gfx}\n"
        f"  Marketing Name:          {marketing_name}\n"
        f"  Compute Unit:            {compute_units}\n"
    )
    return f"import sys\nsys.stdout.write({text!r})\n"


def _install_fake_rocminfo(
    bin_dir: Path,
    *,
    gfx: str = "gfx90a",
    marketing_name: str = "AMD Instinct MI210",
    compute_units: str = "104",
) -> Path:
    path = bin_dir / "rocminfo"
    _write_script(
        path,
        _fake_rocminfo_source(gfx=gfx, marketing_name=marketing_name, compute_units=compute_units),
    )
    return path


def _install_fake_att_decoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "attlib"
    directory.mkdir(exist_ok=True)
    (directory / "librocprof-trace-decoder.so").touch()
    monkeypatch.setenv("VIBESYS_ROCPROF_ATT_LIBRARY_PATH", str(directory))
    return directory


def _fake_rocprof_compute_source(*, fixtures_dir: Path, no_copy: bool) -> str:
    return textwrap.dedent(
        f"""
        import signal

        state = {{"got_sigint": False}}

        def _on_sigint(signum, frame):
            state["got_sigint"] = True

        signal.signal(signal.SIGINT, _on_sigint)

        import shutil
        import subprocess
        import sys
        from pathlib import Path

        FIXTURES = Path({str(fixtures_dir)!r})
        NO_COPY = {no_copy!r}


        def _opt(tokens, name):
            if name in tokens:
                idx = tokens.index(name)
                if idx + 1 < len(tokens):
                    return tokens[idx + 1]
            return None


        argv = sys.argv[1:]

        if not argv:
            sys.exit(2)

        if argv[0] == "--help":
            print("fake rocprof-compute help")
            sys.exit(0)

        if argv[0] == "--version":
            print("rocprofiler-compute version: 3.1.0")
            sys.exit(0)

        sub = argv[0]
        rest = argv[1:]

        if sub == "profile":
            workload_dir = _opt(rest, "-p")
            wrapped = rest[rest.index("--") + 1 :] if "--" in rest else []
            rc = 0
            if wrapped:
                child = subprocess.Popen(wrapped)
                rc = child.wait()
            if workload_dir:
                out_path = Path(workload_dir)
                out_path.mkdir(parents=True, exist_ok=True)
                if rc == 0 and not NO_COPY:
                    for name in ("pmc_kernel_top.csv", "pmc_perf.csv", "roofline.csv"):
                        src = FIXTURES / "compute_real" / "workloads2" / name
                        if src.is_file():
                            shutil.copy(src, out_path / name)
            sys.exit(rc)

        if sub == "analyze":
            workload_dir = _opt(rest, "-p")
            print("Top Stats")
            print(f"workload: {{workload_dir}}")
            print("Speed of Light")
            print("Roofline")
            sys.exit(0)

        sys.exit(f"unknown fake rocprof-compute invocation: {{argv}}")
        """
    )


def _install_fake_rocprof_compute(
    path: Path, monkeypatch: pytest.MonkeyPatch, *, no_copy: bool = False
) -> Path:
    _write_script(
        path, _fake_rocprof_compute_source(fixtures_dir=_ROCPROF_FIXTURES, no_copy=no_copy)
    )
    monkeypatch.setenv("VIBESYS_ROCPROF_COMPUTE_BIN", str(path))
    monkeypatch.setenv("VIBESYS_ROCPROF_COMPUTE_PYTHON", sys.executable)
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# profiling_capabilities
# ---------------------------------------------------------------------------


def test_profiling_capabilities_nothing_installed(profiles_dir: Path) -> None:
    del profiles_dir
    out = capture.profiling_capabilities()

    assert "rocprofv3" in out
    assert "GPU agent" in out
    assert "ATT" in out
    assert "rocprof-compute" in out
    assert "torch" in out
    assert "capture store" in out


def test_profiling_capabilities_reports_rocprofv3_and_att_when_available(
    profiles_dir: Path, bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    _install_fake_rocprofv3(bin_dir, version="7.2.0")
    _install_fake_att_decoder(tmp_path, monkeypatch)

    out = capture.profiling_capabilities()

    assert "rocprofv3:" in out
    assert "version 7.2.0" in out
    assert "ATT (instruction-level trace): available" in out


def test_profiling_capabilities_reports_rocprof_compute_binary_when_available(
    profiles_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    compute_bin = tmp_path / "fake_rocprof_compute.py"
    _install_fake_rocprof_compute(compute_bin, monkeypatch)

    out = capture.profiling_capabilities()

    assert "rocprof-compute binary: not found" not in out


# ---------------------------------------------------------------------------
# profile_timeline
# ---------------------------------------------------------------------------


def test_profile_timeline_ok_runs_auto_summary(profiles_dir: Path, bin_dir: Path) -> None:
    del profiles_dir
    _install_fake_rocprofv3(bin_dir)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_timeline(lifecycle)

    assert "): ok" in out
    assert "flash_attn_decode_kernel" in out
    assert "Next:" in out


def test_profile_timeline_records_hip_api_and_kernel_include_in_manifest(
    profiles_dir: Path, bin_dir: Path
) -> None:
    del profiles_dir
    _install_fake_rocprofv3(bin_dir)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    capture.profile_timeline(lifecycle, hip_api=True, kernel_include="foo.*")

    (summary,) = cr.list_captures(limit=1)
    manifest = cr.load_manifest(summary.dir)
    assert manifest["meta"]["hip_api"] is True
    assert manifest["meta"]["kernel_include"] == "foo.*"


def test_profile_timeline_target_failed_has_no_kernel_data(
    profiles_dir: Path, bin_dir: Path
) -> None:
    del profiles_dir
    _install_fake_rocprofv3(bin_dir)
    lifecycle = cr.Lifecycle(command="exit 7", timeout_s=10.0)

    out = capture.profile_timeline(lifecycle)

    assert "target_failed" in out
    assert "flash_attn_decode_kernel" not in out


def test_profile_timeline_with_load_command_ready_poll_and_graceful_stop(
    profiles_dir: Path, bin_dir: Path, tmp_path: Path
) -> None:
    del profiles_dir
    _install_fake_rocprofv3(bin_dir)
    port = _free_port()

    server_script = tmp_path / "fake_server.py"
    server_script.write_text(
        textwrap.dedent(
            f"""
            import http.server


            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")

                def log_message(self, *args):
                    pass


            httpd = http.server.HTTPServer(("127.0.0.1", {port}), Handler)
            try:
                httpd.serve_forever(poll_interval=0.05)
            except KeyboardInterrupt:
                pass
            """
        )
    )
    ready_script = tmp_path / "ready_check.py"
    ready_script.write_text(
        textwrap.dedent(
            f"""
            import urllib.request

            urllib.request.urlopen("http://127.0.0.1:{port}/", timeout=1)
            """
        )
    )
    load_script = tmp_path / "load_client.py"
    load_script.write_text(
        textwrap.dedent(
            f"""
            import urllib.request

            for _ in range(2):
                urllib.request.urlopen("http://127.0.0.1:{port}/", timeout=1)
            """
        )
    )

    lifecycle = cr.Lifecycle(
        command=f"{shlex.quote(sys.executable)} {shlex.quote(str(server_script))}",
        ready_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(ready_script))}",
        ready_timeout_s=5.0,
        ready_interval_s=0.05,
        load_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(load_script))}",
        stop_signal="SIGINT",
        grace_s=3.0,
        timeout_s=10.0,
    )

    out = capture.profile_timeline(lifecycle)

    assert "): ok" in out
    (summary,) = cr.list_captures(limit=1)
    manifest = cr.load_manifest(summary.dir)
    assert manifest["ready_achieved"] is True

    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


# ---------------------------------------------------------------------------
# profile_counters
# ---------------------------------------------------------------------------


def test_profile_counters_unknown_set_raises(profiles_dir: Path, bin_dir: Path) -> None:
    del profiles_dir
    _install_fake_rocprofv3(bin_dir)
    _install_fake_rocminfo(bin_dir)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    with pytest.raises(ValueError, match="unknown_set_xyz"):
        capture.profile_counters(lifecycle, sets=["unknown_set_xyz"])


def test_profile_counters_rejects_unknown_set_mixed_with_valid(
    profiles_dir: Path, bin_dir: Path
) -> None:
    del profiles_dir
    _install_fake_rocprofv3(bin_dir)
    _install_fake_rocminfo(bin_dir)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    with pytest.raises(ValueError, match="unknown counter set") as excinfo:
        capture.profile_counters(lifecycle, sets=["l2", "bogus_set"])

    message = str(excinfo.value)
    assert "bogus_set" in message
    assert "['bogus_set']" in message


def test_profile_counters_no_gpu_detected_raises(profiles_dir: Path, bin_dir: Path) -> None:
    del profiles_dir
    # Fake rocprofv3 present, but no rocminfo on PATH: this dev sandbox has
    # no real one, so _detect_arch() should come back empty.
    _install_fake_rocprofv3(bin_dir)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    with pytest.raises(ValueError, match="architecture"):
        capture.profile_counters(lifecycle, sets=["l2"])


def test_profile_counters_success_runs_two_passes_and_triages(
    profiles_dir: Path, bin_dir: Path
) -> None:
    del profiles_dir
    _install_fake_rocprofv3(bin_dir)
    _install_fake_rocminfo(bin_dir)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_counters(lifecycle, sets=["l2", "hbm"], kernel="flash_attn.*")

    assert "): ok" in out
    assert "2 pass(es)" in out
    assert "pass l2: ok" in out
    assert "pass hbm: ok" in out
    assert "verdict:" in out

    (summary,) = cr.list_captures(limit=1)
    manifest = cr.load_manifest(summary.dir)
    assert manifest["kind"] == "counters"
    assert set(manifest["set_dirs"]) == {"l2", "hbm"}
    assert manifest["arch"] == "gfx90a"


# ---------------------------------------------------------------------------
# profile_kernel_deep
# ---------------------------------------------------------------------------


def test_profile_kernel_deep_no_rocprof_compute_binary_returns_error_string(
    profiles_dir: Path,
) -> None:
    del profiles_dir
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_kernel_deep(lifecycle, kernel="Cijk_Ailk")

    assert out.startswith("error:")
    assert "profiling_capabilities" in out


def test_profile_kernel_deep_success_path(
    profiles_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    compute_bin = tmp_path / "fake_rocprof_compute.py"
    _install_fake_rocprof_compute(compute_bin, monkeypatch)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_kernel_deep(lifecycle, kernel="Cijk_Ailk")

    assert "): ok" in out
    assert "Top Stats" in out
    assert "Next: compute_analyze" in out


def test_profile_kernel_deep_empty_match_guard(
    profiles_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    compute_bin = tmp_path / "fake_rocprof_compute_nocsv.py"
    _install_fake_rocprof_compute(compute_bin, monkeypatch, no_copy=True)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_kernel_deep(lifecycle, kernel="nonexistent-kernel-xyz")

    assert "matched no kernel dispatches" in out
    assert "hipBLASLt/Tensile" in out


# ---------------------------------------------------------------------------
# profile_instructions
# ---------------------------------------------------------------------------


def test_profile_instructions_no_decoder_returns_error_string(profiles_dir: Path) -> None:
    del profiles_dir
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_instructions(lifecycle, kernel="flash_attn.*")

    assert "rocprof-trace-decoder" in out
    assert "profiling_capabilities" in out


def test_profile_instructions_no_rocprofv3_returns_error_string(
    profiles_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    _install_fake_att_decoder(tmp_path, monkeypatch)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_instructions(lifecycle, kernel="flash_attn.*")

    assert out.startswith("error:")
    assert "rocprofv3" in out


def test_profile_instructions_rocprofv3_too_old_returns_error_string(
    profiles_dir: Path, bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    _install_fake_att_decoder(tmp_path, monkeypatch)
    _install_fake_rocprofv3(bin_dir, version="6.4.1")
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_instructions(lifecycle, kernel="flash_attn.*")

    assert out.startswith("error:")
    assert "rocprofv3" in out
    assert "6.4.1" in out


def test_profile_instructions_success_path(
    profiles_dir: Path, bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    _install_fake_att_decoder(tmp_path, monkeypatch)
    _install_fake_rocprofv3(bin_dir, version="7.2.0")
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_instructions(lifecycle, kernel="flash_attn.*")

    assert "): ok" in out
    assert "Stall category totals" in out
    assert "Next: att_hotspots" in out


def test_profile_instructions_no_dispatch_dir_produced(
    profiles_dir: Path, bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    _install_fake_att_decoder(tmp_path, monkeypatch)
    _install_fake_rocprofv3(bin_dir, version="7.2.0", att_skip=True)
    lifecycle = cr.Lifecycle(command="true", timeout_s=10.0)

    out = capture.profile_instructions(lifecycle, kernel="flash_attn.*")

    assert "): ok" in out
    assert "No decoded ui_output_agent_*_dispatch_*" in out


# ---------------------------------------------------------------------------
# profile_ops
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_torch_sibling_imports() -> Iterator[None]:
    """Undo every ``import_torch_sibling`` side effect a test makes.

    ``import_torch_sibling`` caches under the bare module name in
    ``sys.modules`` and only inserts its sibling dir into ``sys.path`` when
    not already present -- so without restoring both, a stub's tmp dir
    installed by one test (still on disk, and still first in ``sys.path``)
    would shadow the real ``resources/profilers/torch`` sibling for every
    later test in this file, including the real end-to-end one.
    """
    for name in ("capture_ops", "analyze_torch_profile"):
        sys.modules.pop(name, None)
    original_sys_path = list(sys.path)
    yield
    sys.path[:] = original_sys_path
    for name in ("capture_ops", "analyze_torch_profile"):
        sys.modules.pop(name, None)


def test_profile_ops_delegates_to_stub_and_returns_marker_unchanged(
    profiles_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    torch_dir = tmp_path / "torch"
    torch_dir.mkdir()
    (torch_dir / "capture_ops.py").write_text(
        "def profile_ops(**kwargs):\n    return 'STUB-MARKER'\n"
    )
    monkeypatch.setattr(capture, "_HERE", tmp_path / "rocprof")

    out = capture.profile_ops(command="true")

    assert out == "STUB-MARKER"


def test_profile_ops_stub_signature_mismatch_becomes_error_string(
    profiles_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    torch_dir = tmp_path / "torch"
    torch_dir.mkdir()
    (torch_dir / "capture_ops.py").write_text(
        "def profile_ops(only_this_kwarg=None):\n    raise TypeError('boom')\n"
    )
    monkeypatch.setattr(capture, "_HERE", tmp_path / "rocprof")

    out = capture.profile_ops(command="true")

    assert out.startswith("error:")
    assert "signature mismatch" in out


def test_profile_ops_not_staged_returns_error_string(
    profiles_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    monkeypatch.setattr(capture, "_HERE", tmp_path / "rocprof")

    out = capture.profile_ops(command="true")

    assert "not staged alongside rocprof" in out


def test_profile_ops_real_end_to_end_smoke_no_trace_files(profiles_dir: Path) -> None:
    del profiles_dir
    out = capture.profile_ops(command="true", timeout_s=15.0, grace_s=2.0)

    assert "no *.pt.trace.json.gz trace files were produced" in out


# ---------------------------------------------------------------------------
# captures
# ---------------------------------------------------------------------------


def test_captures_lists_recent_and_respects_limit(profiles_dir: Path) -> None:
    del profiles_dir
    id1, dir1 = cr.new_capture("timeline")
    cr.write_manifest(dir1, {"kind": "timeline", "status": "ok"})
    time.sleep(0.01)
    id2, dir2 = cr.new_capture("counters")
    cr.write_manifest(dir2, {"kind": "counters", "status": "target_failed"})

    out_all = capture.captures(limit=10)
    assert id1 in out_all
    assert id2 in out_all
    assert "timeline" in out_all
    assert "counters" in out_all

    out_one = capture.captures(limit=1)
    assert id2 in out_one
    assert id1 not in out_one


def test_captures_empty(profiles_dir: Path) -> None:
    del profiles_dir
    out = capture.captures()

    assert "(no captures under" in out


# ---------------------------------------------------------------------------
# summary: dispatch on manifest kind
# ---------------------------------------------------------------------------


def test_summary_timeline(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, capture_dir = cr.new_capture("timeline")
    shutil.copy(
        _ROCPROF_FIXTURES / "kernel_trace" / "out_kernel_trace.csv",
        capture_dir / "out_kernel_trace.csv",
    )
    cr.write_manifest(capture_dir, {"kind": "timeline", "capture_id": capture_id})

    out = capture.summary(capture_id)

    assert "flash_attn_decode_kernel" in out


def test_summary_counters(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, capture_dir = cr.new_capture("counters")
    l2_dir = capture_dir / "l2"
    hbm_dir = capture_dir / "hbm"
    shutil.copytree(_ROCPROF_FIXTURES / "pmc" / "l2", l2_dir)
    shutil.copytree(_ROCPROF_FIXTURES / "pmc" / "hbm", hbm_dir)
    cr.write_manifest(
        capture_dir,
        {
            "kind": "counters",
            "capture_id": capture_id,
            "arch": "gfx90a",
            "kernel": None,
            "set_dirs": {"l2": str(l2_dir), "hbm": str(hbm_dir)},
        },
    )

    out = capture.summary(capture_id)

    assert "flash_attn_decode_kernel" in out
    assert "verdict:" in out


def test_summary_kernel_deep(
    profiles_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del profiles_dir
    compute_bin = tmp_path / "fake_rocprof_compute.py"
    _install_fake_rocprof_compute(compute_bin, monkeypatch)
    capture_id, capture_dir = cr.new_capture("kernel_deep")
    workload_dir = capture_dir / "workloads" / capture_id
    workload_dir.mkdir(parents=True)
    for name in ("pmc_kernel_top.csv", "pmc_perf.csv", "roofline.csv"):
        shutil.copy(_ROCPROF_FIXTURES / "compute_real" / "workloads2" / name, workload_dir / name)
    cr.write_manifest(
        capture_dir,
        {
            "kind": "kernel_deep",
            "capture_id": capture_id,
            "meta": {"workload_dir": str(workload_dir)},
        },
    )

    out = capture.summary(capture_id)

    assert "Top Stats" in out


def test_summary_instructions(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, capture_dir = cr.new_capture("instructions")
    shutil.copytree(
        _ROCPROF_FIXTURES / "att" / "ui_output_agent_123_dispatch_1",
        capture_dir / "ui_output_agent_123_dispatch_1",
    )
    cr.write_manifest(capture_dir, {"kind": "instructions", "capture_id": capture_id})

    out = capture.summary(capture_id)

    assert "Stall category totals" in out


def test_summary_missing_manifest_is_a_clean_error(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, _capture_dir = cr.new_capture("timeline")
    # No manifest.json written -- capture_runtime.resolve() succeeds (the
    # directory exists) but load_manifest() must raise inside the try.

    out = capture.summary(capture_id)

    assert out.startswith("error:")


def test_summary_unknown_capture_id_returns_error_string_not_raise(profiles_dir: Path) -> None:
    del profiles_dir
    out = capture.summary("totally-bogus-capture-id-xyz")

    assert out.startswith("error:")


def test_summary_unknown_kind_in_manifest(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, capture_dir = cr.new_capture("weird")
    cr.write_manifest(capture_dir, {"kind": "totally_unknown_kind", "capture_id": capture_id})

    out = capture.summary(capture_id)

    assert out.startswith("error:")
    assert "totally_unknown_kind" in out


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def test_compare_unknown_capture_id_returns_error_string_not_raise(profiles_dir: Path) -> None:
    del profiles_dir
    out = capture.compare("no-such-a", "no-such-b")

    assert out.startswith("error:")


def test_compare_different_kinds_is_an_error(profiles_dir: Path) -> None:
    del profiles_dir
    id_a, dir_a = cr.new_capture("timeline")
    cr.write_manifest(dir_a, {"kind": "timeline"})
    id_b, dir_b = cr.new_capture("counters")
    cr.write_manifest(dir_b, {"kind": "counters"})

    out = capture.compare(id_a, id_b)

    assert out.startswith("error:")


def test_compare_timeline_reports_deltas_and_new_kernels(profiles_dir: Path) -> None:
    del profiles_dir
    id_a, dir_a = cr.new_capture("timeline")
    id_b, dir_b = cr.new_capture("timeline")

    base_csv = (_ROCPROF_FIXTURES / "kernel_trace" / "out_kernel_trace.csv").read_text()
    (dir_a / "out_kernel_trace.csv").write_text(base_csv)
    cr.write_manifest(dir_a, {"kind": "timeline", "capture_id": id_a})

    # b: the existing kernel runs longer, plus one brand-new kernel.
    rows = base_csv.strip().splitlines()
    header, data_rows = rows[0], rows[1:]
    longer_rows = []
    for row in data_rows:
        name, dispatch_id, start, end = row.split(",")
        longer_rows.append(f"{name},{dispatch_id},{start},{int(end) + 1_000_000}")
    longer_rows.append("brand_new_kernel,99,0,50000")
    (dir_b / "out_kernel_trace.csv").write_text("\n".join([header, *longer_rows]) + "\n")
    cr.write_manifest(dir_b, {"kind": "timeline", "capture_id": id_b})

    out = capture.compare(id_a, id_b)

    assert "Family time deltas" in out
    assert "delta=" in out
    assert "New kernels in b (not in a): 1" in out
    assert "brand_new_kernel" in out


def test_compare_counters_reports_per_kernel_metric_deltas(profiles_dir: Path) -> None:
    del profiles_dir
    id_a, dir_a = cr.new_capture("counters")
    before_dir = dir_a / "l2"
    shutil.copytree(_ROCPROF_FIXTURES / "pmc" / "l2", before_dir)
    cr.write_manifest(
        dir_a,
        {
            "kind": "counters",
            "capture_id": id_a,
            "arch": "gfx90a",
            "set_dirs": {"l2": str(before_dir)},
        },
    )

    id_b, dir_b = cr.new_capture("counters")
    after_dir = dir_b / "occupancy_low"
    shutil.copytree(_ROCPROF_FIXTURES / "pmc" / "occupancy_low", after_dir)
    cr.write_manifest(
        dir_b,
        {
            "kind": "counters",
            "capture_id": id_b,
            "arch": "gfx90a",
            "set_dirs": {"occupancy_low": str(after_dir)},
        },
    )

    out = capture.compare(id_a, id_b)

    assert "Counter metric deltas for gfx90a" in out
    assert "l2_hit_rate_pct" in out


@given(
    totals_a=st.dictionaries(
        st.text(min_size=1, max_size=6),
        st.floats(allow_nan=False, allow_infinity=False, width=32, min_value=-1e6, max_value=1e6),
        max_size=5,
    ),
    totals_b=st.dictionaries(
        st.text(min_size=1, max_size=6),
        st.floats(allow_nan=False, allow_infinity=False, width=32, min_value=-1e6, max_value=1e6),
        max_size=5,
    ),
)
@PURE_SETTINGS
def test_delta_rows_is_antisymmetric(
    totals_a: dict[str, float], totals_b: dict[str, float]
) -> None:
    forward = {name: delta for name, _va, _vb, delta in capture._delta_rows(totals_a, totals_b)}
    backward = {name: delta for name, _va, _vb, delta in capture._delta_rows(totals_b, totals_a)}

    assert set(forward) == set(backward)
    for name, delta in forward.items():
        assert backward[name] == pytest.approx(-delta, rel=1e-4, abs=1e-6)


# ---------------------------------------------------------------------------
# resolver helpers
# ---------------------------------------------------------------------------


def test_resolve_report_arg_capture_id_round_trips(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, directory = cr.new_capture("timeline")

    assert capture.resolve_report_arg(capture_id) == str(directory)


def test_resolve_report_arg_existing_path_passes_through(
    profiles_dir: Path, tmp_path: Path
) -> None:
    del profiles_dir
    existing = tmp_path / "some_report_dir"
    existing.mkdir()

    assert capture.resolve_report_arg(str(existing)) == str(existing)


def test_resolve_report_arg_bogus_value_passes_through(profiles_dir: Path) -> None:
    del profiles_dir
    assert capture.resolve_report_arg("no-such-capture-xyz") == "no-such-capture-xyz"


@given(kind=st.sampled_from(["timeline", "counters", "kernel_deep", "instructions", "ops"]))
@PROC_SETTINGS
def test_resolve_report_arg_round_trips_for_every_kind(
    tmp_path_factory: pytest.TempPathFactory, kind: str
) -> None:
    root = tmp_path_factory.mktemp("resolve_rt")
    previous = os.environ.get("VIBESYS_PROFILE_DIR")
    os.environ["VIBESYS_PROFILE_DIR"] = str(root)
    try:
        capture_id, directory = cr.new_capture(kind)
        assert capture.resolve_report_arg(capture_id) == str(directory)
    finally:
        if previous is None:
            os.environ.pop("VIBESYS_PROFILE_DIR", None)
        else:
            os.environ["VIBESYS_PROFILE_DIR"] = previous


def test_resolve_counter_dirs_expands_a_counters_capture(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, directory = cr.new_capture("counters")
    set_dirs = {"l2": str(directory / "l2"), "hbm": str(directory / "hbm")}
    cr.write_manifest(directory, {"kind": "counters", "set_dirs": set_dirs})

    expanded = capture.resolve_counter_dirs([capture_id])

    assert set(expanded) == set(set_dirs.values())


def test_resolve_counter_dirs_plain_paths_pass_through(profiles_dir: Path, tmp_path: Path) -> None:
    del profiles_dir
    path_a, path_b = tmp_path / "a", tmp_path / "b"
    path_a.mkdir()
    path_b.mkdir()

    assert capture.resolve_counter_dirs([str(path_a), str(path_b)]) == [str(path_a), str(path_b)]


def test_resolve_counter_dirs_non_counters_capture_is_not_expanded(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, directory = cr.new_capture("timeline")
    cr.write_manifest(directory, {"kind": "timeline"})

    assert capture.resolve_counter_dirs([capture_id]) == [str(directory)]


def test_resolve_kernel_deep_workload_arg_uses_manifest_meta(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, directory = cr.new_capture("kernel_deep")
    workload_dir = directory / "workloads" / capture_id
    workload_dir.mkdir(parents=True)
    cr.write_manifest(
        directory, {"kind": "kernel_deep", "meta": {"workload_dir": str(workload_dir)}}
    )

    assert capture.resolve_kernel_deep_workload_arg(capture_id) == str(workload_dir)


def test_resolve_kernel_deep_workload_arg_plain_dir_passes_through(tmp_path: Path) -> None:
    directory = tmp_path / "workload"
    directory.mkdir()

    assert capture.resolve_kernel_deep_workload_arg(str(directory)) == str(directory)


def test_resolve_ops_trace_arg_uses_primary_trace(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, directory = cr.new_capture("ops")
    (directory / "0.pt.trace.json.gz").write_bytes(b"")
    cr.write_manifest(directory, {"kind": "ops", "primary_trace": "0.pt.trace.json.gz"})

    expected = str(directory / "0.pt.trace.json.gz")
    assert capture.resolve_ops_trace_arg(capture_id) == expected


def test_resolve_ops_trace_arg_plain_file_passes_through(tmp_path: Path) -> None:
    trace = tmp_path / "trace.pt.trace.json"
    trace.write_text("{}")

    assert capture.resolve_ops_trace_arg(str(trace)) == str(trace)


def test_resolve_ops_trace_arg_no_primary_trace_falls_back_to_input(profiles_dir: Path) -> None:
    del profiles_dir
    capture_id, directory = cr.new_capture("ops")
    cr.write_manifest(directory, {"kind": "ops"})

    assert capture.resolve_ops_trace_arg(capture_id) == capture_id


# ---------------------------------------------------------------------------
# counter-set catalogue invariant
# ---------------------------------------------------------------------------


def test_every_counter_set_has_between_one_and_four_counters() -> None:
    for arch, sets in capture.counters.COUNTER_SETS.items():
        for set_name, cset in sets.items():
            assert 1 <= len(cset.counters) <= 4, (arch, set_name, cset.counters)
