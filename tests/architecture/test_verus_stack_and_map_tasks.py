from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[2] / "examples" / "data-structures" / "repositories"

RUNTIME_PRIMITIVES = (
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
)

TASKS = (
    ("stack-rs", "verus-mpmc-open", "verus-mpmc"),
    ("unordered-map-rs", "verus-open", "verus-map"),
    ("ordered-map-rs", "verus-open", "verus-ordered-map"),
)


def _task_root(project: str, task: str) -> Path:
    return _REPO_ROOT / project / ".vibesys" / "tasks" / task


def _copy_task(tmp_path: Path, project: str, task: str, crate: str) -> tuple[Path, Path, Path]:
    source_project = _REPO_ROOT / project
    copied_project = tmp_path / project
    copied_task = copied_project / ".vibesys" / "tasks" / task
    candidate = copied_project / crate
    copied_task.mkdir(parents=True)
    shutil.copy2(
        source_project / ".vibesys" / "tasks" / task / "runner.py",
        copied_task / "runner.py",
    )
    shutil.copytree(source_project / crate, candidate, ignore=shutil.ignore_patterns("target"))
    return copied_project, copied_task, candidate


@pytest.mark.parametrize(("project", "task", "crate"), TASKS)
def test_verus_task_requires_cargo_verus_verify(project: str, task: str, crate: str) -> None:
    task_root = _task_root(project, task)
    check_command = f".vibesys/tasks/{task}/runner.py"
    manifest = tomllib.loads((task_root / "vibesys.input.toml").read_text(encoding="utf-8"))

    assert manifest["agent"] == {"domain": "generic"}
    assert "evaluator" not in manifest
    assert manifest["accuracy"] == {
        "command": ["python3", check_command, "check"],
        "timeout_seconds": 300,
    }
    assert manifest["benchmark"]["command"] == ["python3", check_command, "benchmark"]
    assert manifest["benchmark"]["result"] == {
        "json_argument": "--output-json",
        "metric": "total_ops_per_sec",
    }

    runner = (task_root / "runner.py").read_text(encoding="utf-8")
    ast.parse(runner)
    assert '"cargo",\n            "verus",\n            "verify"' in runner
    assert '"--num-threads",\n            "1"' in runner
    assert 'subparsers.add_parser("check-fixture")' in runner
    assert "FORBIDDEN_SOURCE_PATTERNS" in runner
    assert "FIXED_CANDIDATE_FILES" in runner
    assert "package.metadata.verus.verify = true" in runner
    assert f'CANDIDATE_ROOT = PROJECT_ROOT / "{crate}"' in runner
    assert "candidate.so" not in runner

    dockerfile = (task_root / "Dockerfile").read_text(encoding="utf-8")
    assert "VERUS_VERSION=0.2026.08.30.b432e82" in dockerfile
    assert "ln -s /opt/verus/verus /usr/local/bin/verus" in dockerfile
    assert "ln -s /opt/verus/cargo-verus /usr/local/bin/cargo-verus" in dockerfile


def test_verus_stack_contract_is_exact_lifo() -> None:
    task = _task_root("stack-rs", "verus-mpmc-open")
    objective = (task / "OBJECTIVE.md").read_text(encoding="utf-8")
    accuracy = (task / "accuracy" / "src" / "main.rs").read_text(encoding="utf-8")
    benchmark = (task / "benchmark" / "src" / "main.rs").read_text(encoding="utf-8")

    assert "exact linearizable bounded-LIFO semantics" in objective
    assert "does not permit capacity" in objective
    assert "pub fn push(&self, value: T) -> Result<(), T>" in objective
    assert "pub fn pop(&self) -> Option<T>" in objective
    assert "stack_verus_mpmc::MpmcStack" in accuracy
    assert "producer_order_contract" in accuracy
    assert "total_ops_per_sec=" not in accuracy
    assert "stack_verus_mpmc::MpmcStack" in benchmark
    assert "total_ops_per_sec=" in benchmark


def test_verus_unordered_map_contract_is_linearizable_point_map() -> None:
    task = _task_root("unordered-map-rs", "verus-open")
    objective = (task / "OBJECTIVE.md").read_text(encoding="utf-8")
    accuracy = (task / "accuracy" / "src" / "main.rs").read_text(encoding="utf-8")

    assert "exact linearizable put/get/remove semantics" in objective
    assert "There is no ordering, range, or iteration contract" in objective
    assert "pub fn put(&self, key: u64, value: u64) -> Option<u64>" in objective
    assert "unordered_map_verus::ConcurrentMap" in accuracy
    assert "concurrent_remove_contract" in accuracy
    assert "range(" not in accuracy


def test_verus_ordered_map_contract_requires_ordered_ops() -> None:
    task = _task_root("ordered-map-rs", "verus-open")
    objective = (task / "OBJECTIVE.md").read_text(encoding="utf-8")
    accuracy = (task / "accuracy" / "src" / "main.rs").read_text(encoding="utf-8")

    assert "pub fn min(&self) -> Option<(u64, u64)>" in objective
    assert "pub fn range(&self, start: u64, end: u64, max_items: usize)" in objective
    assert "still exact snapshots" in objective
    assert "ordered_map_verus::OrderedMap" in accuracy
    assert "sequential_order_contract" in accuracy
    assert "map.range(" in accuracy


