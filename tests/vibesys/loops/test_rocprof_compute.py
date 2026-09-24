"""Tests for resources/profilers/rocprof/compute.py.

That module is a standalone, stdlib-only CLI (see its module docstring): no
vibesys imports, staged verbatim into the agent workspace as
``rocprof_profiler/compute.py``. It is loaded here by file path, the same
way ``tests/vibesys/loops/test_profiler_mcp.py`` loads the sibling nsys/torch
profiler modules, so these tests stay decoupled from sys.path/package state.

Every external-tool interaction here is exercised through a mocked
``subprocess.run``/``subprocess.Popen``, or (for ``run_with_timeout``'s
process-group kill behavior) a real, sub-second child process -- never a real
``rocprof-compute``/``rocprofv3`` install.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from types import ModuleType

_MODULE_NAME = "rocprof_compute_under_test"
_MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "resources" / "profilers" / "rocprof" / "compute.py"
)
# Real MI210 (gfx90a), rocprof-compute 3.1.0 output, trimmed -- see
# amd-profiler-samples/FINDINGS.md ("3. rocprof-compute").
_REAL_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "rocprof" / "compute_real"
_REAL_WORKLOAD_DIR = _REAL_FIXTURE_DIR / "workloads2"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Dataclasses with `from __future__ import annotations` resolve their
    # field types via `sys.modules[cls.__module__]`, so the module must be
    # registered under its own name before `exec_module` runs.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


# Loaded once at collection time (not inside a fixture) so the `from
# rocprof_compute_under_test import _private_helper` statements below can
# resolve it through sys.modules. Testing these internal helpers directly
# this way -- rather than via `compute._private_helper(...)` attribute
# access -- keeps them plain name references instead of a private-attribute
# access pattern.
_load_module(_MODULE_NAME, _MODULE_PATH)

from rocprof_compute_under_test import (  # noqa: E402  (module must load first)
    _build_profile_cmd,
    _clean_analyze_output,
    _empty_match_message,
    _find_workload_csvs,
    _log_shows_zero_contexts,
    _maybe_preflight_torch_import,
    _print_csv_fallback,
    _print_pmc_perf_ratios,
    _print_top_kernels_from_csv,
    _python_satisfies_deps,
    _report_profile_result,
    _require_rocprof_compute,
    _resolve_profiled_cmd,
    _rocm_path_roots,
    _shebang_python,
    _sum_column,
    _workload_has_kernel_data,
)


@pytest.fixture(scope="module")
def compute() -> ModuleType:
    return sys.modules[_MODULE_NAME]


@pytest.fixture(autouse=True)
def _clean_rocm_env(monkeypatch: pytest.MonkeyPatch, compute: ModuleType) -> None:
    for name in ("ROCM_PATH", compute.ROCPROF_COMPUTE_BIN_ENV, compute.ROCPROF_COMPUTE_PYTHON_ENV):
        monkeypatch.delenv(name, raising=False)


def _make_executable(path: Path, shebang: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{shebang}\n" if shebang else "", encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Locating the tool
# ---------------------------------------------------------------------------


def test_rocm_path_roots_drops_empty_rocm_path(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.delenv("ROCM_PATH", raising=False)
    assert _rocm_path_roots() == ["/opt/rocm"]

    monkeypatch.setenv("ROCM_PATH", "/opt/custom-rocm")
    assert _rocm_path_roots() == ["/opt/custom-rocm", "/opt/rocm"]


def test_find_rocprof_compute_bin_prefers_env_override(monkeypatch, tmp_path, compute):  # noqa: ANN001, ANN201
    override = _make_executable(tmp_path / "rocprof-compute")
    monkeypatch.setenv(compute.ROCPROF_COMPUTE_BIN_ENV, str(override))

    assert compute.find_rocprof_compute_bin() == str(override)


def test_find_rocprof_compute_bin_rejects_non_executable_override(monkeypatch, tmp_path, compute):  # noqa: ANN001, ANN201
    not_executable = tmp_path / "rocprof-compute"
    not_executable.write_text("", encoding="utf-8")
    monkeypatch.setenv(compute.ROCPROF_COMPUTE_BIN_ENV, str(not_executable))
    # Even though a real binary exists on PATH, an unusable override wins (and fails), by design.
    monkeypatch.setenv("PATH", str(tmp_path))

    assert compute.find_rocprof_compute_bin() is None


def test_find_rocprof_compute_bin_falls_back_to_rocm_path(monkeypatch, tmp_path, compute):  # noqa: ANN001, ANN201
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
    rocm_root = tmp_path / "rocm"
    _make_executable(rocm_root / "bin" / "rocprof-compute")
    monkeypatch.setenv("ROCM_PATH", str(rocm_root))

    assert compute.find_rocprof_compute_bin() == str(rocm_root / "bin" / "rocprof-compute")


def test_find_rocprof_compute_bin_returns_none_when_unresolvable(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")

    assert compute.find_rocprof_compute_bin() is None


def test_find_aqlprofile_lib_searches_rocm_lib_dirs(monkeypatch, tmp_path, compute):  # noqa: ANN001, ANN201
    rocm_root = tmp_path / "rocm"
    lib_dir = rocm_root / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "libaqlprofile64.so").write_bytes(b"")
    monkeypatch.setenv("ROCM_PATH", str(rocm_root))

    assert compute.find_aqlprofile_lib() == str(lib_dir / "libaqlprofile64.so")


def test_find_aqlprofile_lib_falls_back_to_loader_search(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(compute.ctypes.util, "find_library", lambda _name: None)

    assert compute.find_aqlprofile_lib() is None


# ---------------------------------------------------------------------------
# The dependency gate
# ---------------------------------------------------------------------------


def test_shebang_python_extracts_interpreter(tmp_path):  # noqa: ANN001, ANN201
    script = _make_executable(tmp_path / "rocprof-compute", shebang="#!/opt/venv/bin/python3")

    assert _shebang_python(str(script)) == "/opt/venv/bin/python3"


def test_shebang_python_returns_none_without_shebang(tmp_path):  # noqa: ANN001, ANN201
    script = tmp_path / "rocprof-compute"
    script.write_text("plain text, no shebang\n", encoding="utf-8")

    assert _shebang_python(str(script)) is None


def test_shebang_python_returns_none_for_missing_file():  # noqa: ANN201
    assert _shebang_python("/nonexistent/rocprof-compute") is None


def test_candidate_interpreters_orders_and_dedupes(monkeypatch, tmp_path, compute):  # noqa: ANN001, ANN201
    rocprof_bin = _make_executable(tmp_path / "rocprof-compute", shebang="#!/usr/bin/env python3")
    monkeypatch.setenv(compute.ROCPROF_COMPUTE_PYTHON_ENV, "/opt/venv/bin/python")
    monkeypatch.setattr(compute.shutil, "which", lambda _name: "/usr/bin/env python3")

    ordered = compute.candidate_interpreters(str(rocprof_bin))

    assert ordered[0] == "/opt/venv/bin/python"
    # The venv override and sys.executable never repeat later in the list.
    assert ordered.count(sys.executable) == 1


def test_python_satisfies_deps_true_on_zero_exit(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "help text", ""),  # noqa: ARG005
    )

    assert _python_satisfies_deps("/usr/bin/python3", "/opt/rocm/bin/rocprof-compute") is True


def test_python_satisfies_deps_false_on_nonzero_exit(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "", "ModuleNotFoundError"),  # noqa: ARG005
    )

    assert _python_satisfies_deps("/usr/bin/python3", "/opt/rocm/bin/rocprof-compute") is False


def test_python_satisfies_deps_false_when_interpreter_missing(monkeypatch, compute):  # noqa: ANN001, ANN201
    def _raise(*_a, **_kw):  # noqa: ANN002, ANN003, ANN202
        raise FileNotFoundError

    monkeypatch.setattr(compute.subprocess, "run", _raise)

    assert _python_satisfies_deps("/missing/python", "/opt/rocm/bin/rocprof-compute") is False


def test_find_deps_python_returns_first_satisfying_candidate(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute, "candidate_interpreters", lambda _rocprof_bin: ["/bad/python", "/good/python"]
    )
    monkeypatch.setattr(
        compute, "_python_satisfies_deps", lambda python, _rocprof_bin: python == "/good/python"
    )

    assert compute.find_deps_python("/opt/rocm/bin/rocprof-compute") == "/good/python"


def test_find_deps_python_returns_none_when_no_candidate_satisfies(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(compute, "candidate_interpreters", lambda _rocprof_bin: ["/bad/python"])
    monkeypatch.setattr(compute, "_python_satisfies_deps", lambda *_a: False)

    assert compute.find_deps_python("/opt/rocm/bin/rocprof-compute") is None


def test_check_pandas_version_accepts_pandas_below_3(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "2.2.2\n", ""),  # noqa: ARG005
    )

    ok, message = compute.check_pandas_version("/usr/bin/python3")

    assert ok
    assert "2.2.2" in message


def test_check_pandas_version_rejects_pandas_3(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "3.0.1\n", ""),  # noqa: ARG005
    )

    ok, message = compute.check_pandas_version("/usr/bin/python3")

    assert not ok
    assert ">=3" in message


def test_check_pandas_version_reports_import_failure(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "", "ModuleNotFoundError: pandas"),  # noqa: ARG005
    )

    ok, message = compute.check_pandas_version("/usr/bin/python3")

    assert not ok
    assert "not importable" in message


# ---------------------------------------------------------------------------
# Subcommand -- doctor
# ---------------------------------------------------------------------------


def test_cmd_doctor_reports_all_checks_passed(monkeypatch, capsys, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute, "find_rocprof_compute_bin", lambda: "/opt/rocm/bin/rocprof-compute"
    )
    monkeypatch.setattr(compute, "find_deps_python", lambda _bin: "/usr/bin/python3")
    monkeypatch.setattr(
        compute, "check_pandas_version", lambda _py: (True, "pandas 2.2.2 under /usr/bin/python3")
    )
    monkeypatch.setattr(compute, "find_rocprof_legacy_bin", lambda: "/opt/rocm/bin/rocprof")
    monkeypatch.setattr(compute, "find_aqlprofile_lib", lambda: "/opt/rocm/lib/libaqlprofile64.so")

    compute.cmd_doctor(argparse.Namespace())

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "FAIL" not in out
    assert "All checks passed." in out


def test_cmd_doctor_reports_fixes_and_skips_downstream_checks(monkeypatch, capsys, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(compute, "find_rocprof_compute_bin", lambda: None)
    monkeypatch.setattr(compute, "find_rocprof_legacy_bin", lambda: None)
    monkeypatch.setattr(compute, "find_aqlprofile_lib", lambda: None)

    compute.cmd_doctor(argparse.Namespace())

    out = capsys.readouterr().out
    assert "[FAIL] rocprof-compute binary: not found" in out
    assert "[SKIP] deps interpreter" in out
    assert "[SKIP] pandas" in out
    assert "Fixes:" in out
    assert f"${compute.ROCPROF_COMPUTE_BIN_ENV}" in out


def test_cmd_doctor_fails_when_legacy_rocprof_missing_even_with_rocprofv3_present(monkeypatch, capsys, compute):  # noqa: ANN001, ANN201
    """Regression: doctor used to gate on ``rocprofv3``, but a real MI210 run
    confirmed rocprof-compute 3.1.0 shells out to the deprecated *legacy*
    ``rocprof`` internally, not ``rocprofv3`` (every pass logs ROCm's own
    deprecation warning for it). The old check could report "all checks
    passed" on a host with rocprofv3 but no legacy rocprof -- exactly the host
    ``profile`` would then fail on -- and would FAIL a host that has legacy
    rocprof but happens to lack rocprofv3, which ``profile`` doesn't need."""
    monkeypatch.setattr(
        compute, "find_rocprof_compute_bin", lambda: "/opt/rocm/bin/rocprof-compute"
    )
    monkeypatch.setattr(compute, "find_deps_python", lambda _bin: "/usr/bin/python3")
    monkeypatch.setattr(
        compute, "check_pandas_version", lambda _py: (True, "pandas 2.2.2 under /usr/bin/python3")
    )
    monkeypatch.setattr(compute, "find_aqlprofile_lib", lambda: "/opt/rocm/lib/libaqlprofile64.so")
    # rocprofv3 present, legacy rocprof missing -- the real dependency profile
    # is unmet even though the old check would have called this host healthy.
    monkeypatch.setattr(compute, "find_rocprofv3_bin", lambda: "/opt/rocm/bin/rocprofv3")
    monkeypatch.setattr(compute, "find_rocprof_legacy_bin", lambda: None)

    compute.cmd_doctor(argparse.Namespace())

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "legacy rocprof" in out


