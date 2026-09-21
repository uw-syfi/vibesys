from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Iterator  # noqa: TC003  # tracked: #288
from pathlib import Path

import pytest

STACK_INPUTS = {
    "stack-spsc": "spsc",
    "stack-mpsc": "mpsc",
    "stack-spmc": "spmc",
    "stack-mpmc": "mpmc",
}

STACK_ACCURACY_SETTINGS = {
    "stack-spsc": ("32", "100"),
    "stack-mpsc": ("24", "50"),
    "stack-spmc": ("24", "50"),
    "stack-mpmc": ("24", "100"),
}

# mpmc relaxes strict linearizability to tolerate reservation windows; the other
# scenarios are checked against the strict model.
STACK_CONTRACTS = {
    "spsc": "linearizable bounded stack",
    "mpsc": "linearizable bounded stack",
    "spmc": "linearizable bounded stack",
    "mpmc": "reservation-aware bounded stack",
}


def _boost_lockfree_is_available() -> bool:
    compiler = shutil.which("c++")
    if compiler is None:
        return False
    probe = subprocess.run(  # noqa: S603  # tracked: #288
        [compiler, "-fsyntax-only", "-x", "c++", "-"],
        input="#include <boost/lockfree/stack.hpp>\nint main() { return 0; }\n",
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def _copy_input_bundle(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "stack-candidate.so", "target"),
    )


def _materialize_stack_input(
    project_root: Path,
    input_name: str,
    workspace: Path,
) -> Path:
    from vibesys.input_manifest import load_input_bundle  # noqa: PLC0415  # tracked: #288

    input_dir = project_root / "examples" / "data-structures" / input_name
    starter = project_root / "examples" / "starters" / "stack-rs"
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
def compiled_stack_candidate(tmp_path_factory) -> Path:  # noqa: ANN001  # tracked: #288
    """Build the shared Rust starter once for materialized-input tests."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    starter = project_root / "examples" / "starters" / "stack-rs"
    build_dir = tmp_path_factory.mktemp("stack-rs-build") / "starter"
    _copy_input_bundle(starter, build_dir)
    subprocess.run(["make"], cwd=build_dir, check=True)  # noqa: S607  # tracked: #288

    candidate = build_dir / "stack-candidate.so"
    assert candidate.is_file()
    return candidate


@pytest.fixture(scope="session")
def stack_native_runner(tmp_path_factory) -> Iterator[Path]:  # noqa: ANN001  # tracked: #288
    """Build the trusted evaluator runner once and reuse it across subprocesses."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    source = project_root / "examples" / "evaluators" / "stack" / "native_runner"
    target_dir = tmp_path_factory.mktemp("stack-native-runner") / "target"
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
    runner = target_dir / "release" / "vibesys-stack-native-runner"
    assert runner.is_file()

    environment = pytest.MonkeyPatch()
    environment.setenv("VIBESYS_STACK_NATIVE_RUNNER", str(runner))
    try:
        yield runner
    finally:
        environment.undo()


def test_stack_manifests_invoke_go_evaluator_directly():  # noqa: ANN201  # tracked: #288
    root = Path(__file__).parents[2] / "examples" / "data-structures"

    for input_name, scenario in STACK_INPUTS.items():
        manifest = tomllib.loads((root / input_name / "vibesys.input.toml").read_text())
        operations, trials = STACK_ACCURACY_SETTINGS[input_name]
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
        assert manifest["evaluator"] == {"source": "../../evaluators/stack"}
        for section, expected_suffix in expected_suffixes.items():
            command = manifest[section]["command"]
            assert command[:3] == ["go", "-C", "_evaluator/stack"]
            assert command[3:] == expected_suffix
        assert manifest["benchmark"]["result"] == {
            "json_argument": "--output-json",
            "metric": "total_ops_per_sec",
        }

    evaluator = root.parents[0] / "evaluators" / "stack"
    assert (evaluator / "DESIGN.md").exists()
    assert (evaluator / "CANDIDATE_CONTRACT.md").exists()
    assert (evaluator / "include" / "vibesys_stack_abi.h").exists()


