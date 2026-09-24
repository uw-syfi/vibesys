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
import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_MODULE_NAME = "rocprof_compute_under_test"
_MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "resources" / "profilers" / "rocprof" / "compute.py"
)


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
    _find_workload_csvs,
    _maybe_preflight_torch_import,
    _print_csv_fallback,
    _python_satisfies_deps,
    _report_profile_result,
    _require_rocprof_compute,
    _resolve_profiled_cmd,
    _rocm_path_roots,
    _shebang_python,
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
    monkeypatch.setattr(compute, "find_rocprofv3_bin", lambda: "/opt/rocm/bin/rocprofv3")
    monkeypatch.setattr(compute, "find_aqlprofile_lib", lambda: "/opt/rocm/lib/libaqlprofile64.so")

    compute.cmd_doctor(argparse.Namespace())

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "FAIL" not in out
    assert "All checks passed." in out


def test_cmd_doctor_reports_fixes_and_skips_downstream_checks(monkeypatch, capsys, compute):  # noqa: ANN001, ANN201
    monkeypatch.setattr(compute, "find_rocprof_compute_bin", lambda: None)
    monkeypatch.setattr(compute, "find_rocprofv3_bin", lambda: None)
    monkeypatch.setattr(compute, "find_aqlprofile_lib", lambda: None)

    compute.cmd_doctor(argparse.Namespace())

    out = capsys.readouterr().out
    assert "[FAIL] rocprof-compute binary: not found" in out
    assert "[SKIP] deps interpreter" in out
    assert "[SKIP] pandas" in out
    assert "Fixes:" in out
    assert f"${compute.ROCPROF_COMPUTE_BIN_ENV}" in out


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
    ns = argparse.Namespace(name="run", kernel="gemm.*", dispatch=3)

    cmd = _build_profile_cmd(
        "/usr/bin/python3", "/opt/rocm/bin/rocprof-compute", ns, ["python", "driver.py"]
    )

    assert cmd == [
        "/usr/bin/python3",
        "/opt/rocm/bin/rocprof-compute",
        "profile",
        "-n",
        "run",
        "-k",
        "gemm.*",
        "--dispatch",
        "3",
        "--",
        "python",
        "driver.py",
    ]


def test_build_profile_cmd_omits_optional_flags_by_default():  # noqa: ANN201
    ns = argparse.Namespace(name="run", kernel="", dispatch=None)

    cmd = _build_profile_cmd(
        "/usr/bin/python3", "/opt/rocm/bin/rocprof-compute", ns, ["python", "driver.py"]
    )

    assert cmd == [
        "/usr/bin/python3",
        "/opt/rocm/bin/rocprof-compute",
        "profile",
        "-n",
        "run",
        "--",
        "python",
        "driver.py",
    ]


def test_report_profile_result_exits_on_nonzero_rc(tmp_path, capsys):  # noqa: ANN001, ANN201
    with pytest.raises(SystemExit):
        _report_profile_result(1, "boom", tmp_path / "out", "run")

    assert "PROFILE FAILED" in capsys.readouterr().out


def test_report_profile_result_exits_when_no_workload_dir(tmp_path, capsys):  # noqa: ANN001, ANN201
    with pytest.raises(SystemExit):
        _report_profile_result(0, "ok", tmp_path, "run")

    assert "no workload directory" in capsys.readouterr().out


def test_report_profile_result_prints_workload_path(tmp_path, capsys):  # noqa: ANN001, ANN201
    workload = tmp_path / "workloads" / "run" / "gfx90a"
    workload.mkdir(parents=True)

    _report_profile_result(0, "ok", tmp_path, "run")

    out = capsys.readouterr().out
    assert f"Workload written to: {workload}" in out
    assert "python compute.py analyze" in out


def test_cmd_profile_end_to_end_with_mocked_tool(monkeypatch, tmp_path, capsys, compute):  # noqa: ANN001, ANN201
    """The command construction, out-dir creation, and workload resolution --
    everything except actually shelling out to rocprof-compute."""
    out_dir = tmp_path / "out"
    recorded: dict[str, object] = {}

    def fake_run_with_timeout(cmd, *, cwd=None, timeout=None, **_kw: object):  # noqa: ANN001, ANN202, ARG001
        recorded["cmd"] = cmd
        recorded["cwd"] = cwd
        (Path(cwd) / "workloads" / "run" / "gfx90a").mkdir(parents=True)
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
        timeout=60.0,
        check_torch_import=False,
    )
    compute.cmd_profile(ns)

    assert recorded["cwd"] == str(out_dir)
    assert recorded["cmd"][-2:] == ["python", "driver.py"]
    assert "Workload written to" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Subcommand -- analyze
# ---------------------------------------------------------------------------


def test_clean_analyze_output_strips_ansi_and_decoration():  # noqa: ANN201
    raw = "\n\n\x1b[1m=====\x1b[0m\nTop Stats\n------\nkernel_a  42%\n\n"

    cleaned = _clean_analyze_output(raw)

    assert cleaned == "Top Stats\nkernel_a  42%"


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