# ---------------------------------------------------------------------------
# Process lifecycle
# ---------------------------------------------------------------------------


def test_run_with_timeout_returns_output_on_success(compute):  # noqa: ANN001, ANN201
    rc, out = compute.run_with_timeout([sys.executable, "-c", "print('hi')"], timeout=10)

    assert rc == 0
    assert "hi" in out


def test_run_with_timeout_returns_nonzero_on_failure(compute):  # noqa: ANN001, ANN201
    rc, out = compute.run_with_timeout(
        [sys.executable, "-c", "import sys; sys.exit(2)"], timeout=10
    )

    assert rc == 2
    assert out == ""


def test_run_with_timeout_kills_a_stuck_child_and_its_subtree(compute):  # noqa: ANN001, ANN201
    start = time.monotonic()
    rc, out = compute.run_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2
    )
    elapsed = time.monotonic() - start

    assert rc == 124
    assert "TIMEOUT" in out
    # The kill actually happened; we did not wait out the 30s sleep.
    assert elapsed < 10


def test_check_torch_import_under_rocprofv3_skips_when_rocprofv3_missing(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(compute, "find_rocprofv3_bin", lambda: None)

    ok, detail = compute.check_torch_import_under_rocprofv3(sys.executable)

    assert ok
    assert "skipping preflight" in detail


def test_check_torch_import_under_rocprofv3_reports_failure(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(compute, "find_rocprofv3_bin", lambda: "/opt/rocm/bin/rocprofv3")
    monkeypatch.setattr(
        compute, "run_with_timeout", lambda *_a, **_kw: (1, "spirv-expand-step collision")
    )

    ok, detail = compute.check_torch_import_under_rocprofv3(sys.executable)

    assert not ok
    assert "collision" in detail


# ---------------------------------------------------------------------------
# Subcommand -- profile
# ---------------------------------------------------------------------------


def test_resolve_profiled_cmd_strips_leading_separator():  # noqa: ANN201
    assert _resolve_profiled_cmd(["--", "python", "driver.py"]) == ["python", "driver.py"]
    assert _resolve_profiled_cmd(["python", "driver.py"]) == ["python", "driver.py"]


def test_resolve_profiled_cmd_exits_when_empty():  # noqa: ANN201
    with pytest.raises(SystemExit):
        _resolve_profiled_cmd(["--"])
    with pytest.raises(SystemExit):
        _resolve_profiled_cmd([])


def test_require_rocprof_compute_exits_without_binary(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(compute, "find_rocprof_compute_bin", lambda: None)

    with pytest.raises(SystemExit):
        _require_rocprof_compute()


def test_require_rocprof_compute_exits_without_deps_python(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute, "find_rocprof_compute_bin", lambda: "/opt/rocm/bin/rocprof-compute"
    )
    monkeypatch.setattr(compute, "find_deps_python", lambda _bin: None)

    with pytest.raises(SystemExit):
        _require_rocprof_compute()


def test_require_rocprof_compute_returns_pair_when_resolved(monkeypatch, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute, "find_rocprof_compute_bin", lambda: "/opt/rocm/bin/rocprof-compute"
    )
    monkeypatch.setattr(compute, "find_deps_python", lambda _bin: "/usr/bin/python3")

    assert _require_rocprof_compute() == ("/opt/rocm/bin/rocprof-compute", "/usr/bin/python3")


def test_maybe_preflight_torch_import_skipped_when_not_requested():  # noqa: ANN201
    ns = argparse.Namespace(check_torch_import=False)
    _maybe_preflight_torch_import(ns, ["python", "driver.py"])  # must not raise


def test_maybe_preflight_torch_import_exits_on_failure(monkeypatch, capsys, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute, "check_torch_import_under_rocprofv3", lambda _py, **_kw: (False, "boom")
    )
    ns = argparse.Namespace(check_torch_import=True)

    with pytest.raises(SystemExit) as exc_info:
        _maybe_preflight_torch_import(ns, ["python", "driver.py"])

    assert exc_info.value.code == 3
    assert "boom" in capsys.readouterr().out


def test_build_profile_cmd_includes_optional_flags():  # noqa: ANN201
    ns = argparse.Namespace(name="run", kernel="gemm.*", dispatch=3, block=["SQ", "TCC"])

    cmd = _build_profile_cmd(
        "/usr/bin/python3",
        "/opt/rocm/bin/rocprof-compute",
        ns,
        ["python", "driver.py"],
        Path("/out/workloads/run"),
    )

    assert cmd == [
        "/usr/bin/python3",
        "/opt/rocm/bin/rocprof-compute",
        "profile",
        "-n",
        "run",
        "-p",
        "/out/workloads/run",
        "-k",
        "gemm.*",
        "--dispatch",
        "3",
        "-b",
        "SQ",
        "TCC",
        "--",
        "python",
        "driver.py",
    ]


def test_build_profile_cmd_omits_optional_flags_by_default():  # noqa: ANN201
    ns = argparse.Namespace(name="run", kernel="", dispatch=None, block=None)

    cmd = _build_profile_cmd(
        "/usr/bin/python3",
        "/opt/rocm/bin/rocprof-compute",
        ns,
        ["python", "driver.py"],
        Path("/out/workloads/run"),
    )

    assert cmd == [
        "/usr/bin/python3",
        "/opt/rocm/bin/rocprof-compute",
        "profile",
        "-n",
        "run",
        "-p",
        "/out/workloads/run",
        "--",
        "python",
        "driver.py",
    ]


def test_build_profile_cmd_always_passes_explicit_path():  # noqa: ANN201
    """Regression: ``profile`` used to rely on rocprof-compute's cwd-relative
    default output path instead of passing ``-p`` explicitly, which is what
    let ``_report_profile_result``'s old subdirectory-globbing pick the wrong
    directory on a real run (see
    ``test_report_profile_result_uses_the_passed_workload_dir_directly``)."""
    ns = argparse.Namespace(name="run", kernel="", dispatch=None, block=None)

    cmd = _build_profile_cmd(
        "/usr/bin/python3",
        "/opt/rocm/bin/rocprof-compute",
        ns,
        ["python", "driver.py"],
        Path("/out/workloads/run"),
    )

    assert "-p" in cmd
    assert cmd[cmd.index("-p") + 1] == "/out/workloads/run"


def test_report_profile_result_exits_on_nonzero_rc(tmp_path, capsys):  # noqa: ANN001, ANN201
    with pytest.raises(SystemExit):
        _report_profile_result(1, "boom", tmp_path / "out")

    assert "PROFILE FAILED" in capsys.readouterr().out


def test_report_profile_result_exits_when_no_workload_dir(tmp_path, capsys):  # noqa: ANN001, ANN201
    with pytest.raises(SystemExit):
        _report_profile_result(0, "ok", tmp_path / "does-not-exist")

    assert "no workload directory" in capsys.readouterr().out


def test_report_profile_result_prints_workload_path(tmp_path, capsys):  # noqa: ANN001, ANN201
    workload = tmp_path / "workloads" / "run"
    workload.mkdir(parents=True)
    (workload / "pmc_kernel_top.csv").write_text(
        "Kernel_Name,Count,Sum(ns)\nkernel_a,1,100\n", encoding="utf-8"
    )

    _report_profile_result(0, "ok", workload)

    out = capsys.readouterr().out
    assert f"Workload written to: {workload}" in out
    assert "python compute.py analyze" in out


def test_report_profile_result_uses_the_passed_workload_dir_directly(tmp_path, capsys):  # noqa: ANN001, ANN201
    """Regression for the real-3.1.0-output bug: with an explicit ``-p``,
    rocprof-compute writes CSVs straight into that directory with no ``<gpu>``
    subdirectory layer -- the only subdirectory present is its own internal
    ``perfmon/``. The old code globbed for "the first subdirectory" and picked
    ``perfmon`` (alphabetically first `is_dir()` match after the top-level
    ``.csv``/``.txt`` files), silently reporting the wrong workload path. Built
    from the real ``workloads2/`` layout in the compute_real fixture (perfmon/
    plus top-level CSVs, no gpu subdir)."""
    workload = tmp_path / "workloads" / "run"
    workload.mkdir(parents=True)
    (workload / "perfmon").mkdir()  # rocprof-compute's own internal subdir.
    (workload / "perfmon" / "pmc_perf_0.txt").write_text("pmc: SQ_WAVES\n", encoding="utf-8")
    (workload / "pmc_kernel_top.csv").write_text(
        "Kernel_Name,Count,Sum(ns)\nkernel_a,1,100\n", encoding="utf-8"
    )

    _report_profile_result(0, "ok", workload)

    out = capsys.readouterr().out
    assert f"Workload written to: {workload}" in out
    assert "perfmon" not in out


def test_report_profile_result_fails_loudly_on_zero_contexts_signature(tmp_path, capsys):  # noqa: ANN001, ANN201
    """Regression for the ``-k gemm`` pitfall (FINDINGS.md, real MI210 run):
    every counter-collection pass logs "0 contexts collected" and
    post-processing crashes with ``KeyError: 'Grid_Size'``. The old code just
    dumped the last 2000 chars of that traceback with no explanation. Uses the
    real, trimmed failing-run log excerpt."""
    log = (_REAL_FIXTURE_DIR / "profile_run_empty_match_excerpt.txt").read_text(encoding="utf-8")

    with pytest.raises(SystemExit):
        _report_profile_result(1, log, tmp_path / "workloads" / "run", kernel="gemm")

    out = capsys.readouterr().out
    assert "matched no dispatches" in out
    assert "Cijk" in out
    assert "rocprofv3 --kernel-trace --stats" in out
    assert "'gemm'" in out
    assert "--kernel 'gemm'" in out


def test_report_profile_result_fails_loudly_when_rc_zero_but_no_data(tmp_path, capsys):  # noqa: ANN001, ANN201
    """The "silent" half of the pitfall: a rocprof-compute build where the
    empty match doesn't crash ``profile`` (rc == 0) but leaves a header-only
    CSV. Regression: the old code took the success path and printed
    "Workload written to" for a workload with zero usable dispatches."""
    workload = tmp_path / "workloads" / "run"
    workload.mkdir(parents=True)
    (workload / "pmc_kernel_top.csv").write_text("Kernel_Name,Count,Sum(ns)\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        _report_profile_result(0, "profiled ok, no dispatches", workload, kernel="gemm")

    out = capsys.readouterr().out
    assert "matched no kernel dispatches" in out
    assert "Workload written to" not in out


def test_report_profile_result_succeeds_against_real_workload(capsys):  # noqa: ANN001, ANN201
    """Sanity check the guard doesn't false-positive on the real, successful
    workload (73 real dispatches, no filter) that seeds the compute_real
    fixture."""
    _report_profile_result(0, "ok", _REAL_WORKLOAD_DIR)

    out = capsys.readouterr().out
    assert f"Workload written to: {_REAL_WORKLOAD_DIR}" in out


def test_cmd_profile_end_to_end_with_mocked_tool(monkeypatch, tmp_path, capsys, compute):  # noqa: ANN001, ANN201
    """The command construction, out-dir creation, and workload resolution --
    everything except actually shelling out to rocprof-compute."""
    out_dir = tmp_path / "out"
    recorded: dict[str, object] = {}

    def fake_run_with_timeout(cmd, *, timeout=None, **_kw: object):  # noqa: ANN001, ANN202, ARG001
        recorded["cmd"] = cmd
        workload_dir = Path(cmd[cmd.index("-p") + 1])
        workload_dir.mkdir(parents=True)
        (workload_dir / "pmc_kernel_top.csv").write_text(
            "Kernel_Name,Count,Sum(ns)\nkernel_a,1,100\n", encoding="utf-8"
        )
        return 0, "profiled ok"

    monkeypatch.setattr(
        compute, "find_rocprof_compute_bin", lambda: "/opt/rocm/bin/rocprof-compute"
    )
    monkeypatch.setattr(compute, "find_deps_python", lambda _bin: "/usr/bin/python3")
    monkeypatch.setattr(compute, "run_with_timeout", fake_run_with_timeout)
    monkeypatch.setattr(compute, "install_signal_handlers", lambda: None)

    ns = argparse.Namespace(
        cmd=["--", "python", "driver.py"],
        name="run",
        out=str(out_dir),
        kernel="",
        dispatch=None,
        block=None,
        timeout=60.0,
        check_torch_import=False,
    )
    compute.cmd_profile(ns)

    assert "-p" in recorded["cmd"]
    assert recorded["cmd"][-2:] == ["python", "driver.py"]
    assert "Workload written to" in capsys.readouterr().out


def test_cmd_profile_end_to_end_fails_loudly_on_empty_kernel_match(monkeypatch, tmp_path, capsys, compute):  # noqa: ANN001, ANN201
    """End-to-end regression for the ``-k gemm`` pitfall through the real
    ``cmd_profile`` entry point, using the real failing-run log excerpt as the
    mocked subprocess output."""
    log = (_REAL_FIXTURE_DIR / "profile_run_empty_match_excerpt.txt").read_text(encoding="utf-8")
    out_dir = tmp_path / "out"

    def fake_run_with_timeout(cmd, *, timeout=None, **_kw: object):  # noqa: ANN001, ANN202, ARG001
        workload_dir = Path(cmd[cmd.index("-p") + 1])
        workload_dir.mkdir(parents=True)
        (workload_dir / "pmc_kernel_top.csv").write_text("Kernel_Name,Count,Sum(ns)\n", encoding="utf-8")
        return 1, log

    monkeypatch.setattr(
        compute, "find_rocprof_compute_bin", lambda: "/opt/rocm/bin/rocprof-compute"
    )
    monkeypatch.setattr(compute, "find_deps_python", lambda _bin: "/usr/bin/python3")
    monkeypatch.setattr(compute, "run_with_timeout", fake_run_with_timeout)
    monkeypatch.setattr(compute, "install_signal_handlers", lambda: None)

    ns = argparse.Namespace(
        cmd=["--", "python", "driver.py"],
        name="run",
        out=str(out_dir),
        kernel="gemm",
        dispatch=None,
        block=None,
        timeout=60.0,
        check_torch_import=False,
    )

    with pytest.raises(SystemExit):
        compute.cmd_profile(ns)

    out = capsys.readouterr().out
    assert "matched no dispatches" in out
    assert "Cijk" in out


# ---------------------------------------------------------------------------
# Subcommand -- profile: empty-match / pitfall guard helpers
# ---------------------------------------------------------------------------


def test_log_shows_zero_contexts_true_when_every_pass_is_empty():  # noqa: ANN201
    log = (
        "ROCPRofiler: 0 contexts collected, output directory /a\n"
        "ROCPRofiler: 0 contexts collected, output directory /b\n"
    )
    assert _log_shows_zero_contexts(log) is True


def test_log_shows_zero_contexts_false_when_any_pass_collected_data():  # noqa: ANN201
    log = (
        "ROCPRofiler: 0 contexts collected, output directory /a\n"
        "ROCPRofiler: 73 contexts collected, output directory /b\n"
    )
    assert _log_shows_zero_contexts(log) is False


def test_log_shows_zero_contexts_false_with_no_signature_at_all():  # noqa: ANN201
    assert _log_shows_zero_contexts("nothing relevant here") is False


def test_log_shows_zero_contexts_on_real_failing_log():  # noqa: ANN201
    log = (_REAL_FIXTURE_DIR / "profile_run_empty_match_excerpt.txt").read_text(encoding="utf-8")
    assert _log_shows_zero_contexts(log) is True


def test_log_shows_zero_contexts_on_real_successful_log():  # noqa: ANN201
    log = (_REAL_FIXTURE_DIR / "profile_run_success_excerpt.txt").read_text(encoding="utf-8")
    assert _log_shows_zero_contexts(log) is False


def test_workload_has_kernel_data_none_when_no_known_csv(tmp_path):  # noqa: ANN001, ANN201
    tmp_path.joinpath("sysinfo.csv").write_text("x\n1\n", encoding="utf-8")
    assert _workload_has_kernel_data(tmp_path) is None


def test_workload_has_kernel_data_false_on_header_only_csv(tmp_path):  # noqa: ANN001, ANN201
    tmp_path.joinpath("pmc_kernel_top.csv").write_text(
        "Kernel_Name,Count,Sum(ns)\n", encoding="utf-8"
    )
    assert _workload_has_kernel_data(tmp_path) is False


def test_workload_has_kernel_data_true_with_rows(tmp_path):  # noqa: ANN001, ANN201
    tmp_path.joinpath("pmc_kernel_top.csv").write_text(
        "Kernel_Name,Count,Sum(ns)\nkernel_a,1,100\n", encoding="utf-8"
    )
    assert _workload_has_kernel_data(tmp_path) is True


def test_workload_has_kernel_data_true_on_real_fixture():  # noqa: ANN201
    assert _workload_has_kernel_data(_REAL_WORKLOAD_DIR) is True


def test_empty_match_message_mentions_the_active_kernel_filter():  # noqa: ANN201
    message = _empty_match_message("gemm", None)
    assert "Cijk" in message
    assert "rocprofv3 --kernel-trace --stats" in message
    assert "--kernel 'gemm'" in message


def test_empty_match_message_without_a_filter_still_actionable():  # noqa: ANN201
    message = _empty_match_message("", None)
    assert "matched no dispatches" in message
    assert "Current filter" not in message


# ---------------------------------------------------------------------------
# Subcommand -- analyze
# ---------------------------------------------------------------------------


def test_clean_analyze_output_strips_ansi_and_decoration():  # noqa: ANN201
    raw = "\n\n\x1b[1m=====\x1b[0m\nTop Stats\n------\nkernel_a  42%\n\n"

    cleaned = _clean_analyze_output(raw)

    assert cleaned == "Top Stats\nkernel_a  42%"


def test_clean_analyze_output_strips_the_rocm_ascii_art_banner():  # noqa: ANN201
    """rocprof-compute prints its ROCm-logo ASCII-art banner (only whitespace
    and shape-drawing ASCII, no alnum) before every real command's output --
    pure noise that the old cleaner didn't recognize as decoration."""
    banner = (
        "\n                                 __                                       _\n"
        " _ __ ___   ___ _ __  _ __ ___  / _|       ___ ___  _ __ ___  _ __  _   _| |_ ___\n"
        "| '__/ _ \\ / __| '_ \\| '__/ _ \\| |_ _____ / __/ _ \\| '_ ` _ \\| '_ \\| | | | __/ _ \\\n"
    )
    raw = banner + "   INFO Analysis mode = cli\nTop Stats\n"

    cleaned = _clean_analyze_output(raw)

    assert "___" not in cleaned
    assert "INFO Analysis mode" in cleaned
    assert "Top Stats" in cleaned


def test_clean_analyze_output_keeps_unicode_box_drawing_tables():  # noqa: ANN201
    """Box-drawing table borders use a disjoint Unicode character set from the
    ASCII-art banner and must survive -- this is real content, not decoration."""
    raw = "╒════╤═══════╕\n│  0 │ kernel_a │\n╘════╧═══════╛\n"

    cleaned = _clean_analyze_output(raw)

    assert cleaned == raw.strip()


def test_clean_analyze_output_caps_line_count():  # noqa: ANN201
    raw = "\n".join(f"line {i}" for i in range(50))

    cleaned = _clean_analyze_output(raw, max_lines=10)

    lines = cleaned.splitlines()
    assert len(lines) == 11  # 10 kept + 1 truncation notice
    assert "line 9" in lines[9]
    assert "40 more lines truncated" in lines[10]


def test_clean_analyze_output_on_real_analyze_text():  # noqa: ANN201
    """The real (trimmed) analyze output: banner gone, INFO/table content and
    long Tensile kernel names survive, and it's capped to a small line budget
    even though the untrimmed real output runs to 1700 lines across 18
    sections when no ``-b`` filter is given."""
    raw = (_REAL_FIXTURE_DIR / "analyze_top_stats_excerpt.txt").read_text(encoding="utf-8")

    cleaned = _clean_analyze_output(raw, max_lines=60)

    assert "___" not in cleaned  # banner gone
    assert "0. Top Stats" in cleaned
    assert "Cijk_Ailk_Bljk" in cleaned  # a real Tensile kernel name survived
    assert len(cleaned.splitlines()) <= 61
    assert "more lines truncated" in cleaned


def test_find_workload_csvs_recurses(tmp_path):  # noqa: ANN001, ANN201
    (tmp_path / "gfx90a").mkdir()
    (tmp_path / "gfx90a" / "pmc_perf.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "sysinfo.csv").write_text("x\n1\n", encoding="utf-8")

    found = {p.name for p in _find_workload_csvs(str(tmp_path))}

    assert found == {"pmc_perf.csv", "sysinfo.csv"}


def test_print_csv_fallback_prefers_pmc_perf(tmp_path, capsys):  # noqa: ANN001, ANN201
    (tmp_path / "sysinfo.csv").write_text("x\n1\n", encoding="utf-8")
    (tmp_path / "pmc_perf.csv").write_text(
        "Kernel_Name,Duration\nkernel_a,10\nkernel_b,20\nkernel_c,30\n", encoding="utf-8"
    )

    _print_csv_fallback(str(tmp_path), max_rows=2)

    out = capsys.readouterr().out
    assert "pmc_perf.csv" in out
    assert "sysinfo.csv" not in out
    assert "columns (2): Kernel_Name, Duration" in out
    assert "3 data rows; showing first 2" in out
    assert "kernel_c" not in out


def test_print_csv_fallback_reports_when_no_csvs(tmp_path, capsys):  # noqa: ANN001, ANN201
    _print_csv_fallback(str(tmp_path), max_rows=5)

    assert "No CSVs found" in capsys.readouterr().out


def test_print_csv_fallback_prefers_pmc_kernel_top_over_raw_pmc_perf(tmp_path, capsys):  # noqa: ANN001, ANN201
    """rocprof-compute already writes a per-kernel time summary
    (``pmc_kernel_top.csv``) during ``profile`` itself -- prefer it over
    dumping the ~1000-column raw ``pmc_perf.csv``."""
    (tmp_path / "pmc_perf.csv").write_text("Kernel_Name,Duration\nkernel_a,10\n", encoding="utf-8")
    (tmp_path / "pmc_kernel_top.csv").write_text(
        "Kernel_Name,Count,Sum(ns),Mean(ns),Median(ns),Pct\n"
        "kernel_a,23,25767340.0,1120319.1,1120006.0,58.84\n"
        "kernel_b,23,8733168.0,379702.9,382402.0,19.94\n",
        encoding="utf-8",
    )

    _print_csv_fallback(str(tmp_path), max_rows=10)

    out = capsys.readouterr().out
    assert "top kernels by time" in out
    assert "kernel_a" in out
    assert "58.8%" in out
    # The raw pmc_perf.csv row dump is not also printed once we have a summary.
    assert "columns (2): Kernel_Name, Duration" not in out


def test_print_csv_fallback_caps_top_kernels_rows(tmp_path, capsys):  # noqa: ANN001, ANN201
    rows = "\n".join(f"kernel_{i},1,{i},{i},{i},1.0" for i in range(5))
    (tmp_path / "pmc_kernel_top.csv").write_text(
        f"Kernel_Name,Count,Sum(ns),Mean(ns),Median(ns),Pct\n{rows}\n", encoding="utf-8"
    )

    _print_csv_fallback(str(tmp_path), max_rows=2)

    out = capsys.readouterr().out
    assert "kernel_0" in out
    assert "kernel_1" in out
    assert "kernel_4" not in out
    assert "3 more kernels omitted" in out


def test_print_csv_fallback_on_real_workload(capsys):  # noqa: ANN001, ANN201
    """End-to-end against the real, trimmed MI210 workload fixture."""
    _print_csv_fallback(str(_REAL_WORKLOAD_DIR), max_rows=5)

    out = capsys.readouterr().out
    assert "top kernels by time" in out
    assert "Cijk_Ailk_Bljk" in out  # shortened but present
    assert "rough counter ratios" in out
    assert "L2 cache hit rate" in out
    assert "roofline.csv also present" in out


def test_print_top_kernels_from_csv_shortens_long_kernel_names(tmp_path, capsys):  # noqa: ANN001, ANN201
    long_name = "Cijk_" + "A" * 200
    path = tmp_path / "pmc_kernel_top.csv"
    path.write_text(
        f"Kernel_Name,Count,Sum(ns),Mean(ns),Median(ns),Pct\n{long_name},1,100,100,100,100.0\n",
        encoding="utf-8",
    )

    printed = _print_top_kernels_from_csv(path, max_rows=10)

    out = capsys.readouterr().out
    assert printed is True
    assert long_name not in out
    assert "Cijk_" in out
    assert "..." in out


def test_print_top_kernels_from_csv_returns_false_when_empty(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "pmc_kernel_top.csv"
    path.write_text("Kernel_Name,Count,Sum(ns)\n", encoding="utf-8")

    assert _print_top_kernels_from_csv(path, max_rows=10) is False


def test_print_top_kernels_from_csv_rounds_the_real_long_decimal_pct(tmp_path, capsys):  # noqa: ANN001, ANN201
    """The real ``pmc_kernel_top.csv`` (e.g. ``58.841478257827454``) shouldn't
    be dumped verbatim -- round it to something prompt-sized."""
    path = tmp_path / "pmc_kernel_top.csv"
    path.write_text(
        "Kernel_Name,Count,Sum(ns),Mean(ns),Median(ns),Pct\n"
        "kernel_a,23,25767340.0,1120319.1,1120006.0,58.841478257827454\n",
        encoding="utf-8",
    )

    _print_top_kernels_from_csv(path, max_rows=10)

    out = capsys.readouterr().out
    assert "58.8%" in out
    assert "58.841478257827454" not in out


def test_sum_column_ignores_missing_and_non_numeric_values():  # noqa: ANN201
    rows = [{"x": "1.5"}, {"x": ""}, {"x": "not-a-number"}, {"x": "2.5"}, {}]

    assert _sum_column(rows, "x") == pytest.approx(4.0)


def test_sum_column_returns_none_when_column_never_present():  # noqa: ANN201
    assert _sum_column([{"y": "1"}], "x") is None


def test_print_pmc_perf_ratios_computes_l2_hit_rate_and_mfma_share(tmp_path, capsys):  # noqa: ANN001, ANN201
    path = tmp_path / "pmc_perf.csv"
    path.write_text(
        "TCC_HIT_sum,TCC_MISS_sum,SQ_INSTS_VALU,SQ_INSTS_MFMA\n80,20,10,90\n",
        encoding="utf-8",
    )

    printed = _print_pmc_perf_ratios(path)

    out = capsys.readouterr().out
    assert printed is True
    assert "L2 cache hit rate" in out
    assert "80.0%" in out
    assert "MFMA share" in out
    assert "90.0%" in out


def test_print_pmc_perf_ratios_returns_false_without_known_columns(tmp_path):  # noqa: ANN001, ANN201
    path = tmp_path / "pmc_perf.csv"
    path.write_text("Kernel_Name,Duration\nkernel_a,10\n", encoding="utf-8")

    assert _print_pmc_perf_ratios(path) is False


def test_cmd_analyze_prints_cleaned_output_on_success(monkeypatch, tmp_path, capsys, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(
        compute, "find_rocprof_compute_bin", lambda: "/opt/rocm/bin/rocprof-compute"
    )
    monkeypatch.setattr(compute, "find_deps_python", lambda _bin: "/usr/bin/python3")
    monkeypatch.setattr(
        compute, "run_with_timeout", lambda *_a, **_kw: (0, "==\nTop Stats\nkernel_a 10%\n")
    )

    ns = argparse.Namespace(
        workload_dir=str(tmp_path), blocks="0,2,4", max_stat=10, kernel="", timeout=60.0
    )
    compute.cmd_analyze(ns)

    out = capsys.readouterr().out
    assert "Top Stats" in out
    assert "NOTE:" not in out


def test_cmd_analyze_falls_back_to_csv_on_nonzero_exit(monkeypatch, tmp_path, capsys, compute):  # noqa: ANN001, ANN201
    (tmp_path / "pmc_perf.csv").write_text("Kernel_Name,Duration\nkernel_a,10\n", encoding="utf-8")
    monkeypatch.setattr(
        compute, "find_rocprof_compute_bin", lambda: "/opt/rocm/bin/rocprof-compute"
    )
    monkeypatch.setattr(compute, "find_deps_python", lambda _bin: "/usr/bin/python3")
    monkeypatch.setattr(
        compute, "run_with_timeout", lambda *_a, **_kw: (1, "dependency gate error")
    )

    ns = argparse.Namespace(
        workload_dir=str(tmp_path), blocks="0,2,4", max_stat=10, kernel="", timeout=60.0
    )
    compute.cmd_analyze(ns)

    out = capsys.readouterr().out
    assert "NOTE: analyze failed" in out
    assert "exited 1" in out
    assert "pmc_perf.csv" in out
    assert "kernel_a" in out


def test_cmd_analyze_falls_back_when_tool_missing(monkeypatch, tmp_path, capsys, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(compute, "find_rocprof_compute_bin", lambda: None)

    ns = argparse.Namespace(
        workload_dir=str(tmp_path), blocks="0,2,4", max_stat=10, kernel="", timeout=60.0
    )
    compute.cmd_analyze(ns)

    out = capsys.readouterr().out
    assert "not available" in out
    assert "No CSVs found" in out


def test_cmd_analyze_exits_for_non_directory(compute):  # noqa: ANN001, ANN201
    ns = argparse.Namespace(
        workload_dir="/nonexistent/workload", blocks="0,2,4", max_stat=10, kernel="", timeout=60.0
    )
    with pytest.raises(SystemExit):
        compute.cmd_analyze(ns)


# ---------------------------------------------------------------------------
# Property-based fuzzing -- generalizes the empty-kernel-match bug pattern
# ---------------------------------------------------------------------------
#
# Small max_examples: these are pure-function/small-file-IO properties, no
# GPU and no real rocprof-compute involved, so they stay fast while still
# covering CSV-shape and filter-string variation the handwritten unit tests
# above only sample a few points of.

# Real kernel names from the trimmed MI210 fixture (Tensile GEMM + torch
# elementwise/RNG kernels) -- used to fuzz kernel-name filters against data
# that actually looks like a real workload.
with (_REAL_FIXTURE_DIR / "workloads2" / "pmc_kernel_top.csv").open(
    newline="", encoding="utf-8"
) as _f:
    _REAL_KERNEL_NAMES = [row["Kernel_Name"] for row in csv.DictReader(_f)]


def _real_substrings() -> list[st.SearchStrategy[str]]:
    """One strategy per real kernel name, each yielding a genuine substring of
    it (so some fuzzed filters are guaranteed to match something real)."""
    return [
        st.builds(
            lambda start, length, _name=name: _name[start : start + length],
            start=st.integers(min_value=0, max_value=len(name) - 1),
            length=st.integers(min_value=1, max_value=min(20, len(name))),
        )
        for name in _REAL_KERNEL_NAMES
    ]


@given(
    known_name=st.sampled_from(["pmc_kernel_top.csv", "pmc_perf.csv"]),
    num_rows=st.integers(min_value=0, max_value=4),
    columns=st.permutations(["Kernel_Name", "Count", "Sum(ns)"]),
    variant=st.fixed_dictionaries(
        {"extra_column": st.booleans(), "unrelated_sibling": st.booleans()}
    ),
)
@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_workload_has_kernel_data_depends_only_on_row_count(  # noqa: ANN201
    tmp_path_factory,  # noqa: ANN001
    known_name,  # noqa: ANN001
    num_rows,  # noqa: ANN001
    columns,  # noqa: ANN001
    variant,  # noqa: ANN001
):
    """Property pinning the fix for the real-3.1.0-output bug at the CSV
    level: whether a workload "has data" must depend only on row count, never
    on column order, an unknown extra column, or an unrelated sibling CSV
    sitting next to it (fuzzes the column-order/variant and partial-workload-
    dir shapes from the bug write-up)."""
    workload_dir = tmp_path_factory.mktemp("workload")
    cols = [*columns, "Extra_Col"] if variant["extra_column"] else list(columns)
    lines = [",".join(cols)]
    lines.extend(",".join(f"v{i}" for _ in cols) for i in range(num_rows))
    (workload_dir / known_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    if variant["unrelated_sibling"]:
        (workload_dir / "sysinfo.csv").write_text("unrelated\n1\n", encoding="utf-8")

    assert _workload_has_kernel_data(workload_dir) == (num_rows > 0)


@given(
    kernel_filter=st.one_of(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=10),
        *_real_substrings(),
    )
)
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_empty_match_guard_fires_iff_filter_matches_no_real_kernel(  # noqa: ANN201
    tmp_path_factory, capsys, kernel_filter  # noqa: ANN001
):
    """Property pinning the ``-k gemm`` pitfall fix against the real fixture's
    kernel names: for *any* kernel filter, simulate rocprof-compute's own
    literal-substring ``-k`` match against the real names, build the workload
    CSV a matching/non-matching run would actually produce, and assert the
    guard fires (SystemExit + actionable message) exactly when the filter
    matched nothing -- it must never silently report success on an empty
    match, and never reject a filter that would have matched something."""
    matches = [name for name in _REAL_KERNEL_NAMES if kernel_filter in name]

    workload_dir = tmp_path_factory.mktemp("workload")
    header = "Kernel_Name,Count,Sum(ns),Mean(ns),Median(ns),Pct\n"
    if matches:
        body = "".join(f"{name},1,100,100,100,10.0\n" for name in matches)
        log = "ROCPRofiler: 1 contexts collected, output directory /tmp/x\n"
    else:
        body = ""
        log = "ROCPRofiler: 0 contexts collected, output directory /tmp/x\n"
    (workload_dir / "pmc_kernel_top.csv").write_text(header + body, encoding="utf-8")

    if matches:
        _report_profile_result(0, log, workload_dir, kernel=kernel_filter)
        assert "Workload written to" in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit):
            _report_profile_result(0, log, workload_dir, kernel=kernel_filter)
        out = capsys.readouterr().out
        assert "matched no kernel dispatches" in out
        assert "Cijk" in out


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_main_dispatches_doctor(monkeypatch, compute):  # noqa: ANN001, ANN201
    calls: list[str] = []
    monkeypatch.setattr(compute, "cmd_doctor", lambda _ns: calls.append("doctor"))

    compute.main(["doctor"])

    assert calls == ["doctor"]


def test_main_requires_a_subcommand(compute):  # noqa: ANN001, ANN201
    with pytest.raises(SystemExit):
        compute.main([])