def test_stack_inputs_use_shared_editable_rust_starter():  # noqa: ANN201  # tracked: #288
    from vibesys.input_manifest import load_input_bundle  # noqa: PLC0415  # tracked: #288

    project_root = Path(__file__).parents[2]
    root = project_root / "examples" / "data-structures"
    starter = project_root / "examples" / "starters" / "stack-rs"
    evaluator = project_root / "examples" / "evaluators" / "stack"
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

    for input_name in STACK_INPUTS:
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
        pytest.skip("Rust is required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    starter = project_root / "examples" / "starters" / "stack-rs"
    build_dir = tmp_path / "starter"
    _copy_input_bundle(starter, build_dir)
    cargo_target = tmp_path / "cargo-target"
    subprocess.run(
        ["make"],  # noqa: S607  # tracked: #288
        cwd=build_dir,
        check=True,
        env=os.environ | {"CARGO_TARGET_DIR": str(cargo_target)},
    )
    assert (build_dir / "stack-candidate.so").is_file()
    built = cargo_target / "release"
    assert (built / "libstack_candidate.so").is_file() or (
        built / "libstack_candidate.dylib"
    ).is_file()
    assert not (build_dir / "target").exists()


def test_stack_benchmark_pins_linux_workers(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted stack evaluator")

    source = Path(__file__).parents[2] / "examples" / "evaluators" / "stack" / "native_runner"
    text = (source / "src" / "benchmark.rs").read_text()
    assert "fn pin_current_thread(worker_index: usize)" in text
    assert "sched_setaffinity" in text
    assert "configure_benchmark_thread(lane)" in text
    assert "configure_benchmark_thread(worker_index)" in text
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


@pytest.mark.parametrize(("input_name", "scenario"), STACK_INPUTS.items())
@pytest.mark.usefixtures("stack_native_runner")
def test_materialized_rust_starter_passes_accuracy(  # noqa: ANN201  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
    input_name,  # noqa: ANN001  # tracked: #288
    scenario,  # noqa: ANN001  # tracked: #288
    compiled_stack_candidate,  # noqa: ANN001  # tracked: #288
):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    _materialize_stack_input(project_root, input_name, workspace)

    candidate = workspace / "stack-candidate.so"
    shutil.copy2(compiled_stack_candidate, candidate)
    assert candidate.is_file()

    manifest = tomllib.loads((workspace / "vibesys.input.toml").read_text())
    accuracy = [
        *manifest["accuracy"]["command"],
        "--capacity",
        "4",
        "--value-size",
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
    assert f"PASS - {scenario} {STACK_CONTRACTS[scenario]}" in completed.stdout


@pytest.mark.usefixtures("stack_native_runner")
def test_materialized_manifest_commands_run_go_evaluator_directly(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    input_dir = _materialize_stack_input(
        project_root,
        "stack-spsc",
        workspace,
    )
    assert (workspace / "_evaluator" / "stack" / "DESIGN.md").is_file()
    subprocess.run(["make"], cwd=workspace, check=True)  # noqa: S607  # tracked: #288
    manifest = tomllib.loads((input_dir / "vibesys.input.toml").read_text())

    accuracy = [
        *manifest["accuracy"]["command"],
        "--capacity",
        "4",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    subprocess.run(accuracy, cwd=workspace, check=True)  # noqa: S603  # tracked: #288

    output = workspace / "results.json"
    benchmark = [
        *manifest["benchmark"]["command"],
        "--capacity",
        "4",
        "--duration",
        "20ms",
        "--warmup",
        "0s",
        "--output-json",
        str(output),
    ]
    subprocess.run(benchmark, cwd=workspace, check=True)  # noqa: S603  # tracked: #288
    results = json.loads(output.read_text())
    assert [result["scenario"] for result in results] == ["spsc"]
    assert all(result["repetitions"] == 3 for result in results)
    assert all(len(result["total_ops_per_sec_samples"]) == 3 for result in results)


@pytest.mark.usefixtures("stack_native_runner")
def test_stack_evaluator_rejects_adversarial_histories():  # noqa: ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted stack evaluator")

    evaluator = Path(__file__).parents[2] / "examples" / "evaluators" / "stack"
    subprocess.run(["go", "test", "./..."], cwd=evaluator, check=True)  # noqa: S607  # tracked: #288


def test_stack_boost_lockfree_baseline_uses_boost_stack():  # noqa: ANN201  # tracked: #288
    project_root = Path(__file__).parents[2]
    adapter = (
        project_root
        / "examples"
        / "baselines"
        / "stack-boost-lockfree"
        / "boost_lockfree_stack.cpp"
    ).read_text()
    assert "#include <boost/lockfree/stack.hpp>" in adapter
    assert "boost::lockfree::stack" in adapter
    assert "bounded_push" in adapter


@pytest.fixture(scope="session")
def built_stack_boost_lockfree(tmp_path_factory) -> Path:  # noqa: ANN001  # tracked: #288
    """Build the Boost.Lockfree stack baseline once against the evaluator ABI header."""
    if shutil.which("c++") is None:
        pytest.skip("A C++ compiler is required by the Boost stack baseline")
    if not _boost_lockfree_is_available():
        pytest.skip("Boost.Lockfree (libboost-dev) is required by the Boost stack baseline")

    project_root = Path(__file__).parents[2]
    evaluator = project_root / "examples" / "evaluators" / "stack"
    abi_header = evaluator / "include" / "vibesys_stack_abi.h"
    baseline = tmp_path_factory.mktemp("stack-boost-lockfree") / "baseline"
    shutil.copytree(
        project_root / "examples" / "baselines" / "stack-boost-lockfree",
        baseline,
        ignore=shutil.ignore_patterns("stack-candidate.so"),
    )
    subprocess.run(  # noqa: S603  # tracked: #288
        ["make", "clean", "all", f"ABI_HEADER={abi_header}"],  # noqa: S607  # tracked: #288
        cwd=baseline,
        check=True,
    )
    assert (baseline / "stack-candidate.so").is_file()
    return baseline


@pytest.mark.parametrize("scenario", list(STACK_INPUTS.values()))
@pytest.mark.usefixtures("stack_native_runner")
def test_stack_boost_lockfree_baseline_builds_and_passes_accuracy(  # noqa: ANN201  # tracked: #288
    scenario,  # noqa: ANN001  # tracked: #288
    built_stack_boost_lockfree,  # noqa: ANN001  # tracked: #288
):
    if shutil.which("go") is None:
        pytest.skip("Go is required by the Boost stack baseline")

    evaluator = Path(__file__).parents[2] / "examples" / "evaluators" / "stack"
    completed = subprocess.run(  # noqa: S603  # tracked: #288
        [  # noqa: S607  # tracked: #288
            "go",
            "-C",
            str(evaluator),
            "run",
            ".",
            "check",
            "--workspace",
            str(built_stack_boost_lockfree),
            "--scenario",
            scenario,
            "--capacity",
            "4",
            "--value-size",
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
    assert f"PASS - {scenario} {STACK_CONTRACTS[scenario]}" in completed.stdout
