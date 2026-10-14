from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.support import run_test_command
from tests.support.fast_cargo import fast_cargo_env

from vibesys.inputs import load_project_task
from vs_project.api import Project

if TYPE_CHECKING:
    from collections.abc import Iterator

PRIORITY_QUEUE_INPUTS = {
    "spsc": "spsc",
    "mpsc": "mpsc",
    "spmc": "spmc",
    "mpmc": "mpmc",
}

PRIORITY_QUEUE_ACCURACY_SETTINGS = {
    "spsc": ("32", "100"),
    "mpsc": ("24", "50"),
    "spmc": ("24", "50"),
    "mpmc": ("24", "100"),
}

# mpmc relaxes strict linearizability to tolerate reservation windows; the other
# scenarios are checked against the strict model.
PRIORITY_QUEUE_CONTRACTS = {
    "spsc": "linearizable bounded priority queue",
    "mpsc": "linearizable bounded priority queue",
    "spmc": "linearizable bounded priority queue",
    "mpmc": "reservation-aware bounded priority queue",
}

PRIORITY_QUEUE_BASELINES = {
    "prioqueue-locked-heap": "locked_heap.c",
    "prioqueue-sharded-heap": "sharded_heap.c",
}


CARGO_TARGET_DIRNAME = "cargo-target"


def _copy_input_bundle(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "priority-queue-candidate.so", "target"),
    )


def _project(project_root: Path) -> Path:
    return project_root / "examples" / "data-structures" / "prioqueue"


def _evaluator(project_root: Path) -> Path:
    return project_root / "resources" / "evaluators" / "priority-queue"


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


def _materialize_priority_queue_input(
    project_root: Path,
    task_name: str,
    workspace: Path,
) -> dict[str, Any]:
    """Copy the project into ``workspace`` and return the task's manifest."""
    _copy_input_bundle(_project(project_root), workspace)
    return _task_manifest(project_root, task_name)


def _normalized_source(path: Path) -> str:
    """Strip C comments and collapse whitespace so assertions survive reformatting.

    Comments are dropped first so prose describing an invariant can never satisfy
    an assertion about the code that enforces it.
    """
    without_comments = re.sub(r"/\*.*?\*/", " ", path.read_text(), flags=re.DOTALL)
    return re.sub(r"\s+", " ", without_comments)


def _function_body(source: str, signature: str, *, until: str | None = None) -> str:
    body = source[source.index(signature) :]
    if until is not None:
        body = body[: body.index(until)]
    return body


@pytest.fixture(scope="session")
def compiled_priority_queue_candidate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the shared Rust starter once for materialized-input tests."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted priority-queue evaluator")

    project_root = Path(__file__).parents[2]
    starter = project_root / "examples" / "data-structures" / "prioqueue"
    build_dir = tmp_path_factory.mktemp("priority-queue-rs-build") / "starter"
    _copy_input_bundle(starter, build_dir)
    run_test_command(
        ["make"],
        cwd=build_dir,
        check=True,
        env=fast_cargo_env(CARGO_TARGET_DIR=str(build_dir.parent / CARGO_TARGET_DIRNAME)),
    )

    candidate = build_dir / "priority-queue-candidate.so"
    assert candidate.is_file()
    return candidate


