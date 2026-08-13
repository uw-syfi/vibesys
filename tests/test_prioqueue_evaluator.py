from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator  # noqa: TC003  # tracked: #288
from pathlib import Path

import pytest

PRIORITY_QUEUE_INPUTS = {
    "prioqueue-spsc": "spsc",
    "prioqueue-mpsc": "mpsc",
    "prioqueue-spmc": "spmc",
    "prioqueue-mpmc": "mpmc",
}

PRIORITY_QUEUE_ACCURACY_SETTINGS = {
    "prioqueue-spsc": ("32", "100"),
    "prioqueue-mpsc": ("24", "50"),
    "prioqueue-spmc": ("24", "50"),
    "prioqueue-mpmc": ("24", "100"),
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


def _copy_input_bundle(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "priority-queue-candidate.so", "target"),
    )


def _materialize_priority_queue_input(
    project_root: Path,
    input_name: str,
    workspace: Path,
) -> Path:
    from vibesys.input_manifest import load_input_bundle  # noqa: PLC0415  # tracked: #288

    input_dir = project_root / "examples" / "data-structures" / input_name
    bundle = load_input_bundle(input_dir, project_root=project_root)
    assert bundle.workspace_seed_path is not None
    assert bundle.evaluator_path is not None
    _copy_input_bundle(bundle.workspace_seed_path, workspace)
    _copy_input_bundle(input_dir, workspace)
    _copy_input_bundle(
        bundle.evaluator_path,
        workspace / "_evaluator" / bundle.evaluator_path.name,
    )
    return input_dir


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
def compiled_priority_queue_candidate(tmp_path_factory) -> Path:  # noqa: ANN001  # tracked: #288
    """Build the shared Rust starter once for materialized-input tests."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted priority-queue evaluator")

    project_root = Path(__file__).parents[1]
    starter = project_root / "examples" / "starters" / "priority-queue-rs"
    build_dir = tmp_path_factory.mktemp("priority-queue-rs-build") / "starter"
    _copy_input_bundle(starter, build_dir)
    subprocess.run(["make"], cwd=build_dir, check=True)  # noqa: S607  # tracked: #288

    candidate = build_dir / "priority-queue-candidate.so"
    assert candidate.is_file()
    return candidate


@pytest.fixture(scope="session")
def priority_queue_native_runner(tmp_path_factory) -> Iterator[Path]:  # noqa: ANN001  # tracked: #288
    """Build the trusted evaluator runner once and reuse it across subprocesses."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted priority-queue evaluator")

    project_root = Path(__file__).parents[1]
    source = project_root / "examples" / "evaluators" / "priority-queue" / "native_runner"
    target_dir = tmp_path_factory.mktemp("priority-queue-native-runner") / "target"
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
    runner = target_dir / "release" / "vibesys-priority-queue-native-runner"
    assert runner.is_file()

    environment = pytest.MonkeyPatch()
    environment.setenv("VIBESYS_PRIORITY_QUEUE_NATIVE_RUNNER", str(runner))
    try:
        yield runner
    finally:
        environment.undo()


@pytest.fixture(scope="session")
def built_priority_queue_baselines(tmp_path_factory) -> dict[str, Path]:  # noqa: ANN001  # tracked: #288
    """Build each C baseline once against the evaluator ABI header."""
    if shutil.which("cc") is None:
        pytest.skip("A C compiler is required by the priority-queue baselines")

    project_root = Path(__file__).parents[1]
    evaluator = project_root / "examples" / "evaluators" / "priority-queue"
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
        subprocess.run(  # noqa: S603  # tracked: #288
            ["make", "clean", "all", f"ABI_HEADER={abi_header}"],  # noqa: S607  # tracked: #288
            cwd=baseline,
            check=True,
        )
        assert (baseline / "priority-queue-candidate.so").is_file()
        built[baseline_name] = baseline
    return built


def test_priority_queue_manifests_invoke_go_evaluator_directly():  # noqa: ANN201  # tracked: #288
    root = Path(__file__).parents[1] / "examples" / "data-structures"

    for input_name, scenario in PRIORITY_QUEUE_INPUTS.items():
        manifest = tomllib.loads((root / input_name / "vibesys.input.toml").read_text())
        operations, trials = PRIORITY_QUEUE_ACCURACY_SETTINGS[input_name]
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
        assert manifest["evaluator"] == {"source": "../../evaluators/priority-queue"}
        for section, expected_suffix in expected_suffixes.items():
            command = manifest[section]["command"]
            assert command[:3] == ["go", "-C", "_evaluator/priority-queue"]
            assert command[3:] == expected_suffix
        assert manifest["benchmark"]["result"] == {
            "json_argument": "--output-json",
            "metric": "total_ops_per_sec",
        }

    evaluator = root.parents[0] / "evaluators" / "priority-queue"
    assert (evaluator / "DESIGN.md").exists()
    assert (evaluator / "CANDIDATE_CONTRACT.md").exists()
    assert (evaluator / "include" / "vibesys_priority_queue_abi.h").exists()


