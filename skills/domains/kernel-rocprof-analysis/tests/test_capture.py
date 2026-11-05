"""The capture wrapper: version preflight, `capture.json`, and handing the process to the profiler.

`capture.py` runs inside a candidate container, so a test describes that container with a Fake
`Host` (which programs exist and what they print) and replaces `exec` with a recorder: no real
profiler, GPU, clock or process is involved. Signals reach the profiler because the wrapper
`exec`s it (the profiler keeps the wrapper's PID and nothing stands between them), so the
signal guarantee is checked as "the wrapper execs exactly once, as its last act, never running
the command itself".
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kernel_rocprof_capture", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their annotations through this
    spec.loader.exec_module(module)
    return module


capture = _load()
STARTED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

ROCPROFV3_72 = "rocprofv3 (rocprofiler-sdk-tool)\nversion: 1.3.2\nrocm_version: 7.2.3\n"


@dataclass
class FakeContainer:
    """A candidate container: its programs, what each prints, and its files."""

    programs: dict[str, str] = field(default_factory=dict)  # name -> path
    outputs: dict[tuple[str, ...], tuple[int | None, str]] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    ran: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv: Sequence[str]) -> tuple[int | None, str]:
        key = tuple(argv)
        self.ran.append(key)
        return self.outputs.get(key, (None, ""))

    def host(self) -> object:
        return capture.Host(
            run=self.run,
            read_text=self.files.get,
            is_file=lambda path: path in self.files,
            which=self.programs.get,
        )


def _container(
    *,
    profiler_rocm: str | None = "7.2.3",
    hipconfig: str | None = "7.2.53211-abc",
    torch_hip: str | None = None,
) -> FakeContainer:
    box = FakeContainer(files={capture.AMDGPU_VERSION_FILE: "6.14.14\n"})
    if profiler_rocm is not None:
        box.programs["rocprofv3"] = "/opt/rocm/bin/rocprofv3"
        text = f"version: 1.3.2\nrocm_version: {profiler_rocm}\n"
        box.outputs[("/opt/rocm/bin/rocprofv3", "--version")] = (0, text)
    if hipconfig is not None:
        box.programs["hipconfig"] = "/opt/rocm/bin/hipconfig"
        box.outputs[("/opt/rocm/bin/hipconfig", "--version")] = (0, hipconfig)
    if torch_hip is not None:
        box.outputs[("python3", "-c", "import torch; print(torch.version.hip or '')")] = (
            0,
            torch_hip + "\n",
        )
    return box


@dataclass
class ExecRecorder:
    calls: list[tuple[str, list[str]]] = field(default_factory=list)

    def __call__(self, file: str, argv: list[str]) -> None:
        self.calls.append((file, list(argv)))


def _main(
    box: FakeContainer, argv: list[str], env: dict[str, str]
) -> tuple[int, ExecRecorder, str]:
    recorder = ExecRecorder()
    out = io.StringIO()
    status = capture.main(
        argv,
        environ=env,
        host=box.host(),
        execvp=recorder,
        now=lambda: STARTED,
        stdout=out,
    )
    return status, recorder, out.getvalue()


def _with_python(argv: list[str]) -> list[str]:
    return [argv[0], "--python", "python3", *argv[1:]]


SERVE = ["python", "-m", "engine.server", "--port", "8123"]


# ---------------------------------------------------------------------------
# The preflight rule
# ---------------------------------------------------------------------------

_RELEASES = st.tuples(st.integers(5, 8), st.integers(0, 4))


@given(profiler=_RELEASES, runtime=_RELEASES)
def test_the_preflight_refuses_exactly_a_major_mismatch_or_a_profiler_older_than_the_runtime(
    profiler: tuple[int, int], runtime: tuple[int, int]
) -> None:
    versions = {
        "rocprofv3": {"path": "/x/rocprofv3", "rocm": f"{profiler[0]}.{profiler[1]}.0"},
        "hip_runtime": {"torch": f"{runtime[0]}.{runtime[1]}.41133"},
    }
    findings = capture.check_versions("timeline", versions)
    incompatible = runtime[0] != profiler[0] or runtime > profiler
    assert bool(findings) == incompatible
    for finding in findings:
        assert f"ROCm {profiler[0]}.{profiler[1]}" in finding
        assert f"{runtime[0]}.{runtime[1]}.41133" in finding


@given(profiler=_RELEASES)
def test_thread_trace_needs_rocm_7_1(profiler: tuple[int, int]) -> None:
    versions = {"rocprofv3": {"path": "/x/rocprofv3", "rocm": f"{profiler[0]}.{profiler[1]}.0"}}
    refused = bool(capture.check_versions("thread-trace", versions))
    assert refused == (profiler < (7, 1))


def test_a_missing_profiler_is_named_for_every_recipe_that_runs_it() -> None:
    for recipe in capture.ROCPROFV3_RECIPES:
        assert capture.missing_tools(recipe, {"rocprofv3": {"path": None}})
    assert capture.missing_tools("kernel-deep", {"rocprof-compute": {"path": None}})
    assert not capture.missing_tools("check", {})


def test_a_missing_profiler_is_refused_even_with_the_mismatch_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["timeline", "--allow-version-mismatch", "--", *SERVE]
    box = _container(profiler_rocm=None)
    status, recorder, _out = _main(box, _with_python(argv), {capture.ARTIFACTS_ENV: str(tmp_path)})
    assert (status, recorder.calls) == (capture.EXIT_REFUSED, [])
    assert "rocprofv3 was not found" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_versions_are_read_from_each_tool_and_the_driver() -> None:
    box = _container(torch_hip="7.2.53211")
    versions = capture.collect_versions(box.host(), python="python3")
    assert versions["rocprofv3"] == {
        "path": "/opt/rocm/bin/rocprofv3",
        "tool": "1.3.2",
        "rocm": "7.2.3",
    }
    assert versions["hip_runtime"] == {"hipconfig": "7.2.53211", "torch": "7.2.53211"}
    assert versions["amdgpu_driver"] == {
        "version": "6.14.14",
        "source": capture.AMDGPU_VERSION_FILE,
    }


def test_the_driver_falls_back_to_amd_smi_and_is_null_when_unknown() -> None:
    box = _container()
    del box.files[capture.AMDGPU_VERSION_FILE]
    assert (
        capture.collect_versions(box.host(), python="python3")["amdgpu_driver"]["version"] is None
    )
    box.programs["amd-smi"] = "/usr/bin/amd-smi"
    box.outputs[("/usr/bin/amd-smi", "version")] = (
        0,
        "AMDSMI Tool: 26.0 | amdgpu version: 6.12.12 |\n",
    )
    driver = capture.collect_versions(box.host(), python="python3")["amdgpu_driver"]
    assert driver == {"version": "6.12.12", "source": "amd-smi version"}


def test_rocprofv3_version_output_of_both_generations_parses() -> None:
    assert capture.parse_rocprofv3_version(ROCPROFV3_72) == {"tool": "1.3.2", "rocm": "7.2.3"}
    assert capture.parse_rocprofv3_version("rocprofv3 6.4.1\n") == {
        "tool": "6.4.1",
        "rocm": "6.4.1",
    }


# ---------------------------------------------------------------------------
# The whole wrapper
# ---------------------------------------------------------------------------


def test_a_matched_capture_writes_its_record_then_execs_the_profiler(tmp_path: Path) -> None:
    box = _container(torch_hip="7.2.53211")
    argv = ["timeline", "--hip-api", "--collection-period", "120:30", "--", *SERVE]
    status, recorder, _out = _main(box, _with_python(argv), {capture.ARTIFACTS_ENV: str(tmp_path)})

    capture_dir = tmp_path / "timeline"
    expected_exec = [
        "rocprofv3", "--kernel-trace", "--memory-copy-trace", "--stats", "--hip-runtime-trace",
        "--output-format", "csv", "--collection-period", "120:30:1", "-d", str(capture_dir),
        "--", *SERVE,
    ]  # fmt: skip
    assert recorder.calls == [("rocprofv3", expected_exec)]
    assert status == capture.EXIT_REFUSED  # only reached because the recorder returned
    record = json.loads((capture_dir / "capture.json").read_text())
    assert record["schema"] == capture.SCHEMA_VERSION
    assert record["recipe"] == "timeline"
    assert record["command"] == SERVE
    assert record["profiler_argv"] == expected_exec
    assert record["started_at"] == STARTED.isoformat()
    assert record["preflight"] == {"findings": [], "overridden": False}
    assert record["versions"]["rocprofv3"]["rocm"] == "7.2.3"
    assert record["versions"]["hip_runtime"] == {"hipconfig": "7.2.53211", "torch": "7.2.53211"}
    assert record["versions"]["amdgpu_driver"]["version"] == "6.14.14"
    assert record["options"]["collection_period"] == "120:30"


def test_a_mismatched_capture_is_refused_naming_the_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    box = _container(profiler_rocm="6.4.1", torch_hip="7.0.51831")
    status, recorder, _out = _main(
        box, _with_python(["timeline", "--", *SERVE]), {capture.ARTIFACTS_ENV: str(tmp_path)}
    )
    err = capsys.readouterr().err
    assert status == capture.EXIT_REFUSED
    assert recorder.calls == []
    assert not (tmp_path / "timeline").exists()
    assert "ROCm 6.4" in err
    assert "7.0.51831" in err
    assert "--allow-version-mismatch" in err


def test_an_overridden_mismatch_is_captured_and_recorded_as_overridden(tmp_path: Path) -> None:
    box = _container(profiler_rocm="6.4.1", hipconfig="7.0.51831-x")
    argv = ["counters", "--allow-version-mismatch", "--pmc", "SQ_WAVES", "GRBM_COUNT", "--", *SERVE]
    _status, recorder, _out = _main(box, _with_python(argv), {capture.ARTIFACTS_ENV: str(tmp_path)})
    assert len(recorder.calls) == 1
    record = json.loads((tmp_path / "counters" / "capture.json").read_text())
    assert record["preflight"]["overridden"] is True
    assert record["preflight"]["findings"]
    assert recorder.calls[0][1][:4] == ["rocprofv3", "--pmc", "SQ_WAVES", "GRBM_COUNT"]


_ARG = st.text(
    st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=12
)


@given(
    command=st.lists(_ARG, min_size=1, max_size=6), recipe=st.sampled_from(["timeline", "counters"])
)
def test_the_profiler_gets_the_command_verbatim_and_the_wrapper_never_runs_it(
    tmp_path_factory: pytest.TempPathFactory, command: list[str], recipe: str
) -> None:
    """Every argument, a `--` or spaces included, reaches the profiler unchanged, by one exec."""
    root = tmp_path_factory.mktemp("artifacts")
    box = _container()
    options = ["--pmc", "SQ_WAVES"] if recipe == "counters" else []
    _status, recorder, _out = _main(
        box, _with_python([recipe, *options, "--", *command]), {capture.ARTIFACTS_ENV: str(root)}
    )
    assert len(recorder.calls) == 1
    file, exec_argv = recorder.calls[0]
    assert file == exec_argv[0] == "rocprofv3"
    cut = exec_argv.index("--")
    assert exec_argv[cut + 1 :] == command
    assert all(tuple(command) != ran[: len(command)] for ran in box.ran)


def test_thread_trace_and_kernel_deep_build_their_profiler_invocations(tmp_path: Path) -> None:
    box = _container()
    box.files["/opt/rocm/lib/" + capture.ATT_DECODER_LIB] = ""
    argv = ["thread-trace", "--name", "att1", "--kernel-include", "fmha", "--", *SERVE]
    _status, recorder, _out = _main(box, _with_python(argv), {capture.ARTIFACTS_ENV: str(tmp_path)})
    att = recorder.calls[0][1]
    assert att[:2] == ["rocprofv3", "--att"]
    assert att[att.index("--att-library-path") + 1] == "/opt/rocm/lib"
    assert att[att.index("--kernel-include-regex") + 1] == "fmha"

    box.programs["rocprof-compute"] = "/opt/rocm/bin/rocprof-compute"
    argv = ["kernel-deep", "--kernel", "Cijk_", "--dispatch", "3", "--", *SERVE]
    _status, recorder, _out = _main(box, _with_python(argv), {capture.ARTIFACTS_ENV: str(tmp_path)})
    deep = recorder.calls[0][1]
    assert deep[:3] == ["rocprof-compute", "profile", "-n"]
    assert deep[deep.index("-p") + 1] == str(tmp_path / "kernel-deep" / "workload")
    assert deep[deep.index("--dispatch") + 1] == "3"


def test_thread_trace_without_the_decoder_library_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["thread-trace", "--kernel-include", "fmha", "--", *SERVE]
    status, recorder, _out = _main(
        _container(), _with_python(argv), {capture.ARTIFACTS_ENV: str(tmp_path)}
    )
    assert (status, recorder.calls) == (capture.EXIT_REFUSED, [])
    assert capture.ATT_DECODER_LIB in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        pytest.param(["timeline", "--", *SERVE], {}, capture.ARTIFACTS_ENV, id="no-artifacts-dir"),
        pytest.param(["timeline"], {"VIBESYS_ARTIFACTS_DIR": "{tmp}"}, "after --", id="no-command"),
        pytest.param(
            ["counters", "--", *SERVE], {"VIBESYS_ARTIFACTS_DIR": "{tmp}"}, "--pmc", id="no-pmc"
        ),
        pytest.param(
            ["timeline", "--collection-period", "soon", "--", *SERVE],
            {"VIBESYS_ARTIFACTS_DIR": "{tmp}"},
            "--collection-period",
            id="bad-period",
        ),
        pytest.param(
            ["timeline", "--name", "../up", "--", *SERVE],
            {"VIBESYS_ARTIFACTS_DIR": "{tmp}"},
            "--name",
            id="bad-name",
        ),
    ],
)
def test_wrong_input_is_refused_naming_it_and_runs_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    env: dict[str, str],
    message: str,
) -> None:
    env = {key: value.format(tmp=tmp_path) for key, value in env.items()}
    status, recorder, _out = _main(_container(), _with_python(argv), env)
    assert (status, recorder.calls) == (capture.EXIT_REFUSED, [])
    assert message in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_a_second_capture_under_the_same_name_is_refused(tmp_path: Path) -> None:
    env = {capture.ARTIFACTS_ENV: str(tmp_path)}
    _main(_container(), _with_python(["timeline", "--", *SERVE]), env)
    first = (tmp_path / "timeline" / "capture.json").read_text()
    status, recorder, _out = _main(_container(), _with_python(["timeline", "--", *SERVE]), env)
    assert (status, recorder.calls) == (capture.EXIT_REFUSED, [])
    assert (tmp_path / "timeline" / "capture.json").read_text() == first


def test_check_reports_the_versions_and_exits_by_the_verdict() -> None:
    status, recorder, out = _main(
        _container(torch_hip="7.2.1"), ["check", "--python", "python3"], {}
    )
    assert (status, recorder.calls) == (0, [])
    assert json.loads(out)["versions"]["hip_runtime"]["torch"] == "7.2.1"
    status, _recorder, out = _main(
        _container(profiler_rocm="6.2.0"), ["check", "--python", "python3"], {}
    )
    assert status == capture.EXIT_REFUSED
    assert json.loads(out)["findings"]


def test_dry_run_prints_the_record_and_writes_nothing(tmp_path: Path) -> None:
    argv = ["timeline", "--dry-run", "--", *SERVE]
    status, recorder, out = _main(
        _container(), _with_python(argv), {capture.ARTIFACTS_ENV: str(tmp_path)}
    )
    assert (status, recorder.calls) == (0, [])
    assert '"recipe": "timeline"' in out
    assert list(tmp_path.iterdir()) == []
