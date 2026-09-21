from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Iterator  # noqa: TC003  # tracked: #288
from pathlib import Path

import pytest

ORDERED_MAP_INPUTS = {
    "ordered-map-swmr": "swmr",
    "ordered-map-mw": "mw",
}

ORDERED_MAP_ACCURACY_SETTINGS = {
    "ordered-map-swmr": ("32", "100"),
    "ordered-map-mw": ("24", "100"),
}

ORDERED_MAP_CONTRACT = "linearizable point map with weakly consistent ordered operations"


def _tbb_is_available() -> bool:
    if shutil.which("pkg-config") is None:
        return False
    return (
        subprocess.run(
            ["pkg-config", "--exists", "tbb"],  # noqa: S607  # tracked: #288
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _copy_input_bundle(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "ordered-map-candidate.so", "target"),
    )


def _materialize_ordered_map_input(
    project_root: Path,
    input_name: str,
    workspace: Path,
) -> Path:
    from vibesys.input_manifest import load_input_bundle  # noqa: PLC0415  # tracked: #288

    input_dir = project_root / "examples" / "data-structures" / input_name
    starter = project_root / "examples" / "starters" / "ordered-map-rs"
    bundle = load_input_bundle(input_dir)
    assert bundle.evaluator_path is not None
    _copy_input_bundle(starter, workspace)
    _copy_input_bundle(input_dir, workspace)
    _copy_input_bundle(
        bundle.evaluator_path,
        workspace / "_evaluator" / bundle.evaluator_path.name,
    )
    return input_dir


@pytest.fixture(scope="session")
def compiled_ordered_map_candidate(tmp_path_factory) -> Path:  # noqa: ANN001  # tracked: #288
    """Build the shared Rust starter once for materialized-input tests."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    starter = project_root / "examples" / "starters" / "ordered-map-rs"
    build_dir = tmp_path_factory.mktemp("ordered-map-rs-build") / "starter"
    _copy_input_bundle(starter, build_dir)
    subprocess.run(["make"], cwd=build_dir, check=True)  # noqa: S607  # tracked: #288

    candidate = build_dir / "ordered-map-candidate.so"
    assert candidate.is_file()
    return candidate


@pytest.fixture(scope="session")
def ordered_map_native_runner(tmp_path_factory) -> Iterator[Path]:  # noqa: ANN001  # tracked: #288
    """Build the trusted evaluator runner once and reuse it across subprocesses."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    source = project_root / "examples" / "evaluators" / "ordered-map" / "native_runner"
    target_dir = tmp_path_factory.mktemp("ordered-map-native-runner") / "target"
    subprocess.run(  # noqa: S603  # tracked: #288
        [  # noqa: S607  # tracked: #288
            "cargo",
            "build",
            "--quiet",
            "--release",
            "--locked",
            "--manifest-path",
            str(source / "Cargo.toml"),
            "--target-dir",
            str(target_dir),
        ],
        cwd=source,
        check=True,
    )
    runner = target_dir / "release" / "vibesys-ordered-map-native-runner"
    assert runner.is_file()

    environment = pytest.MonkeyPatch()
    environment.setenv("VIBESYS_ORDERED_MAP_NATIVE_RUNNER", str(runner))
    try:
        yield runner
    finally:
        environment.undo()


def test_ordered_map_manifests_invoke_go_evaluator_directly():  # noqa: ANN201  # tracked: #288
    root = Path(__file__).parents[2] / "examples" / "data-structures"

    for input_name, scenario in ORDERED_MAP_INPUTS.items():
        manifest = tomllib.loads((root / input_name / "vibesys.input.toml").read_text())
        operations, trials = ORDERED_MAP_ACCURACY_SETTINGS[input_name]
        expected_suffixes = {
            "accuracy": [
                "run",
                ".",
                "check",
                "--workspace",
                "../..",
                "--scenario",
                scenario,
                "--operations",
                operations,
                "--trials",
                trials,
            ],
            "benchmark": [
                "run",
                ".",
                "benchmark",
                "--workspace",
                "../..",
                "--scenario",
                scenario,
                "--repetitions",
                "3",
            ],
        }
        assert manifest["agent"] == {"domain": "generic"}
        assert manifest["evaluator"] == {"source": "../../evaluators/ordered-map"}
        for section, expected_suffix in expected_suffixes.items():
            command = manifest[section]["command"]
            assert command[:3] == ["go", "-C", "_evaluator/ordered-map"]
            assert command[3:] == expected_suffix
        assert manifest["benchmark"]["result"] == {
            "json_argument": "--output-json",
            "metric": "total_ops_per_sec",
        }

    evaluator = root.parents[0] / "evaluators" / "ordered-map"
    assert (evaluator / "DESIGN.md").exists()
    assert (evaluator / "CANDIDATE_CONTRACT.md").exists()
    assert (evaluator / "include" / "vibesys_ordered_map_abi.h").exists()
    go_mod = (evaluator / "go.mod").read_text()
    assert "github.com/anishathalye/porcupine v1.3.0" in go_mod
    assert "module vibesys/ordered-map-evaluator" in go_mod
    assert (evaluator / "go.sum").is_file()


def test_ordered_map_inputs_use_shared_editable_rust_starter():  # noqa: ANN201  # tracked: #288
    from vibesys.input_manifest import load_input_bundle  # noqa: PLC0415  # tracked: #288

    project_root = Path(__file__).parents[2]
    root = project_root / "examples" / "data-structures"
    starter = project_root / "examples" / "starters" / "ordered-map-rs"
    evaluator = project_root / "examples" / "evaluators" / "ordered-map"
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
        input_dir = root / input_name
        bundle = load_input_bundle(input_dir)
        assert bundle.workspace_sources == ()
        assert bundle.evaluator_path == evaluator.resolve()
        assert not (input_dir / "baseline").exists()
        assert not (input_dir / "reference").exists()
        assert not (input_dir / "pyproject.toml").exists()
        for relative in starter_files:
            assert not (input_dir / relative).exists()


def test_starter_make_honors_cargo_target_dir(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    starter = project_root / "examples" / "starters" / "ordered-map-rs"
    build_dir = tmp_path / "starter"
    _copy_input_bundle(starter, build_dir)
    cargo_target = tmp_path / "cargo-target"
    subprocess.run(
        ["make"],  # noqa: S607  # tracked: #288
        cwd=build_dir,
        check=True,
        env=os.environ | {"CARGO_TARGET_DIR": str(cargo_target)},
    )
    assert (build_dir / "ordered-map-candidate.so").is_file()
    built = cargo_target / "release"
    assert (built / "libordered_map_candidate.so").is_file() or (
        built / "libordered_map_candidate.dylib"
    ).is_file()
    assert not (build_dir / "target").exists()


def test_ordered_map_benchmark_pins_linux_workers(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted ordered-map evaluator")

    source = Path(__file__).parents[2] / "examples" / "evaluators" / "ordered-map" / "native_runner"
    text = (source / "src" / "benchmark.rs").read_text()
    assert "fn pin_current_thread(worker_index: usize)" in text
    assert "sched_setaffinity" in text
    assert "configure_benchmark_thread(lane as usize)" in text
    completed = subprocess.run(  # noqa: S603  # tracked: #288
        [  # noqa: S607  # tracked: #288
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
def test_materialized_rust_starter_passes_accuracy(  # noqa: ANN201  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
    input_name,  # noqa: ANN001  # tracked: #288
    scenario,  # noqa: ANN001  # tracked: #288
    compiled_ordered_map_candidate,  # noqa: ANN001  # tracked: #288
):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    _materialize_ordered_map_input(project_root, input_name, workspace)

    candidate = workspace / "ordered-map-candidate.so"
    shutil.copy2(compiled_ordered_map_candidate, candidate)
    assert candidate.is_file()

    manifest = tomllib.loads((workspace / "vibesys.input.toml").read_text())
    accuracy = [
        *manifest["accuracy"]["command"],
        "--max-key-size",
        "8",
        "--max-value-size",
        "64",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    completed = subprocess.run(  # noqa: S603  # tracked: #288
        accuracy,
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"PASS - {scenario} {ORDERED_MAP_CONTRACT}" in completed.stdout


@pytest.mark.usefixtures("ordered_map_native_runner")
def test_materialized_manifest_commands_run_go_evaluator_directly(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted ordered-map evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    input_dir = _materialize_ordered_map_input(
        project_root,
        "ordered-map-swmr",
        workspace,
    )
    assert (workspace / "_evaluator" / "ordered-map" / "DESIGN.md").is_file()
    subprocess.run(["make"], cwd=workspace, check=True)  # noqa: S607  # tracked: #288
    manifest = tomllib.loads((input_dir / "vibesys.input.toml").read_text())

    accuracy = [
        *manifest["accuracy"]["command"],
        "--max-key-size",
        "8",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    subprocess.run(accuracy, cwd=workspace, check=True)  # noqa: S603  # tracked: #288

    output = workspace / "results.json"
    benchmark = [
        *manifest["benchmark"]["command"],
        "--max-key-size",
        "8",
        "--duration",
        "20ms",
        "--warmup",
        "0s",
        "--output-json",
        str(output),
    ]
    subprocess.run(benchmark, cwd=workspace, check=True)  # noqa: S603  # tracked: #288
    results = json.loads(output.read_text())
    assert [result["scenario"] for result in results] == ["swmr"]
    assert all(result["repetitions"] == 3 for result in results)
    assert all(len(result["total_ops_per_sec_samples"]) == 3 for result in results)


@pytest.mark.usefixtures("ordered_map_native_runner")
def test_ordered_map_evaluator_go_tests():  # noqa: ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted ordered-map evaluator")

    evaluator = Path(__file__).parents[2] / "examples" / "evaluators" / "ordered-map"
    subprocess.run(["go", "test", "./..."], cwd=evaluator, check=True)  # noqa: S607  # tracked: #288


def test_ordered_map_tbb_baseline_uses_concurrent_map():  # noqa: ANN201  # tracked: #288
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
def built_ordered_map_tbb(tmp_path_factory) -> Path:  # noqa: ANN001  # tracked: #288
    """Build the oneTBB ordered-map baseline once against the evaluator ABI header."""
    if shutil.which("c++") is None:
        pytest.skip("A C++ compiler is required by the TBB ordered-map baseline")
    if not _tbb_is_available():
        pytest.skip("oneTBB (libtbb-dev) is required by the TBB ordered-map baseline")

    project_root = Path(__file__).parents[2]
    evaluator = project_root / "examples" / "evaluators" / "ordered-map"
    abi_header = evaluator / "include" / "vibesys_ordered_map_abi.h"
    baseline = tmp_path_factory.mktemp("ordered-map-tbb") / "baseline"
    shutil.copytree(
        project_root / "examples" / "baselines" / "ordered-map-tbb",
        baseline,
        ignore=shutil.ignore_patterns("ordered-map-candidate.so"),
    )
    subprocess.run(  # noqa: S603  # tracked: #288
        ["make", "clean", "all", f"ABI_HEADER={abi_header}"],  # noqa: S607  # tracked: #288
        cwd=baseline,
        check=True,
    )
    assert (baseline / "ordered-map-candidate.so").is_file()
    return baseline


@pytest.mark.parametrize("scenario", list(ORDERED_MAP_INPUTS.values()))
@pytest.mark.usefixtures("ordered_map_native_runner")
def test_ordered_map_tbb_baseline_builds_and_passes_accuracy(  # noqa: ANN201  # tracked: #288
    scenario,  # noqa: ANN001  # tracked: #288
    built_ordered_map_tbb,  # noqa: ANN001  # tracked: #288
):
    if shutil.which("go") is None:
        pytest.skip("Go is required by the TBB ordered-map baseline")

    evaluator = Path(__file__).parents[2] / "examples" / "evaluators" / "ordered-map"
    completed = subprocess.run(  # noqa: S603  # tracked: #288
        [  # noqa: S607  # tracked: #288
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