def test_priority_queue_inputs_use_shared_editable_rust_starter():  # noqa: ANN201  # tracked: #288
    from vibesys.input_manifest import load_input_bundle  # noqa: PLC0415  # tracked: #288

    project_root = Path(__file__).parents[1]
    root = project_root / "examples" / "data-structures"
    starter = project_root / "examples" / "starters" / "priority-queue-rs"
    evaluator = project_root / "examples" / "evaluators" / "priority-queue"
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
        input_dir = root / input_name
        bundle = load_input_bundle(input_dir, project_root=project_root)
        assert bundle.workspace_seed_path == starter.resolve()
        assert bundle.evaluator_path == evaluator.resolve()
        assert not (input_dir / "baseline").exists()
        assert not (input_dir / "reference").exists()
        assert not (input_dir / "pyproject.toml").exists()
        for relative in starter_files:
            assert not (input_dir / relative).exists()


@pytest.mark.parametrize(("input_name", "scenario"), PRIORITY_QUEUE_INPUTS.items())
@pytest.mark.usefixtures("priority_queue_native_runner")
def test_materialized_rust_starter_passes_accuracy(  # noqa: ANN201  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
    input_name,  # noqa: ANN001  # tracked: #288
    scenario,  # noqa: ANN001  # tracked: #288
    compiled_priority_queue_candidate,  # noqa: ANN001  # tracked: #288
):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted priority-queue evaluator")

    project_root = Path(__file__).parents[1]
    workspace = tmp_path / "workspace"
    _materialize_priority_queue_input(project_root, input_name, workspace)

    candidate = workspace / "priority-queue-candidate.so"
    shutil.copy2(compiled_priority_queue_candidate, candidate)
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
    assert f"PASS - {scenario} {PRIORITY_QUEUE_CONTRACTS[scenario]}" in completed.stdout


@pytest.mark.usefixtures("priority_queue_native_runner")
def test_materialized_manifest_commands_run_go_evaluator_directly(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted priority-queue evaluator")

    project_root = Path(__file__).parents[1]
    workspace = tmp_path / "workspace"
    input_dir = _materialize_priority_queue_input(
        project_root,
        "prioqueue-spsc",
        workspace,
    )
    assert (workspace / "_evaluator" / "priority-queue" / "DESIGN.md").is_file()
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


@pytest.mark.usefixtures("priority_queue_native_runner")
def test_priority_queue_evaluator_rejects_adversarial_histories():  # noqa: ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted priority-queue evaluator")

    evaluator = Path(__file__).parents[1] / "examples" / "evaluators" / "priority-queue"
    subprocess.run(["go", "test", "./..."], cwd=evaluator, check=True)  # noqa: S607  # tracked: #288


@pytest.mark.parametrize("scenario", list(PRIORITY_QUEUE_INPUTS.values()))
@pytest.mark.parametrize("baseline_name", list(PRIORITY_QUEUE_BASELINES))
@pytest.mark.usefixtures("priority_queue_native_runner")
def test_c_baselines_build_and_pass_accuracy(  # noqa: ANN201  # tracked: #288
    baseline_name,  # noqa: ANN001  # tracked: #288
    scenario,  # noqa: ANN001  # tracked: #288
    built_priority_queue_baselines,  # noqa: ANN001  # tracked: #288
):
    if shutil.which("go") is None or shutil.which("cc") is None:
        pytest.skip("Go and a C compiler are required by the priority-queue baselines")

    evaluator = Path(__file__).parents[1] / "examples" / "evaluators" / "priority-queue"
    baseline = built_priority_queue_baselines[baseline_name]
    completed = subprocess.run(  # noqa: S603  # tracked: #288
        [  # noqa: S607  # tracked: #288
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


def test_prioqueue_locked_heap_baseline_copies_payloads_under_the_lock():  # noqa: ANN201  # tracked: #288
    project_root = Path(__file__).parents[1]
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


def test_prioqueue_sharded_heap_baseline_rechecks_priority_before_commit():  # noqa: ANN201  # tracked: #288
    project_root = Path(__file__).parents[1]
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
