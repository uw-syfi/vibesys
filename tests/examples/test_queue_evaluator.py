from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Iterator  # noqa: TC003  # tracked: #288
from pathlib import Path
from typing import Any

import pytest

QUEUE_TASKS = {
    "spsc": "spsc",
    "mpsc": "mpsc",
    "mpmc": "mpmc",
}

QUEUE_ACCURACY_SETTINGS = {
    "spsc": ("32", "100"),
    "mpsc": ("24", "50"),
    "mpmc": ("24", "100"),
}


def _queue_repository(project_root: Path) -> Path:
    return project_root / "examples" / "data-structures" / "repositories" / "queue-rs"


def _queue_evaluator(project_root: Path) -> Path:
    return project_root / "resources" / "evaluators" / "queue"


def _task_manifest(repository: Path, task_name: str) -> dict[str, Any]:
    manifest_path = repository / ".vibesys" / "tasks" / task_name / "vibesys.input.toml"
    return tomllib.loads(manifest_path.read_text())


def _evaluator_command(
    evaluator: Path,
    manifest: dict[str, Any],
    section: str,
    project_root: Path,
) -> list[str]:
    package_metadata = tomllib.loads((evaluator / "vibesys.evaluator.toml").read_text())
    task_section = manifest[section]
    assert isinstance(task_section, dict)
    entrypoint = task_section["entrypoint"]
    args = task_section["args"]
    assert isinstance(entrypoint, str)
    assert isinstance(args, list)
    command = package_metadata["entrypoints"][entrypoint]
    return [part.replace("${PACKAGE_ROOT}", str(evaluator)) for part in command] + [
        part.replace("${PROJECT_ROOT}", str(project_root)) for part in args
    ]


