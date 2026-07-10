from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

LINEARIZABLE_QUEUE_INPUTS = {
    "queue-spsc": "spsc",
    "queue-mpsc": "mpsc",
    "queue-mpmc": "mpmc",
}

LINEARIZABLE_ACCURACY_SETTINGS = {
    "queue-spsc": ("32", "100"),
    "queue-mpsc": ("24", "50"),
    "queue-mpmc": ("24", "100"),
}


def _copy_input_bundle(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".venv", "queue-candidate.so", "target"),
    )


def _materialize_linearizable_input(
    project_root: Path,
    input_name: str,
    workspace: Path,
) -> Path:
    from vibe_serve.input_manifest import load_input_bundle

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


def test_linearizable_queue_manifests_invoke_go_evaluator_directly():
    root = Path(__file__).parents[1] / "examples" / "data-structures"

    for input_name, scenario in LINEARIZABLE_QUEUE_INPUTS.items():
        manifest = tomllib.loads((root / input_name / "vibeserve.input.toml").read_text())
        operations, trials = LINEARIZABLE_ACCURACY_SETTINGS[input_name]
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
        assert manifest["evaluator"] == {"source": "../../evaluators/queue"}
        for section, expected_suffix in expected_suffixes.items():
            command = manifest[section]["command"]
            assert command[:3] == ["go", "-C", "_evaluator/queue"]
            assert command[3:] == expected_suffix
        assert manifest["benchmark"]["result"] == {
            "json_argument": "--output-json",
            "metric": "total_ops_per_sec",
        }

    evaluator = root.parents[0] / "evaluators" / "queue"
    assert (evaluator / "DESIGN.md").exists()
    assert (evaluator / "CANDIDATE_CONTRACT.md").exists()
    assert (evaluator / "include" / "vibeserve_queue_abi.h").exists()
    assert not (evaluator / "QUEUE_PROTOCOL.md").exists()
    old_core = root.parents[0] / "libs" / "queue-input-core"
    assert not (old_core / "pyproject.toml").exists()
    assert not any(old_core.glob("src/queue_input_core/*.py"))


def test_linearizable_queue_inputs_use_shared_editable_rust_starter():
    from vibe_serve.input_manifest import load_input_bundle

    project_root = Path(__file__).parents[1]
    root = project_root / "examples" / "data-structures"
    starter = project_root / "examples" / "starters" / "queue-rs"
    starter_files = [".gitignore", "Cargo.toml", "Cargo.lock", "Makefile", "src/lib.rs"]

    for relative in starter_files:
        assert (starter / relative).is_file()

    for input_name in LINEARIZABLE_QUEUE_INPUTS:
        input_dir = root / input_name
        bundle = load_input_bundle(input_dir, project_root=project_root)
        assert bundle.workspace_seed_path == starter.resolve()
        assert (
            bundle.evaluator_path == (project_root / "examples" / "evaluators" / "queue").resolve()
        )
        assert not (input_dir / "reference" / "reference.py").exists()
        assert not (input_dir / "pyproject.toml").exists()
        for relative in starter_files:
            assert not (input_dir / relative).exists()


@pytest.mark.parametrize(("input_name", "scenario"), LINEARIZABLE_QUEUE_INPUTS.items())
def test_materialized_rust_starter_builds_and_passes_accuracy(tmp_path, input_name, scenario):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    project_root = Path(__file__).parents[1]
    workspace = tmp_path / "workspace"
    _materialize_linearizable_input(project_root, input_name, workspace)

    subprocess.run(["make"], cwd=workspace, check=True)
    assert (workspace / "queue-candidate.so").is_file()
    rebuilt = subprocess.run(
        ["make"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "cargo build --release --locked" in rebuilt.stdout

    manifest = tomllib.loads((workspace / "vibeserve.input.toml").read_text())
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
    completed = subprocess.run(
        accuracy,
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    expected = ["spsc", "mpsc", "mpmc"] if scenario == "all" else [scenario]
    for checked_scenario in expected:
        assert f"PASS - {checked_scenario} linearizable" in completed.stdout


def test_materialized_manifest_commands_run_go_evaluator_directly(tmp_path):
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    project_root = Path(__file__).parents[1]
    workspace = tmp_path / "workspace"
    input_dir = _materialize_linearizable_input(
        project_root,
        "queue-spsc",
        workspace,
    )
    assert (workspace / "_evaluator" / "queue" / "DESIGN.md").is_file()
    subprocess.run(["make"], cwd=workspace, check=True)
    manifest = tomllib.loads((input_dir / "vibeserve.input.toml").read_text())

    accuracy = [
        *manifest["accuracy"]["command"],
        "--capacity",
        "4",
        "--operations",
        "12",
        "--trials",
        "1",
    ]
    subprocess.run(accuracy, cwd=workspace, check=True)

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
    subprocess.run(benchmark, cwd=workspace, check=True)
    results = json.loads(output.read_text())
    assert [result["scenario"] for result in results] == ["spsc"]
    assert all(result["repetitions"] == 3 for result in results)
    assert all(len(result["total_ops_per_sec_samples"]) == 3 for result in results)


def test_queue_evaluator_rejects_adversarial_histories():
    if shutil.which("go") is None or shutil.which("cargo") is None:
        pytest.skip("Go and Rust are required by the trusted queue evaluator")

    evaluator = Path(__file__).parents[1] / "examples" / "evaluators" / "queue"
    subprocess.run(["go", "test", "./..."], cwd=evaluator, check=True)
