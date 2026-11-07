"""``vibesys example new`` with real Git: the copy contract, and a catalog example end to end."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from packaging.specifiers import SpecifierSet
from tests.support import run_test_command
from tests.support.example_materializer import (
    check_copy_tracked_contract,
)
from tests.support.example_registry import REPO_ROOT, example_root, load_registry

from entrypoints.examples import GitExampleMaterializer, default_checkout, run
from vs_project.api import Git
from vs_sim.api import OsProcesses
from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    from entrypoints.example_catalog import ExampleEntry

REGISTRY = load_registry()


def _git(cwd: Path, *args: str) -> str:
    return run_test_command(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=HANG_GUARD_S,
    ).stdout


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the initial commit an identity on machines without a Git configuration."""
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Example Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "example-test@localhost")


def test_git_materializer_passes_the_copy_contract(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    example = checkout / "examples" / "demo"
    (example / "src").mkdir(parents=True)
    (example / "src" / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
    (example / "README.md").write_text("demo\n", encoding="utf-8")
    _git(checkout, "init", "--quiet")
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "--message", "catalog")

    check_copy_tracked_contract(
        GitExampleMaterializer(Git(OsProcesses())),
        checkout,
        PurePosixPath("examples/demo"),
        tmp_path / "out",
    )


def test_untracked_files_stay_behind_and_the_copy_is_one_clean_commit(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    example = checkout / "examples" / "demo"
    example.mkdir(parents=True)
    (example / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(checkout, "init", "--quiet")
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "--message", "catalog")
    (example / "build-output.so").write_text("untracked\n", encoding="utf-8")
    destination = tmp_path / "project"

    GitExampleMaterializer(Git(OsProcesses())).copy_tracked(
        checkout, PurePosixPath("examples/demo"), destination, message="Start"
    )

    assert sorted(path.name for path in destination.iterdir()) == [".git", "kept.txt"]
    assert _git(destination, "log", "--format=%s").splitlines() == ["Start"]
    assert _git(destination, "status", "--porcelain") == ""


def test_a_path_source_outside_the_example_is_vendored_and_rewritten(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    example = checkout / "examples" / "serving" / "demo"
    library = checkout / "sdk" / "toolkit"
    (library / "src").mkdir(parents=True)
    (library / "src" / "toolkit.py").write_text("X = 1\n", encoding="utf-8")
    (library / "pyproject.toml").write_text('[project]\nname = "toolkit"\n', encoding="utf-8")
    example.mkdir(parents=True)
    (example / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["toolkit"]\n'
        '[tool.uv.sources]\ntoolkit = { path = "../../../sdk/toolkit" }\n',
        encoding="utf-8",
    )
    _git(checkout, "init", "--quiet")
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "--message", "catalog")
    destination = tmp_path / "project"

    GitExampleMaterializer(Git(OsProcesses())).copy_tracked(
        checkout, PurePosixPath("examples/serving/demo"), destination, message="Start"
    )

    assert 'path = "vendor/toolkit"' in (destination / "pyproject.toml").read_text()
    assert (destination / "vendor" / "toolkit" / "src" / "toolkit.py").is_file()
    assert (destination / "vendor" / "toolkit" / "pyproject.toml").is_file()
    assert _git(destination, "status", "--porcelain") == ""


def test_new_from_the_real_catalog_creates_a_runnable_project(tmp_path: Path) -> None:
    destination = tmp_path / "stack"

    code, stdout, stderr = run(
        ["new", "data-structures/stack", str(destination)],
        cwd=tmp_path,
        checkout=default_checkout(),
    )

    assert (code, stderr) == (0, "")
    assert f"  cd {destination}" in stdout.splitlines()
    assert "  vibesys --task mpmc" in stdout.splitlines()
    assert sorted(path.name for path in (destination / ".vibesys" / "tasks").iterdir()) == [
        "mpmc",
        "mpsc",
        "spmc",
        "spsc",
    ]
    assert (destination / "src" / "lib.rs").is_file()
    assert _git(destination, "log", "--format=%s").splitlines() == [
        "Start from VibeSys example data-structures/stack"
    ]


_PYTHON_MINORS = tuple(f"3.{minor}" for minor in range(8, 21))


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(e, id=e.path)
        for e in REGISTRY.example
        if not e.external_repo and (example_root(e) / "pyproject.toml").is_file()
    ],
)
def test_a_copied_example_resolves_its_path_dependencies(
    entry: ExampleEntry, tmp_path: Path
) -> None:
    """``vibesys example new`` copies an example outside the checkout, where uv must still resolve it.

    Every ``[tool.uv.sources]`` path of the copy must hold a project, and the example's
    ``requires-python`` must admit no Python the dependency refuses (``uv lock`` fails
    otherwise).
    """
    project = tmp_path / "project"
    GitExampleMaterializer(Git(OsProcesses())).copy_tracked(
        REPO_ROOT, PurePosixPath(entry.path), project, message="Start"
    )
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    own = SpecifierSet(config["project"].get("requires-python", ""))
    sources = config.get("tool", {}).get("uv", {}).get("sources", {})
    failures: list[str] = []
    for name, source in sources.items():
        if "path" not in source:
            continue
        manifest = project / source["path"] / "pyproject.toml"
        if not manifest.is_file():
            failures.append(f"{name}: path {source['path']!r} has no pyproject.toml in a copy")
            continue
        dependency = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]
        theirs = SpecifierSet(dependency.get("requires-python", ""))
        refused = [v for v in _PYTHON_MINORS if v in own and v not in theirs]
        if refused:
            failures.append(
                f"{name}: requires-python {str(own)!r} admits Python {refused} that "
                f"{str(theirs)!r} refuses; raise the example's requires-python"
            )
    assert not failures, f"{entry.path}: " + "; ".join(failures)