@pytest.fixture(scope="session")
def compiled_queue_candidate(tmp_path_factory) -> Path:  # noqa: ANN001  # tracked: #288
    """Build the repository's shared Rust candidate once for evaluator tests."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted queue evaluator")

    project_root = Path(__file__).parents[2]
    repository = _queue_repository(project_root)
    build_dir = tmp_path_factory.mktemp("queue-rs-build") / "repository"
    shutil.copytree(repository, build_dir)
    subprocess.run(["make"], cwd=build_dir, check=True)  # noqa: S607  # tracked: #288

    candidate = build_dir / "queue-candidate.so"
    assert candidate.is_file()
    return candidate


@pytest.fixture(scope="session")
def queue_native_runner(tmp_path_factory) -> Iterator[Path]:  # noqa: ANN001  # tracked: #288
    """Build the trusted evaluator runner once and reuse it across subprocesses."""
    if shutil.which("cargo") is None:
        pytest.skip("Rust is required by the trusted queue evaluator")

    project_root = Path(__file__).parents[2]
    source = _queue_evaluator(project_root) / "native_runner"
    target_dir = tmp_path_factory.mktemp("queue-native-runner") / "target"
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
    runner = target_dir / "release" / "vibesys-queue-native-runner"
    assert runner.is_file()

    environment = pytest.MonkeyPatch()
    environment.setenv("VIBESYS_QUEUE_NATIVE_RUNNER", str(runner))
    try:
        yield runner
    finally:
        environment.undo()


def test_queue_task_manifests_use_versioned_evaluator_entrypoint():  # noqa: ANN201  # tracked: #288
    project_root = Path(__file__).parents[2]
    repository = _queue_repository(project_root)

    for task_name, scenario in QUEUE_TASKS.items():
        manifest = _task_manifest(repository, task_name)
        operations, trials = QUEUE_ACCURACY_SETTINGS[task_name]
        expected_args = {
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
            "name": "vibesys-evaluator-queue",
            "version": "0.1.0",
        }
        for section, args in expected_args.items():
            assert manifest[section]["entrypoint"] == "vibesys-queue"
            assert manifest[section]["args"] == args
        # The benchmark speaks the evaluator result protocol, so it reports a
        # complete validated metric row instead of one scraped scalar.
        assert manifest["benchmark"]["result_protocol"] == 2
        assert "result" not in manifest["benchmark"]

    evaluator = _queue_evaluator(project_root)
    assert (evaluator / "DESIGN.md").exists()
    assert (evaluator / "CANDIDATE_CONTRACT.md").exists()
    assert (evaluator / "include" / "vibesys_queue_abi.h").exists()
    assert not (evaluator / "QUEUE_PROTOCOL.md").exists()
    old_core = project_root / "examples" / "libs" / "queue-input-core"
    assert not (old_core / "pyproject.toml").exists()
    assert not any(old_core.glob("src/queue_input_core/*.py"))


def test_queue_tasks_live_with_the_editable_repository():  # noqa: ANN201  # tracked: #288
    project_root = Path(__file__).parents[2]
    repository = _queue_repository(project_root)
    repository_files = [
        ".gitignore",
        "Cargo.toml",
        "Cargo.lock",
        "Makefile",
        "src/ffi.rs",
        "src/lib.rs",
    ]

    for relative in repository_files:
        assert (repository / relative).is_file()

    for task_name in QUEUE_TASKS:
        task_root = repository / ".vibesys" / "tasks" / task_name
        assert (task_root / "OBJECTIVE.md").is_file()
        assert (task_root / "README.md").is_file()
        assert (task_root / "vibesys.input.toml").is_file()

    assert not (project_root / "examples" / "starters" / "queue-rs").exists()
    for old_name in ("queue-spsc", "queue-mpsc", "queue-mpmc"):
        assert not (project_root / "examples" / "data-structures" / old_name).exists()


def test_native_runner_build_ignores_candidate_cargo_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compiled_queue_candidate: Path,
) -> None:
    go = shutil.which("go")
    if go is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    package = workspace / ".vibesys-evaluator-package"
    shutil.copytree(_queue_evaluator(project_root), package)
    shutil.copy2(compiled_queue_candidate, workspace / "queue-candidate.so")
    cargo_config = workspace / ".cargo" / "config.toml"
    cargo_config.parent.mkdir()
    cargo_config.write_text('[build]\nrustc-wrapper = "/candidate/missing-wrapper"\n')
    monkeypatch.delenv("VIBESYS_QUEUE_NATIVE_RUNNER", raising=False)

    completed = subprocess.run(  # noqa: S603
        [
            go,
            "-C",
            str(package),
            "run",
            ".",
            "check",
            "--workspace",
            str(workspace),
            "--scenario",
            "spsc",
            "--capacity",
            "4",
            "--value-size",
            "64",
            "--operations",
            "4",
            "--trials",
            "1",
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "GOWORK": "off"},
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr
    assert "PASS - spsc linearizable bounded FIFO" in completed.stdout


@pytest.mark.usefixtures("queue_native_runner")
def test_spsc_rigtorp_baseline_builds_and_passes_accuracy(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("c++") is None:
        pytest.skip("Go and a C++ compiler are required by the SPSC baseline")

    project_root = Path(__file__).parents[2]
    baseline_source = project_root / "examples" / "baselines" / "queue-spsc-rigtorp"
    baseline = tmp_path / "queue-spsc-rigtorp"
    shutil.copytree(baseline_source, baseline)
    evaluator = _queue_evaluator(project_root)
    abi_header = evaluator / "include" / "vibesys_queue_abi.h"

    subprocess.run(  # noqa: S603  # tracked: #288
        ["make", "clean", "all", f"ABI_HEADER={abi_header}"],  # noqa: S607  # tracked: #288
        cwd=baseline,
        check=True,
    )
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
            "spsc",
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
    assert "PASS - spsc linearizable" in completed.stdout


def test_spsc_rigtorp_baseline_uses_pinned_upstream_header():  # noqa: ANN201  # tracked: #288
    project_root = Path(__file__).parents[2]
    baseline = project_root / "examples" / "baselines" / "queue-spsc-rigtorp"
    upstream_header = baseline / "include" / "rigtorp" / "SPSCQueue.h"
    adapter = (baseline / "spsc_queue.cpp").read_text()

    assert hashlib.sha256(upstream_header.read_bytes()).hexdigest() == (
        "1e631ec9e8ba4955da5cac116620815055f7da0ea936bfdb3036c4e87bc8a6e8"
    )
    assert '#include "rigtorp/SPSCQueue.h"' in adapter
    assert "rigtorp::SPSCQueue<QueueEntry> entries" in adapter


@pytest.mark.usefixtures("queue_native_runner")
def test_mpmc_locked_ring_baseline_builds_and_passes_accuracy(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cc") is None:
        pytest.skip("Go and a C compiler are required by the MPMC baseline")

    project_root = Path(__file__).parents[2]
    baseline_source = project_root / "examples" / "baselines" / "queue-mpmc-locked-ring"
    baseline = tmp_path / "queue-mpmc-locked-ring"
    shutil.copytree(baseline_source, baseline)
    evaluator = _queue_evaluator(project_root)
    abi_header = evaluator / "include" / "vibesys_queue_abi.h"

    subprocess.run(  # noqa: S603  # tracked: #288
        ["make", "clean", "all", f"ABI_HEADER={abi_header}"],  # noqa: S607  # tracked: #288
        cwd=baseline,
        check=True,
    )
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
            "mpmc",
            "--capacity",
            "17",
            "--value-size",
            "64",
            "--operations",
            "16",
            "--trials",
            "12",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "PASS - mpmc reservation-aware bounded FIFO" in completed.stdout


def test_mpmc_locked_ring_baseline_has_atomic_publication_region():  # noqa: ANN201  # tracked: #288
    project_root = Path(__file__).parents[2]
    baseline = project_root / "examples" / "baselines" / "queue-mpmc-locked-ring"
    adapter = (baseline / "locked_ring.c").read_text()

    enqueue = adapter[adapter.index("vsq_status vsq_try_enqueue") :]
    enqueue = enqueue[: enqueue.index("vsq_status vsq_try_dequeue")]
    dequeue = adapter[adapter.index("vsq_status vsq_try_dequeue") :]
    assert "pthread_mutex_lock(&queue->lock)" in enqueue
    assert "memcpy(" in enqueue
    assert enqueue.index("pthread_mutex_lock") < enqueue.index("memcpy(")
    assert "pthread_mutex_lock(&queue->lock)" in dequeue
    assert "memcpy(" in dequeue
    assert dequeue.index("pthread_mutex_lock") < dequeue.index("memcpy(")


@pytest.mark.parametrize(("task_name", "scenario"), QUEUE_TASKS.items())
@pytest.mark.usefixtures("queue_native_runner")
def test_repository_candidate_passes_each_task_accuracy(  # noqa: ANN201  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
    task_name,  # noqa: ANN001  # tracked: #288
    scenario,  # noqa: ANN001  # tracked: #288
    compiled_queue_candidate,  # noqa: ANN001  # tracked: #288
):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    repository = _queue_repository(project_root)
    shutil.copytree(
        repository,
        workspace,
        ignore=shutil.ignore_patterns("queue-candidate.so", "target"),
    )

    shutil.copy2(compiled_queue_candidate, workspace / "queue-candidate.so")
    assert (workspace / "queue-candidate.so").is_file()

    manifest = _task_manifest(workspace, task_name)
    accuracy = [
        *_evaluator_command(_queue_evaluator(project_root), manifest, "accuracy", workspace),
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
    expected = ["spsc", "mpsc", "mpmc"] if scenario == "all" else [scenario]
    for checked_scenario in expected:
        contract = (
            "reservation-aware bounded FIFO"
            if checked_scenario == "mpmc"
            else "linearizable bounded FIFO"
        )
        assert f"PASS - {checked_scenario} {contract}" in completed.stdout


@pytest.mark.usefixtures("queue_native_runner")
def test_task_entrypoints_run_from_repository_root(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    project_root = Path(__file__).parents[2]
    workspace = tmp_path / "workspace"
    shutil.copytree(
        _queue_repository(project_root),
        workspace,
        ignore=shutil.ignore_patterns("queue-candidate.so", "target"),
    )
    subprocess.run(["make"], cwd=workspace, check=True)  # noqa: S607  # tracked: #288
    manifest = _task_manifest(workspace, "spsc")
    evaluator = _queue_evaluator(project_root)

    accuracy = [
        *_evaluator_command(evaluator, manifest, "accuracy", workspace),
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
        *_evaluator_command(evaluator, manifest, "benchmark", workspace),
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


@pytest.mark.usefixtures("queue_native_runner")
def test_queue_evaluator_rejects_adversarial_histories():  # noqa: ANN201  # tracked: #288
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    evaluator = _queue_evaluator(Path(__file__).parents[2])
    subprocess.run(["go", "test", "./..."], cwd=evaluator, check=True)  # noqa: S607  # tracked: #288
