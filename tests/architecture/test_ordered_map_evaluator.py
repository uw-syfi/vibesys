from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.support.fast_cargo import fast_cargo_env
from tests.support.shared_build import shared_build

from vibesys.inputs import load_project_task
from vs_project.api import Project

if TYPE_CHECKING:
    from collections.abc import Iterator

ORDERED_MAP_INPUTS = {
    "swmr": "swmr",
    "mw": "mw",
    "point-heavy": "point-heavy",
    "range-heavy": "range-heavy",
}

ORDERED_MAP_ACCURACY_SETTINGS = {
    "swmr": ("32", "100"),
    "mw": ("24", "100"),
    "point-heavy": ("24", "100"),
    "range-heavy": ("24", "100"),
}

ORDERED_MAP_CONTRACT = "linearizable point map with weakly consistent ordered operations"


def _tbb_is_available() -> bool:
    if shutil.which("pkg-config") is None:
        return False
    return (
        subprocess.run(
            ["pkg-config", "--exists", "tbb"],  # noqa: S607  # lint-waiver: LW-994579 [S607]; Executable name is a project tool resolved from PATH in tests.
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


CARGO_TARGET_DIRNAME = "cargo-target"


def _copy_input_bundle(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "ordered-map-candidate.so", "target"),
    )


def _project(project_root: Path) -> Path:
    return project_root / "examples" / "data-structures" / "ordered-map"


def _evaluator(project_root: Path) -> Path:
    return project_root / "resources" / "evaluators" / "ordered-map"


def _task_manifest(project_root: Path, task_name: str) -> dict[str, Any]:
    manifest = _project(project_root) / ".vibesys" / "tasks" / task_name / "vibesys.input.toml"
    return tomllib.loads(manifest.read_text())


def _evaluator_command(
    project_root: Path,
    manifest: dict[str, Any],
    section: str,
    workspace: Path,
) -> list[str]:
    """Resolve a task command the way VibeSys does for a local run."""
    evaluator = _evaluator(project_root)
    package = tomllib.loads((evaluator / "vibesys.evaluator.toml").read_text())
    prefix = package["entrypoints"][manifest[section]["entrypoint"]]
    return [part.replace("${PACKAGE_ROOT}", str(evaluator)) for part in prefix] + [
        part.replace("${PROJECT_ROOT}", str(workspace)) for part in manifest[section]["args"]
    ]


def _materialize_ordered_map_input(
    project_root: Path,
    task_name: str,
    workspace: Path,
) -> dict[str, Any]:
    """Copy the project into ``workspace`` and return the task's manifest."""
    _copy_input_bundle(_project(project_root), workspace)
    return _task_manifest(project_root, task_name)


@pytest.fixture(scope="session")
def compiled_ordered_map_candidate(tmp_path_factory) -> Path:  # noqa: ANN001  # lint-waiver: LW-994581 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    """Build the shared Rust starter once for materialized-input tests."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    starter = project_root / "examples" / "data-structures" / "ordered-map"

    def build(directory: Path) -> None:
        _copy_input_bundle(starter, directory / "starter")
        subprocess.run(
            ["make"],  # noqa: S607  # lint-waiver: LW-994582 [S607]; Executable name is a project tool resolved from PATH in tests.
            cwd=directory / "starter",
            check=True,
            env=fast_cargo_env(CARGO_TARGET_DIR=str(directory / CARGO_TARGET_DIRNAME)),
        )

    build_dir = shared_build(tmp_path_factory, "ordered-map-rs-build", build) / "starter"

    candidate = build_dir / "ordered-map-candidate.so"
    assert candidate.is_file()
    return candidate


@pytest.fixture(scope="session")
def ordered_map_native_runner(tmp_path_factory) -> Iterator[Path]:  # noqa: ANN001  # lint-waiver: LW-994583 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    """Build the trusted evaluator runner once and reuse it across subprocesses."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    source = project_root / "resources" / "evaluators" / "ordered-map" / "native_runner"

    def build(directory: Path) -> None:
        subprocess.run(  # noqa: S603  # lint-waiver: LW-994584 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
            [  # noqa: S607  # lint-waiver: LW-994585 [S607]; Executable name is a project tool resolved from PATH in tests.
                "cargo",
                "build",
                "--quiet",
                "--release",
                "--locked",
                "--manifest-path",
                str(source / "Cargo.toml"),
                "--target-dir",
                str(directory / "target"),
            ],
            cwd=source,
            check=True,
            env=fast_cargo_env(),
        )

    target_dir = shared_build(tmp_path_factory, "ordered-map-native-runner", build) / "target"
    runner = target_dir / "release" / "vibesys-ordered-map-native-runner"
    assert runner.is_file()

    environment = pytest.MonkeyPatch()
    environment.setenv("VIBESYS_ORDERED_MAP_NATIVE_RUNNER", str(runner))
    try:
        yield runner
    finally:
        environment.undo()


def test_ordered_map_manifests_invoke_go_evaluator_directly():  # noqa: ANN201  # lint-waiver: LW-994586 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    project_root = Path(__file__).parents[2]

    for input_name, scenario in ORDERED_MAP_INPUTS.items():
        manifest = _task_manifest(project_root, input_name)
        operations, trials = ORDERED_MAP_ACCURACY_SETTINGS[input_name]
        expected_suffixes = {
            "accuracy": [
                "check",
                "--workspace",
                "${PROJECT_ROOT}",
                "--scenario",
                scenario,
                "--operations",
                operations,
                "--trials",
                trials,
            ],
            "benchmark": [
                "benchmark",
                "--workspace",
                "${PROJECT_ROOT}",
                "--scenario",
                scenario,
                "--repetitions",
                "3",
            ],
        }
        assert manifest["agent"] == {"domain": "generic"}
        assert manifest["evaluator"] == {
            "name": "vibesys-evaluator-ordered-map",
            "version": "0.1.0",
        }
        for section, expected_args in expected_suffixes.items():
            assert manifest[section]["entrypoint"] == "vibesys-ordered-map"
            assert manifest[section]["args"] == expected_args
        assert manifest["benchmark"]["result"] == {
            "json_argument": "--output-json",
            "metric": "total_ops_per_sec",
        }

    evaluator = _evaluator(project_root)
    assert (evaluator / "DESIGN.md").exists()
    assert (evaluator / "CANDIDATE_CONTRACT.md").exists()
    assert (evaluator / "include" / "vibesys_ordered_map_abi.h").exists()
    go_mod = (evaluator / "go.mod").read_text()
    assert "github.com/anishathalye/porcupine v1.3.0" in go_mod
    assert "module vibesys/ordered-map-evaluator" in go_mod
    assert (evaluator / "go.sum").is_file()


def test_ordered_map_inputs_use_shared_editable_rust_starter():  # noqa: ANN201  # lint-waiver: LW-994587 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    project_root = Path(__file__).parents[2]
    starter = _project(project_root)
    project = Project.open(starter)
    evaluator = project_root / "resources" / "evaluators" / "ordered-map"
    starter_files = [
        ".gitignore",
        "Cargo.toml",
        "Cargo.lock",
        "Makefile",
        "src/lib.rs",
        "src/ffi.rs",
    ]

    for relative in starter_files:
        assert (starter / relative).is_file()

    for input_name in ORDERED_MAP_INPUTS:
        task = project.select_task(input_name)
        input_dir = task.path
        bundle = load_project_task(project, task)
        assert bundle.root == starter.resolve()
        assert bundle.workspace_sources == ()
        assert bundle.evaluator_path is None
        assert bundle.evaluator_package_root == evaluator.resolve()
        assert not (input_dir / "baseline").exists()
        assert not (input_dir / "reference").exists()
        assert not (input_dir / "pyproject.toml").exists()
        for relative in starter_files:
            assert not (input_dir / relative).exists()


def test_starter_make_honors_cargo_target_dir(compiled_ordered_map_candidate: Path) -> None:
    # The shared starter build ran ``make`` with CARGO_TARGET_DIR set, so its
    # layout is the observable effect of the Makefile honoring that variable.
    build_dir = compiled_ordered_map_candidate.parent
    built = build_dir.parent / CARGO_TARGET_DIRNAME / "release"
    assert (build_dir / "ordered-map-candidate.so").is_file()
    assert (built / "libordered_map_candidate.so").is_file() or (
        built / "libordered_map_candidate.dylib"
    ).is_file()
    assert not (build_dir / "target").exists()


@pytest.mark.native_build
def test_ordered_map_benchmark_pins_linux_workers(tmp_path):  # noqa: ANN001, ANN201  # lint-waiver: LW-994591 [ANN001, ANN201]; Pytest fixture argument and unused test return are left unannotated.
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted ordered-map evaluator")

    source = (
        Path(__file__).parents[2] / "resources" / "evaluators" / "ordered-map" / "native_runner"
    )
    text = (source / "src" / "benchmark.rs").read_text()
    assert "fn pin_current_thread(worker_index: usize)" in text
    assert "sched_setaffinity" in text
    assert "configure_benchmark_thread(lane as usize)" in text
    completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-994592 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        [  # noqa: S607  # lint-waiver: LW-994593 [S607]; Executable name is a project tool resolved from PATH in tests.
            "cargo",
            "test",
            "--locked",
            "--manifest-path",
            str(source / "Cargo.toml"),
            "--target-dir",
            str(tmp_path / "target"),
            "benchmark::pin_tests::pins_current_thread_to_indexed_cpu_from_process_mask",
            "--",
            "--exact",
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "1 passed" in completed.stdout


@pytest.mark.parametrize(("input_name", "scenario"), ORDERED_MAP_INPUTS.items())
@pytest.mark.usefixtures("ordered_map_native_runner")
def test_materialized_rust_starter_passes_accuracy(  # noqa: ANN201  # lint-waiver: LW-994594 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    tmp_path,  # noqa: ANN001  # lint-waiver: LW-994595 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    input_name,  # noqa: ANN001  # lint-waiver: LW-994596 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    scenario,  # noqa: ANN001  # lint-waiver: LW-994597 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    compiled_ordered_map_candidate,  # noqa: ANN001  # lint-waiver: LW-994598 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    manifest = _materialize_ordered_map_input(project_root, input_name, workspace)

    candidate = workspace / "ordered-map-candidate.so"
    shutil.copy2(compiled_ordered_map_candidate, candidate)
    assert candidate.is_file()

    accuracy = [
        *_evaluator_command(project_root, manifest, "accuracy", workspace),
        "--max-key-size",
        "8",
        "--max-value-size",
        "64",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-994599 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        accuracy,
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"PASS - {scenario} {ORDERED_MAP_CONTRACT}" in completed.stdout


@pytest.mark.usefixtures("ordered_map_native_runner")
def test_materialized_manifest_commands_run_go_evaluator_directly(
    tmp_path: Path, compiled_ordered_map_candidate: Path
) -> None:
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    manifest = _materialize_ordered_map_input(
        project_root,
        "swmr",
        workspace,
    )
    shutil.copy2(compiled_ordered_map_candidate, workspace / "ordered-map-candidate.so")

    accuracy = [
        *_evaluator_command(project_root, manifest, "accuracy", workspace),
        "--max-key-size",
        "8",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    subprocess.run(accuracy, cwd=workspace, check=True)  # noqa: S603  # lint-waiver: LW-994602 [S603]; Subprocess argv is a fixed trusted build or evaluator command.

    output = workspace / "results.json"
    benchmark = [
        *_evaluator_command(project_root, manifest, "benchmark", workspace),
        "--max-key-size",
        "8",
        "--duration",
        "20ms",
        "--warmup",
        "0s",
        "--output-json",
        str(output),
    ]
    subprocess.run(benchmark, cwd=workspace, check=True)  # noqa: S603  # lint-waiver: LW-994603 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
    results = json.loads(output.read_text())
    assert [result["scenario"] for result in results] == ["swmr"]
    assert all(result["repetitions"] == 3 for result in results)
    assert all(len(result["total_ops_per_sec_samples"]) == 3 for result in results)


@pytest.mark.usefixtures("ordered_map_native_runner")
def test_ordered_map_evaluator_go_tests():  # noqa: ANN201  # lint-waiver: LW-994604 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted ordered-map evaluator")

    evaluator = Path(__file__).parents[2] / "resources" / "evaluators" / "ordered-map"
    subprocess.run(["go", "test", "./..."], cwd=evaluator, check=True)  # noqa: S607  # lint-waiver: LW-994605 [S607]; Executable name is a project tool resolved from PATH in tests.


def test_ordered_map_tbb_baseline_uses_concurrent_map():  # noqa: ANN201  # lint-waiver: LW-994606 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    project_root = Path(__file__).parents[2]
    adapter = (
        project_root / "examples" / "baselines" / "ordered-map-tbb" / "tbb_ordered_map.cpp"
    ).read_text()
    assert "#include <oneapi/tbb/concurrent_map.h>" in adapter
    assert "tbb::concurrent_map" in adapter
    assert "lower_bound" in adapter
    assert "shared_mutex" not in adapter
    assert "unsafe_erase" not in adapter
    assert "string_view" in adapter
    assert "atomic_store_explicit" in adapter


@pytest.fixture(scope="session")
def built_ordered_map_tbb(tmp_path_factory) -> Path:  # noqa: ANN001  # lint-waiver: LW-994607 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    """Build the oneTBB ordered-map baseline once against the evaluator ABI header."""
    if shutil.which("c++") is None:
        pytest.skip("A C++ compiler is required by the TBB ordered-map baseline")
    if not _tbb_is_available():
        pytest.skip("oneTBB (libtbb-dev) is required by the TBB ordered-map baseline")

    project_root = Path(__file__).parents[2]
    evaluator = project_root / "resources" / "evaluators" / "ordered-map"
    abi_header = evaluator / "include" / "vibesys_ordered_map_abi.h"

    def build(directory: Path) -> None:
        shutil.copytree(
            project_root / "examples" / "baselines" / "ordered-map-tbb",
            directory / "baseline",
            ignore=shutil.ignore_patterns("ordered-map-candidate.so"),
        )
        subprocess.run(  # noqa: S603  # lint-waiver: LW-994608 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
            ["make", "clean", "all", f"ABI_HEADER={abi_header}"],  # noqa: S607  # lint-waiver: LW-994609 [S607]; Executable name is a project tool resolved from PATH in tests.
            cwd=directory / "baseline",
            check=True,
        )

    baseline = shared_build(tmp_path_factory, "ordered-map-tbb", build) / "baseline"
    assert (baseline / "ordered-map-candidate.so").is_file()
    return baseline


@pytest.mark.parametrize("scenario", list(ORDERED_MAP_INPUTS.values()))
@pytest.mark.usefixtures("ordered_map_native_runner")
def test_ordered_map_tbb_baseline_builds_and_passes_accuracy(  # noqa: ANN201  # lint-waiver: LW-994610 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    scenario,  # noqa: ANN001  # lint-waiver: LW-994611 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    built_ordered_map_tbb,  # noqa: ANN001  # lint-waiver: LW-994612 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
):
    if shutil.which("go") is None:
        pytest.skip("Go is required by the TBB ordered-map baseline")

    evaluator = Path(__file__).parents[2] / "resources" / "evaluators" / "ordered-map"
    completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-994613 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        [  # noqa: S607  # lint-waiver: LW-994614 [S607]; Executable name is a project tool resolved from PATH in tests.
            "go",
            "-C",
            str(evaluator),
            "run",
            ".",
            "check",
            "--workspace",
            str(built_ordered_map_tbb),
            "--scenario",
            scenario,
            "--max-key-size",
            "8",
            "--max-value-size",
            "64",
            "--operations",
            "12",
            "--trials",
            "4",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"PASS - {scenario} {ORDERED_MAP_CONTRACT}" in completed.stdout