@pytest.mark.parametrize(("project", "crate"), [(project, crate) for project, _, crate in TASKS])
def test_verus_fixed_api_does_not_choose_runtime_synchronization(project: str, crate: str) -> None:
    api = (_REPO_ROOT / project / crate / "src" / "api.rs").read_text(encoding="utf-8")
    assert "AtomicUpdate" in api
    for runtime_primitive in RUNTIME_PRIMITIVES:
        assert runtime_primitive not in api


def test_verus_stack_acceptance_fixture_moves_linearization_points() -> None:
    project = _REPO_ROOT / "stack-rs"
    seed = (project / "verus-mpmc" / "src" / "candidate" / "mod.rs").read_text(encoding="utf-8")
    fixture = (
        _task_root("stack-rs", "verus-mpmc-open")
        / "acceptance"
        / "alternate-lp"
        / "src"
        / "candidate"
        / "mod.rs"
    ).read_text(encoding="utf-8")
    update = "state.auth.borrow_mut().update("
    push = "state.entries.push(value);"
    assert seed.index(push) < seed.index(update)
    assert fixture.index(update) < fixture.index(push)


def test_verus_unordered_map_acceptance_fixture_moves_linearization_points() -> None:
    seed = (
        _REPO_ROOT / "unordered-map-rs" / "verus-map" / "src" / "candidate" / "mod.rs"
    ).read_text(encoding="utf-8")
    fixture = (
        _task_root("unordered-map-rs", "verus-open")
        / "acceptance"
        / "alternate-lp"
        / "src"
        / "candidate"
        / "mod.rs"
    ).read_text(encoding="utf-8")
    update = "state.auth.borrow_mut().update("
    mutate = "state.entries.set(i, (key, value));"
    assert seed.index(mutate) < seed.index(update)
    assert fixture.index(update) < fixture.index(mutate)


def test_verus_ordered_map_acceptance_fixture_moves_linearization_points() -> None:
    seed = (
        _REPO_ROOT / "ordered-map-rs" / "verus-ordered-map" / "src" / "candidate" / "mod.rs"
    ).read_text(encoding="utf-8")
    fixture = (
        _task_root("ordered-map-rs", "verus-open")
        / "acceptance"
        / "alternate-lp"
        / "src"
        / "candidate"
        / "mod.rs"
    ).read_text(encoding="utf-8")
    update = "state.auth.borrow_mut().update("
    mutate = "state.entries.set(i, (key, value));"
    assert seed.index(mutate) < seed.index(update)
    assert fixture.index(update) < fixture.index(mutate)


@pytest.mark.parametrize(("project", "task", "crate"), TASKS)
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
def test_verus_task_rejects_fixed_file_changes(
    tmp_path: Path, project: str, task: str, crate: str, relative_path: str
) -> None:
    _, task_root, candidate = _copy_task(tmp_path, project, task, crate)
    fixed_file = candidate / relative_path
    fixed_file.write_bytes(fixed_file.read_bytes() + b"\n")
    completed = subprocess.run(  # noqa: S603 - executes a copied repository script
        [sys.executable, str(task_root / "runner.py"), "check"],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 1
    assert f"implementer modified fixed candidate file: {relative_path}" in completed.stdout


@pytest.mark.parametrize(("project", "task", "crate"), TASKS)
@pytest.mark.parametrize(
    "relative_path",
    ["build.rs", ".cargo/config.toml", "src/alternate.rs", "notes.txt"],
)
def test_verus_task_rejects_files_outside_candidate(
    tmp_path: Path, project: str, task: str, crate: str, relative_path: str
) -> None:
    project_root, task_root, candidate = _copy_task(tmp_path, project, task, crate)
    unexpected = candidate / relative_path
    unexpected.parent.mkdir(parents=True, exist_ok=True)
    unexpected.write_text("fn main() {}\n", encoding="utf-8")
    completed = subprocess.run(  # noqa: S603 - executes a copied repository script
        [sys.executable, str(task_root / "runner.py"), "check"],
        cwd=project_root,
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
        ("#[cfg (not(verus_keep_ghost))] fn split() {}", "cfg attribute"),
    ],
)
def test_verus_task_rejects_candidate_source_tricks(
    tmp_path: Path, source: str, description: str
) -> None:
    for project, task, crate in TASKS:
        project_root, task_root, candidate = _copy_task(tmp_path, project, task, crate)
        (candidate / "src" / "candidate" / "trick.rs").write_text(source, encoding="utf-8")
        completed = subprocess.run(  # noqa: S603 - executes a copied repository script
            [sys.executable, str(task_root / "runner.py"), "check"],
            cwd=project_root,
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 1
        assert f"forbidden source construct '{description}'" in completed.stdout


@pytest.mark.parametrize(("project", "task"), [(project, task) for project, task, _ in TASKS])
def test_verus_task_readme_shows_vibesys_task_command(project: str, task: str) -> None:
    readme = (_task_root(project, task) / "README.md").read_text(encoding="utf-8")
    assert "vibesys --outer-loop agent" in readme
    assert f"--project examples/data-structures/repositories/{project}" in readme
    assert "--runs-dir /absolute/path/to/vibesys-runs --local" in readme
    assert "No separate `docker build`, `--docker`, or `--docker-image`" in readme