@pytest.fixture(scope="session")
def priority_queue_native_runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Build the trusted evaluator runner once and reuse it across subprocesses."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted priority-queue evaluator")

    project_root = Path(__file__).parents[2]
    source = project_root / "resources" / "evaluators" / "priority-queue" / "native_runner"
    target_dir = tmp_path_factory.mktemp("priority-queue-native-runner") / "target"
    run_test_command(
        [
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
        env=fast_cargo_env(),
    )
    runner = target_dir / "release" / "vibesys-priority-queue-native-runner"
    assert runner.is_file()

    environment = pytest.MonkeyPatch()
    environment.setenv("VIBESYS_PRIORITY_QUEUE_NATIVE_RUNNER", str(runner))
    try:
        yield runner
    finally:
        environment.undo()


@pytest.fixture(scope="session")
def built_priority_queue_baselines(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build each C baseline once against the evaluator ABI header."""
    if shutil.which("cc") is None:
        pytest.skip("A C compiler is required by the priority-queue baselines")

    project_root = Path(__file__).parents[2]
    evaluator = project_root / "resources" / "evaluators" / "priority-queue"
    abi_header = evaluator / "include" / "vibesys_priority_queue_abi.h"
    build_root = tmp_path_factory.mktemp("priority-queue-baselines")

    built: dict[str, Path] = {}
    for baseline_name in PRIORITY_QUEUE_BASELINES:
        baseline = build_root / baseline_name
        shutil.copytree(
            project_root / "examples" / "baselines" / baseline_name,
            baseline,
            ignore=shutil.ignore_patterns("priority-queue-candidate.so"),
        )
        run_test_command(
            ["make", "clean", "all", f"ABI_HEADER={abi_header}"],
            cwd=baseline,
            check=True,
        )
        assert (baseline / "priority-queue-candidate.so").is_file()
        built[baseline_name] = baseline
    return built


def test_priority_queue_manifests_invoke_go_evaluator_directly() -> None:
    project_root = Path(__file__).parents[2]

    for input_name, scenario in PRIORITY_QUEUE_INPUTS.items():
        manifest = _task_manifest(project_root, input_name)
        operations, trials = PRIORITY_QUEUE_ACCURACY_SETTINGS[input_name]
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
            "name": "vibesys-evaluator-priority-queue",
            "version": "0.1.0",
        }
        for section, expected_args in expected_suffixes.items():
            assert manifest[section]["entrypoint"] == "vibesys-priority-queue"
            assert manifest[section]["args"] == expected_args
        assert manifest["benchmark"]["result"] == {
            "json_argument": "--output-json",
            "metric": "total_ops_per_sec",
        }

    evaluator = _evaluator(project_root)
    assert (evaluator / "DESIGN.md").exists()
    assert (evaluator / "CANDIDATE_CONTRACT.md").exists()
    assert (evaluator / "include" / "vibesys_priority_queue_abi.h").exists()


def test_priority_queue_inputs_use_shared_editable_rust_starter() -> None:

    project_root = Path(__file__).parents[2]
    starter = _project(project_root)
    project = Project.open(starter)
    evaluator = project_root / "resources" / "evaluators" / "priority-queue"
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

    for input_name in PRIORITY_QUEUE_INPUTS:
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


def test_starter_make_honors_cargo_target_dir(compiled_priority_queue_candidate: Path) -> None:
    # The shared starter build ran ``make`` with CARGO_TARGET_DIR set, so its
    # layout is the observable effect of the Makefile honoring that variable.
    build_dir = compiled_priority_queue_candidate.parent
    built = build_dir.parent / CARGO_TARGET_DIRNAME / "release"
    assert (build_dir / "priority-queue-candidate.so").is_file()
    assert (built / "libpriority_queue_candidate.so").is_file() or (
        built / "libpriority_queue_candidate.dylib"
    ).is_file()
    assert not (build_dir / "target").exists()


@pytest.mark.native_build
def test_priority_queue_benchmark_pins_linux_workers(tmp_path):  # noqa: ANN001, ANN201  # lint-waiver: LW-994691 [ANN001, ANN201]; Pytest fixture argument and unused test return are left unannotated.
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted priority-queue evaluator")

    source = (
        Path(__file__).parents[2] / "resources" / "evaluators" / "priority-queue" / "native_runner"
    )
    text = (source / "src" / "benchmark.rs").read_text()
    assert "fn pin_current_thread(worker_index: usize)" in text
    assert "sched_setaffinity" in text
    assert "configure_benchmark_thread(lane)" in text
    assert "configure_benchmark_thread(worker_index)" in text
    completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-994692 [S603]; Subprocess argv is a fixed trusted build or evaluator command.
        [  # noqa: S607  # lint-waiver: LW-994693 [S607]; Executable name is a project tool resolved from PATH in tests.
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


@pytest.mark.parametrize(("input_name", "scenario"), PRIORITY_QUEUE_INPUTS.items())
@pytest.mark.usefixtures("priority_queue_native_runner")
def test_materialized_rust_starter_passes_accuracy(
    tmp_path: Path,
    input_name: str,
    scenario: str,
    compiled_priority_queue_candidate: Path,
) -> None:
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted priority-queue evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    manifest = _materialize_priority_queue_input(project_root, input_name, workspace)

    candidate = workspace / "priority-queue-candidate.so"
    shutil.copy2(compiled_priority_queue_candidate, candidate)
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
    completed = run_test_command(
        accuracy,
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"PASS - {scenario} {PRIORITY_QUEUE_CONTRACTS[scenario]}" in completed.stdout


@pytest.mark.usefixtures("priority_queue_native_runner")
def test_materialized_manifest_commands_run_go_evaluator_directly(
    tmp_path: Path, compiled_priority_queue_candidate: Path
) -> None:
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted priority-queue evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    manifest = _materialize_priority_queue_input(
        project_root,
        "spsc",
        workspace,
    )
    shutil.copy2(compiled_priority_queue_candidate, workspace / "priority-queue-candidate.so")

    accuracy = [
        *_evaluator_command(project_root, manifest, "accuracy", workspace),
        "--capacity",
        "4",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    run_test_command(accuracy, cwd=workspace, check=True)

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
    run_test_command(benchmark, cwd=workspace, check=True)
    results = json.loads(output.read_text())
    assert [result["scenario"] for result in results] == ["spsc"]
    assert all(result["repetitions"] == 3 for result in results)
    assert all(len(result["total_ops_per_sec_samples"]) == 3 for result in results)


@pytest.mark.usefixtures("priority_queue_native_runner")
def test_priority_queue_evaluator_rejects_adversarial_histories() -> None:
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted priority-queue evaluator")

    evaluator = Path(__file__).parents[2] / "resources" / "evaluators" / "priority-queue"
    run_test_command(["go", "test", "./..."], cwd=evaluator, check=True)


@pytest.mark.parametrize("scenario", list(PRIORITY_QUEUE_INPUTS.values()))
@pytest.mark.parametrize("baseline_name", list(PRIORITY_QUEUE_BASELINES))
@pytest.mark.usefixtures("priority_queue_native_runner")
def test_c_baselines_build_and_pass_accuracy(
    baseline_name: str,
    scenario: str,
    built_priority_queue_baselines: dict[str, Path],
) -> None:
    if shutil.which("go") is None or shutil.which("cc") is None:
        pytest.skip("Go and a C compiler are required by the priority-queue baselines")

    evaluator = Path(__file__).parents[2] / "resources" / "evaluators" / "priority-queue"
    baseline = built_priority_queue_baselines[baseline_name]
    completed = run_test_command(
        [
            "go",
            "-C",
            str(evaluator),
            "run",
            ".",
            "check",
            "--workspace",
            str(baseline),
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
    assert f"PASS - {scenario} {PRIORITY_QUEUE_CONTRACTS[scenario]}" in completed.stdout


def test_prioqueue_locked_heap_baseline_copies_payloads_under_the_lock() -> None:
    project_root = Path(__file__).parents[2]
    baseline = project_root / "examples" / "baselines" / "prioqueue-locked-heap"
    source = _normalized_source(baseline / "locked_heap.c")

    enqueue = _function_body(
        source,
        "vspq_status vspq_try_enqueue",
        until="vspq_status vspq_try_dequeue",
    )
    dequeue = _function_body(source, "vspq_status vspq_try_dequeue")
    for region in (enqueue, dequeue):
        assert "pthread_mutex_lock(&queue->lock)" in region
        assert "memcpy(" in region
        # The payload copy must sit strictly between acquiring and releasing the
        # lock, so a reader never observes a half-written slot.
        assert region.index("pthread_mutex_lock") < region.index("memcpy(")
        assert region.index("memcpy(") < region.rindex("pthread_mutex_unlock")


def test_prioqueue_sharded_heap_baseline_rechecks_priority_before_commit() -> None:
    project_root = Path(__file__).parents[2]
    baseline = project_root / "examples" / "baselines" / "prioqueue-sharded-heap"
    dequeue = _function_body(
        _normalized_source(baseline / "sharded_heap.c"),
        "vspq_status vspq_try_dequeue",
    )

    # The lock-free scan records the best shard and the runner-up priority it
    # lost to, then re-locks the winner. Committing without re-comparing against
    # the runner-up would let a consumer pop an entry another shard now beats.
    assert "uint64_t runner_up_priority = UINT64_MAX;" in dequeue
    assert "runner_up_priority = best_priority;" in dequeue

    guard = "if (shard->count == 0 || shard->entries[0].priority > runner_up_priority)"
    commit = "const size_t slot = shard->entries[0].slot;"
    assert guard in dequeue
    assert commit in dequeue
    assert dequeue.index(guard) < dequeue.index(commit)

    # A failed re-check releases the shard and restarts the scan instead of
    # falling through to the pop.
    guarded = dequeue[dequeue.index(guard) : dequeue.index(commit)]
    assert "pthread_mutex_unlock(&shard->lock);" in guarded
    assert "continue;" in guarded
