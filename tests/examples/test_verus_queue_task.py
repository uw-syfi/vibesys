from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def _task_root() -> Path:
    project_root = Path(__file__).parents[2]
    return (
        project_root
        / "examples"
        / "data-structures"
        / "repositories"
        / "queue-rs"
        / ".vibesys"
        / "tasks"
        / "verus-mpmc-open"
    )


def _copy_verus_task(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_project = _task_root().parents[2]
    project = tmp_path / "queue-rs"
    task = project / ".vibesys" / "tasks" / "verus-mpmc-open"
    candidate = project / "verus-mpmc"
    task.mkdir(parents=True)
    shutil.copy2(_task_root() / "runner.py", task / "runner.py")
    shutil.copytree(
        source_project / "verus-mpmc",
        candidate,
        ignore=shutil.ignore_patterns("target"),
    )
    return project, task, candidate


def test_open_verus_mpmc_task_uses_pure_rust_runner() -> None:
    task = _task_root()
    manifest = tomllib.loads((task / "vibesys.input.toml").read_text(encoding="utf-8"))

    assert manifest["agent"] == {"domain": "generic"}
    assert "evaluator" not in manifest
    assert manifest["accuracy"] == {
        "command": ["python3", ".vibesys/tasks/verus-mpmc-open/runner.py", "check"],
        "timeout_seconds": 300,
    }
    assert manifest["benchmark"]["command"] == [
        "python3",
        ".vibesys/tasks/verus-mpmc-open/runner.py",
        "benchmark",
    ]
    assert manifest["benchmark"]["result"] == {
        "json_argument": "--output-json",
        "metric": "total_ops_per_sec",
    }

    runner = (task / "runner.py").read_text(encoding="utf-8")
    ast.parse(runner)
    assert '"cargo",\n            "verus",\n            "verify"' in runner
    assert 'subparsers.add_parser("check-fixture")' in runner
    assert '"--num-threads",\n            "1"' in runner
    assert "FORBIDDEN_SOURCE_PATTERNS" in runner
    assert "FIXED_CANDIDATE_FILES" in runner
    assert "implementer modified fixed candidate file" in runner
    assert "package.metadata.verus.verify = true" in runner
    assert "queue-candidate.so" not in runner

    dockerfile = (task / "Dockerfile").read_text(encoding="utf-8")
    assert "VERUS_PLATFORM=linux/amd64" in dockerfile
    assert "ubuntu:24.04@sha256:" in dockerfile
    assert "git python3" in dockerfile
    assert "VERUS_VERSION=0.2026.08.30.b432e82" in dockerfile
    assert "VERUS_SHA256=067f5f72a457fe66b77c0c10b180f2a" in dockerfile
    assert "RUST_TOOLCHAIN=1.97.1-x86_64-unknown-linux-gnu" in dockerfile
    assert "ln -s /opt/cargo/bin/cargo /usr/local/bin/cargo" in dockerfile
    assert "ln -s /opt/verus/verus /usr/local/bin/verus" in dockerfile


def test_open_verus_mpmc_contract_is_exact_fifo() -> None:
    task = _task_root()
    objective = (task / "OBJECTIVE.md").read_text(encoding="utf-8")
    accuracy = (task / "accuracy" / "src" / "main.rs").read_text(encoding="utf-8")
    benchmark = (task / "benchmark" / "src" / "main.rs").read_text(encoding="utf-8")

    assert "exact linearizable bounded-FIFO semantics" in objective
    assert "does not permit capacity" in objective
    assert "reservation before publication" in objective
    assert "pub fn enqueue(&self, value: T) -> Result<(), T>" in objective
    assert "pub fn dequeue(&self) -> Option<T>" in objective
    assert "queue_verus_mpmc::MpmcFifo" in accuracy
    assert "producer_order_contract" in accuracy
    assert "consumer_conservation_contract" in accuracy
    assert "total_ops_per_sec=" not in accuracy
    assert "queue_verus_mpmc::MpmcFifo" in benchmark
    assert "total_ops_per_sec=" in benchmark
    assert "producer_order_contract" not in benchmark
    assert not any(path.is_file() for path in (task / "harness").rglob("*"))


def test_open_verus_mpmc_fixed_api_does_not_choose_runtime_synchronization() -> None:
    api = (_task_root().parents[2] / "verus-mpmc" / "src" / "api.rs").read_text(encoding="utf-8")

    assert "AtomicUpdate" in api
    assert "QueueOperations<T>>::enqueue_op" in api
    assert "QueueOperations<T>>::dequeue_op" in api
    assert "QueueOperations<T>>::len_op" in api
    for runtime_primitive in (
        "RwLock",
        "Mutex",
        "PAtomic",
        "AtomicBool",
        "AtomicPtr",
        "AtomicU64",
        "AtomicUsize",
        "UnsafeCell",
        "Arc<",
        "atomic_with_ghost!",
        "std::sync",
        "vstd::rwlock",
    ):
        assert runtime_primitive not in api


def test_open_verus_mpmc_acceptance_fixture_moves_linearization_points() -> None:
    project = _task_root().parents[2]
    seed = (project / "verus-mpmc" / "src" / "candidate" / "mod.rs").read_text(encoding="utf-8")
    fixture = (
        _task_root() / "acceptance" / "alternate-lp" / "src" / "candidate" / "mod.rs"
    ).read_text(encoding="utf-8")

    enqueue_update = "state.auth.borrow_mut().update("
    enqueue_mutation = "state.entries.push_back(value);"
    assert seed.index(enqueue_mutation) < seed.index(enqueue_update)
    assert fixture.index(enqueue_update) < fixture.index(enqueue_mutation)

    fixture_dequeue = fixture[fixture.index("fn dequeue_op(") :]
    assert fixture_dequeue.index(enqueue_update) < fixture_dequeue.index(
        "state.entries.pop_front();"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        ".gitignore",
        "Cargo.lock",
        "Cargo.toml",
        "README.md",
        "src/lib.rs",
        "src/contract.rs",
        "src/api.rs",
    ],
)
def test_open_verus_mpmc_rejects_fixed_file_changes(tmp_path: Path, relative_path: str) -> None:
    _, task, candidate = _copy_verus_task(tmp_path)

    fixed_file = candidate / relative_path
    fixed_file.write_bytes(fixed_file.read_bytes() + b"\n")
    completed = subprocess.run(  # noqa: S603 - executes a copied repository script
        [sys.executable, str(task / "runner.py"), "check"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 1
    assert f"implementer modified fixed candidate file: {relative_path}" in completed.stdout


@pytest.mark.parametrize(
    "relative_path",
    ["build.rs", ".cargo/config.toml", "src/alternate.rs", "notes.txt"],
)
def test_open_verus_mpmc_rejects_files_outside_candidate(
    tmp_path: Path, relative_path: str
) -> None:
    project, task, candidate = _copy_verus_task(tmp_path)
    unexpected = candidate / relative_path
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_text("fn main() {}\n", encoding="utf-8")

    completed = subprocess.run(  # noqa: S603 - executes a copied repository script
        [sys.executable, str(task / "runner.py"), "check"],
        cwd=project,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 1
    assert "outside src/candidate" in completed.stdout


@pytest.mark.parametrize(
    ("source", "description"),
    [
        ("assume (false);", "assume"),
        ("admit (false);", "admit"),
        ("#[verifier :: external_body] fn hidden() {}", "external_body"),
        ("#[verifier :: external] fn hidden() {}", "verifier::external"),
        ('include ! ("other.rs");', "include macro"),
        ('#[path = "other.rs"] mod hidden;', "path attribute"),
        ("#[cfg (not(verus_keep_ghost))] fn split() {}", "cfg attribute"),
        ("const SPLIT: bool = cfg ! (verus_keep_ghost);", "cfg macro"),
    ],
)
def test_open_verus_mpmc_rejects_candidate_source_tricks(
    tmp_path: Path, source: str, description: str
) -> None:
    project, task, candidate = _copy_verus_task(tmp_path)
    (candidate / "src" / "candidate" / "trick.rs").write_text(source, encoding="utf-8")

    completed = subprocess.run(  # noqa: S603 - executes a copied repository script
        [sys.executable, str(task / "runner.py"), "check"],
        cwd=project,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 1
    assert f"forbidden source construct '{description}'" in completed.stdout


def test_open_verus_mpmc_rejects_candidate_symlink(tmp_path: Path) -> None:
    project, task, candidate = _copy_verus_task(tmp_path)
    link = candidate / "src" / "candidate" / "linked.rs"
    link.symlink_to(candidate / "src" / "api.rs")

    completed = subprocess.run(  # noqa: S603 - executes a copied repository script
        [sys.executable, str(task / "runner.py"), "check"],
        cwd=project,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 1
    assert "candidate source tree contains a symlink" in completed.stdout


def test_open_verus_mpmc_readme_shows_vibesys_task_command() -> None:
    readme = (_task_root() / "README.md").read_text(encoding="utf-8")

    assert "vibesys --outer-loop agent" in readme
    assert "No separate `docker build`, `--docker`, or `--docker-image`" in readme
    assert "--project examples/data-structures/repositories/queue-rs" in readme
    assert "--runs-dir /absolute/path/to/vibesys-runs --local" in readme
    assert "--backend cpu --profiler none" in readme
    assert "detects the conventional task Dockerfile" in readme
    assert "automatically runs both agents and gates" in readme
