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

STACK_INPUTS = {
    "spsc": "spsc",
    "mpsc": "mpsc",
    "spmc": "spmc",
    "mpmc": "mpmc",
}

STACK_ACCURACY_SETTINGS = {
    "spsc": ("32", "100"),
    "mpsc": ("24", "50"),
    "spmc": ("24", "50"),
    "mpmc": ("24", "100"),
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
    probe = subprocess.run(  # noqa: S603  # lint-waiver: LW-994616 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        [compiler, "-fsyntax-only", "-x", "c++", "-"],
        input="#include <boost/lockfree/stack.hpp>\nint main() { return 0; }\n",
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


CARGO_TARGET_DIRNAME = "cargo-target"


def _copy_input_bundle(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "stack-candidate.so", "target"),
    )


def _project(project_root: Path) -> Path:
    return project_root / "examples" / "data-structures" / "stack"


def _evaluator(project_root: Path) -> Path:
    return project_root / "resources" / "evaluators" / "stack"


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


def _materialize_stack_input(
    project_root: Path,
    task_name: str,
    workspace: Path,
) -> dict[str, Any]:
    """Copy the project into ``workspace`` and return the task's manifest."""
    _copy_input_bundle(_project(project_root), workspace)
    return _task_manifest(project_root, task_name)


@pytest.fixture(scope="session")
def compiled_stack_candidate(tmp_path_factory) -> Path:  # noqa: ANN001  # lint-waiver: LW-994618 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    """Build the shared Rust starter once for materialized-input tests."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    starter = project_root / "examples" / "data-structures" / "stack"

    def build(directory: Path) -> None:
        _copy_input_bundle(starter, directory / "starter")
        subprocess.run(
            ["make"],  # noqa: S607  # lint-waiver: LW-994619 [S607]; Executable name is a project tool resolved from PATH in tests.
            cwd=directory / "starter",
            check=True,
            env=fast_cargo_env(CARGO_TARGET_DIR=str(directory / CARGO_TARGET_DIRNAME)),
        )

    build_dir = shared_build(tmp_path_factory, "stack-rs-build", build) / "starter"

    candidate = build_dir / "stack-candidate.so"
    assert candidate.is_file()
    return candidate


@pytest.fixture(scope="session")
def stack_native_runner(tmp_path_factory) -> Iterator[Path]:  # noqa: ANN001  # lint-waiver: LW-994620 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    """Build the trusted evaluator runner once and reuse it across subprocesses."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    source = project_root / "resources" / "evaluators" / "stack" / "native_runner"

    def build(directory: Path) -> None:
        subprocess.run(  # noqa: S603  # lint-waiver: LW-994621 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
            [  # noqa: S607  # lint-waiver: LW-994622 [S607]; Executable name is a project tool resolved from PATH in tests.
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

    target_dir = shared_build(tmp_path_factory, "stack-native-runner", build) / "target"
    runner = target_dir / "release" / "vibesys-stack-native-runner"
    assert runner.is_file()

    environment = pytest.MonkeyPatch()
    environment.setenv("VIBESYS_STACK_NATIVE_RUNNER", str(runner))
    try:
        yield runner
    finally:
        environment.undo()


def test_stack_manifests_invoke_go_evaluator_directly():  # noqa: ANN201  # lint-waiver: LW-994623 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    project_root = Path(__file__).parents[2]

    for input_name, scenario in STACK_INPUTS.items():
        manifest = _task_manifest(project_root, input_name)
        operations, trials = STACK_ACCURACY_SETTINGS[input_name]
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
        assert manifest["evaluator"] == {"name": "vibesys-evaluator-stack", "version": "0.1.0"}
        for section, expected_args in expected_suffixes.items():
            assert manifest[section]["entrypoint"] == "vibesys-stack"
            assert manifest[section]["args"] == expected_args
        assert manifest["benchmark"]["result"] == {
            "json_argument": "--output-json",
            "metric": "total_ops_per_sec",
        }

    evaluator = _evaluator(project_root)
    assert (evaluator / "DESIGN.md").exists()
    assert (evaluator / "CANDIDATE_CONTRACT.md").exists()
    assert (evaluator / "include" / "vibesys_stack_abi.h").exists()


def test_stack_inputs_use_shared_editable_rust_starter():  # noqa: ANN201  # lint-waiver: LW-994624 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    project_root = Path(__file__).parents[2]
    starter = _project(project_root)
    project = Project.open(starter)
    evaluator = project_root / "resources" / "evaluators" / "stack"
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


def test_starter_make_honors_cargo_target_dir(compiled_stack_candidate: Path) -> None:
    # The shared starter build ran ``make`` with CARGO_TARGET_DIR set, so its
    # layout is the observable effect of the Makefile honoring that variable.
    build_dir = compiled_stack_candidate.parent
    built = build_dir.parent / CARGO_TARGET_DIRNAME / "release"
    assert (build_dir / "stack-candidate.so").is_file()
    assert (built / "libstack_candidate.so").is_file() or (
        built / "libstack_candidate.dylib"
    ).is_file()
    assert not (build_dir / "target").exists()


def test_stack_benchmark_pins_linux_workers(tmp_path):  # noqa: ANN001, ANN201  # lint-waiver: LW-994628 [ANN001, ANN201]; Pytest fixture argument and unused test return are left unannotated.
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted stack evaluator")

    source = Path(__file__).parents[2] / "resources" / "evaluators" / "stack" / "native_runner"
    text = (source / "src" / "benchmark.rs").read_text()
    assert "fn pin_current_thread(worker_index: usize)" in text
    assert "sched_setaffinity" in text
    assert "configure_benchmark_thread(lane)" in text
    assert "configure_benchmark_thread(worker_index)" in text
    completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-994629 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        [  # noqa: S607  # lint-waiver: LW-994630 [S607]; Executable name is a project tool resolved from PATH in tests.
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
def test_materialized_rust_starter_passes_accuracy(  # noqa: ANN201  # lint-waiver: LW-994631 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    tmp_path,  # noqa: ANN001  # lint-waiver: LW-994632 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    input_name,  # noqa: ANN001  # lint-waiver: LW-994633 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    scenario,  # noqa: ANN001  # lint-waiver: LW-994634 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    compiled_stack_candidate,  # noqa: ANN001  # lint-waiver: LW-994635 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    manifest = _materialize_stack_input(project_root, input_name, workspace)

    candidate = workspace / "stack-candidate.so"
    shutil.copy2(compiled_stack_candidate, candidate)
    assert candidate.is_file()

    accuracy = [
        *_evaluator_command(project_root, manifest, "accuracy", workspace),
        "--capacity",
        "4",
        "--value-size",
        "64",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-994636 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        accuracy,
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"PASS - {scenario} {STACK_CONTRACTS[scenario]}" in completed.stdout


@pytest.mark.usefixtures("stack_native_runner")
def test_materialized_manifest_commands_run_go_evaluator_directly(
    tmp_path: Path, compiled_stack_candidate: Path
) -> None:
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted stack evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    manifest = _materialize_stack_input(
        project_root,
        "spsc",
        workspace,
    )
    shutil.copy2(compiled_stack_candidate, workspace / "stack-candidate.so")

    accuracy = [
        *_evaluator_command(project_root, manifest, "accuracy", workspace),
        "--capacity",
        "4",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    subprocess.run(accuracy, cwd=workspace, check=True)  # noqa: S603  # lint-waiver: LW-994639 [S603]; Subprocess argv is a fixed trusted build or evaluator command.

    output = workspace / "results.json"
    benchmark = [
        *_evaluator_command(project_root, manifest, "benchmark", workspace),
        "--capacity",
        "4",
        "--duration",
        "20ms",
        "--warmup",
        "0s",
        "--output-json",
        str(output),
    ]
    subprocess.run(benchmark, cwd=workspace, check=True)  # noqa: S603  # lint-waiver: LW-994640 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
    results = json.loads(output.read_text())
    assert [result["scenario"] for result in results] == ["spsc"]
    assert all(result["repetitions"] == 3 for result in results)
    assert all(len(result["total_ops_per_sec_samples"]) == 3 for result in results)


@pytest.mark.usefixtures("stack_native_runner")
def test_stack_evaluator_rejects_adversarial_histories():  # noqa: ANN201  # lint-waiver: LW-994641 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted stack evaluator")

    evaluator = Path(__file__).parents[2] / "resources" / "evaluators" / "stack"
    subprocess.run(["go", "test", "./..."], cwd=evaluator, check=True)  # noqa: S607  # lint-waiver: LW-994642 [S607]; Executable name is a project tool resolved from PATH in tests.


def test_stack_boost_lockfree_baseline_uses_boost_stack():  # noqa: ANN201  # lint-waiver: LW-994643 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
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
def built_stack_boost_lockfree(tmp_path_factory) -> Path:  # noqa: ANN001  # lint-waiver: LW-994644 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    """Build the Boost.Lockfree stack baseline once against the evaluator ABI header."""
    if shutil.which("c++") is None:
        pytest.skip("A C++ compiler is required by the Boost stack baseline")
    if not _boost_lockfree_is_available():
        pytest.skip("Boost.Lockfree (libboost-dev) is required by the Boost stack baseline")

    project_root = Path(__file__).parents[2]
    evaluator = project_root / "resources" / "evaluators" / "stack"
    abi_header = evaluator / "include" / "vibesys_stack_abi.h"
    baseline = tmp_path_factory.mktemp("stack-boost-lockfree") / "baseline"
    shutil.copytree(
        project_root / "examples" / "baselines" / "stack-boost-lockfree",
        baseline,
        ignore=shutil.ignore_patterns("stack-candidate.so"),
    )
    subprocess.run(  # noqa: S603  # lint-waiver: LW-994645 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        ["make", "clean", "all", f"ABI_HEADER={abi_header}"],  # noqa: S607  # lint-waiver: LW-994646 [S607]; Executable name is a project tool resolved from PATH in tests.
        cwd=baseline,
        check=True,
    )
    assert (baseline / "stack-candidate.so").is_file()
    return baseline


@pytest.mark.parametrize("scenario", list(STACK_INPUTS.values()))
@pytest.mark.usefixtures("stack_native_runner")
def test_stack_boost_lockfree_baseline_builds_and_passes_accuracy(  # noqa: ANN201  # lint-waiver: LW-994647 [ANN201]; Test return type is unused and left unannotated for the pytest runner.
    scenario,  # noqa: ANN001  # lint-waiver: LW-994648 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
    built_stack_boost_lockfree,  # noqa: ANN001  # lint-waiver: LW-994649 [ANN001]; Pytest fixture argument is untyped because the fixture plugin supplies it.
):
    if shutil.which("go") is None:
        pytest.skip("Go is required by the Boost stack baseline")

    evaluator = Path(__file__).parents[2] / "resources" / "evaluators" / "stack"
    completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-994650 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        [  # noqa: S607  # lint-waiver: LW-994651 [S607]; Executable name is a project tool resolved from PATH in tests.
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
