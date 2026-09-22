"""Static assurance for every example registered in ``examples/registry.toml``.

What this covers, per registered example:

- the registry is complete (fail-closed discovery) and current;
- every task validates through the same entry point as ``vibesys validate``;
- every ``${PROJECT_ROOT}/...`` path a task command reads exists, and, for the
  task layout, is read-only under the real agent sandbox policy;
- no reference into ``examples/`` from workflows, scripts, or docs is stale.

What it does not cover: running an evaluator, docker, Kubernetes, GPUs, or
model weights. See ``docs/contributing/examples.md``.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.example_registry import (
    REGISTRY_RELATIVE,
    REPO_ROOT,
    Check,
    ExampleEntry,
    Layout,
    external_repo_missing,
    load_registry,
    require_external_repos,
    submodule_example_paths,
    suggested_entry,
    unregistered_examples,
)

from entrypoints.cli import _run_validate
from vibesys.errors import ConfigurationError
from vibesys.run.project_policy import build_project_path_policy, trusted_project_input_paths
from vs_project import Project

if TYPE_CHECKING:
    from collections.abc import Iterable

REGISTRY = load_registry()
ENTRIES = list(REGISTRY.example)
_FETCH_HINT = "run `uv run python scripts/example_repositories.py`"


def _param(entry: ExampleEntry, check: Check) -> object:
    marks = []
    known = entry.known_failure(check)
    if known:
        marks.append(
            pytest.mark.xfail(
                strict=True,
                reason=f"known-failing: {known.reason} ({known.tracking}); remove the {check} "
                f"known_failing entry in {REGISTRY_RELATIVE} once it passes",
            )
        )
    return pytest.param(entry, id=entry.path, marks=marks)


def _require_present(entry: ExampleEntry) -> None:
    """Fail (CI) or skip (local) when this external repo example is not fetched."""
    if not external_repo_missing(entry):
        return
    message = f"{entry.path} is an external repo that is not fetched: {_FETCH_HINT}"
    if require_external_repos():
        pytest.fail(message)
    pytest.skip(f"{message} (or set VIBESYS_REQUIRE_EXAMPLE_EXTERNAL_REPOS=1 to fail instead)")


def _tasks(entry: ExampleEntry) -> tuple[str, ...]:
    if entry.layout is Layout.LEGACY:
        return ()
    return tuple(task.name.value for task in Project.open(entry.root).discover_tasks())


# --- registry completeness (fail closed) ------------------------------------


def test_registry_entries_exist() -> None:
    missing = [entry.path for entry in ENTRIES if not (entry.root).is_dir()]
    assert not missing, f"{REGISTRY_RELATIVE} lists paths that do not exist: {missing}"


def test_every_example_directory_is_registered() -> None:
    missing = unregistered_examples({entry.path for entry in ENTRIES})
    assert not missing, (
        f"Unregistered example(s) {missing}. Add to {REGISTRY_RELATIVE}:\n\n"
        + "\n\n".join(suggested_entry(path) for path in missing)
    )


@pytest.mark.parametrize("entry", [pytest.param(e, id=e.path) for e in ENTRIES])
def test_registered_layout_and_tasks_match_disk(entry: ExampleEntry) -> None:
    _require_present(entry)
    has_tasks = (entry.root / ".vibesys" / "tasks").is_dir()
    has_manifest = (entry.root / "vibesys.input.toml").is_file()
    if entry.layout is Layout.TASK:
        assert has_tasks, f"{entry.path}: registered as layout=task but has no .vibesys/tasks"
        on_disk = set(_tasks(entry))
        assert on_disk, f"{entry.path}: .vibesys/tasks defines no task"
        if entry.tasks != "all":
            assert set(entry.tasks) == on_disk, (
                f"{entry.path}: registered tasks {sorted(entry.tasks)} differ from disk "
                f'{sorted(on_disk)}; edit {REGISTRY_RELATIVE} (or use tasks = "all")'
            )
    else:
        assert not has_tasks, f"{entry.path}: registered as layout=legacy but has .vibesys/tasks"
        assert has_manifest, (
            f"{entry.path}: registered as layout=legacy but has no vibesys.input.toml"
        )


def test_submodule_examples_are_registered_as_external_repos() -> None:
    by_path = {entry.path: entry for entry in ENTRIES}
    wrong = sorted(
        path
        for path in submodule_example_paths()
        if path not in by_path or not by_path[path].external_repo
    )
    assert not wrong, f"submodule examples {wrong} must be registered with external_repo = true"


def test_ci_fetches_external_repos_and_runs_this_module() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text()
    job = workflow.split("\n  validate-examples:", 1)[1].split("\n  # ", 1)[0]
    assert "scripts/example_repositories.py" in job
    assert 'VIBESYS_REQUIRE_EXAMPLE_EXTERNAL_REPOS: "1"' in job
    assert "tests/examples/test_example_registry.py" in job


# --- validation through the CLI entry point ---------------------------------


@pytest.mark.parametrize("entry", [_param(e, Check.VALIDATE) for e in ENTRIES])
def test_example_validates(entry: ExampleEntry, capsys: pytest.CaptureFixture[str]) -> None:
    _require_present(entry)
    failures: list[str] = []
    for task in _tasks(entry) or (None,):
        argv = [str(entry.root), *(("--task", task) if task else ())]
        try:
            _run_validate(argv)
        except ConfigurationError as error:
            failures.append(f"{entry.path} task={task}: {error}")
            continue
    capsys.readouterr()
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("entry", [_param(e, Check.PATH_REFS) for e in ENTRIES])
def test_command_path_references_exist(entry: ExampleEntry) -> None:
    _require_present(entry)
    failures = [
        message for task in _tasks(entry) or (None,) for message in _missing_references(entry, task)
    ]
    skip = entry.skip(Check.PATH_REFS)
    if skip:
        assert failures, (
            f"{entry.path}: the path-refs skip in {REGISTRY_RELATIVE} is obsolete; remove it"
        )
        pytest.skip(f"explicit skip: {skip.reason}")
    assert not failures, "\n".join(failures)


_ROOT_REF = re.compile(r"\$\{PROJECT_ROOT\}((?:/[^\s\"'\\,\[\]=;&|]+)*)")
#: Paths the run creates, or fills from a workspace source, so absence is fine.
_RUNTIME_DIRS = (".vibesys/state", "_evaluator", "_input_libs")


def _command_strings(manifest: Path) -> list[str]:
    document = tomllib.loads(manifest.read_text())
    strings: list[str] = []
    for section in ("accuracy", "benchmark"):
        table = document.get(section, {})
        strings += [str(item) for key in ("command", "args") for item in table.get(key, [])]
    return strings


def _manifest(entry: ExampleEntry, task: str | None) -> Path:
    if task is None:
        return entry.root / "vibesys.input.toml"
    return Project.open(entry.root).select_task(task).manifest_path


def _referenced_paths(entry: ExampleEntry, task: str | None) -> set[str]:
    """Return project-relative paths that the task's commands read via PROJECT_ROOT."""
    manifest = _manifest(entry, task)
    found = set()
    for text in _command_strings(manifest):
        bare = posixpath.normpath(text)
        if (
            not text.startswith(("-", "/"))
            and "${" not in text
            and bare != "."
            and not bare.startswith("..")
            and (entry.root / bare).exists()
        ):
            found.add(bare)
        for match in _ROOT_REF.finditer(text):
            relative = match.group(1).strip("/").rstrip(".")
            if relative and ".." not in relative.split("/"):
                found.add(relative)
    return found


def _workspace_destinations(entry: ExampleEntry, task: str | None) -> set[str]:
    sources = (
        tomllib.loads(_manifest(entry, task).read_text()).get("workspace", {}).get("sources", [])
    )
    return {source["dest"] for source in sources}


def _missing_references(entry: ExampleEntry, task: str | None) -> list[str]:
    destinations = _workspace_destinations(entry, task)
    missing = []
    for relative in sorted(_referenced_paths(entry, task)):
        supplied = (*_RUNTIME_DIRS, *destinations)
        if any(relative == d or relative.startswith(f"{d}/") for d in supplied):
            continue
        if not (entry.root / relative).exists():
            missing.append(
                f"{entry.path} task={task}: command references ${{PROJECT_ROOT}}/{relative}, "
                "which does not exist"
            )
    return missing


# --- trust policy -----------------------------------------------------------


def _readonly(relative: Path, read_only: Iterable[Path]) -> bool:
    return any(relative == path or relative.is_relative_to(path) for path in read_only)


@pytest.mark.parametrize("entry", [_param(e, Check.TRUST_POLICY) for e in ENTRIES])
def test_task_inputs_are_read_only_for_the_agent(entry: ExampleEntry, tmp_path: Path) -> None:
    """Every project file a task command reads must be under a read-only path.

    Builds the sandbox policy with the production functions on a scratch copy
    (so the example is never touched) and checks the `${PROJECT_ROOT}/...`
    paths in the accuracy and benchmark commands that exist inside the example.
    Not covered: files a command opens indirectly (imports, config it reads
    from disk) and paths built at runtime.
    """
    _require_present(entry)
    project = tmp_path / "project"
    shutil.copytree(
        entry.root,
        project,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "target", "node_modules", "__pycache__"),
    )
    policy = build_project_path_policy(project, evaluator_source=None)
    trusted = trusted_project_input_paths(project, evaluator_source=None)
    unprotected = [
        p for p in trusted if (project / p).exists() and not _readonly(p, policy.read_only_paths)
    ]
    assert not unprotected, (
        f"{entry.path}: trusted inputs outside the sandbox policy: {unprotected}"
    )

    writable: list[str] = []
    for task in _tasks(entry) or (None,):
        writable.extend(
            f"{relative} (task {task})"
            for relative in sorted(_referenced_paths(entry, task))
            if (project / relative).exists()
            and not _readonly(Path(relative), policy.read_only_paths)
        )
    assert not writable, (
        f"{entry.path}: agent-writable files that task commands read: {writable}. "
        f"Read-only under the policy: {sorted(p.as_posix() for p in policy.read_only_paths)}. "
        "Move them under .vibesys/tasks/<task>/ so the sandbox protects them."
    )


# --- stale references -------------------------------------------------------

#: `examples/...` literals that are intentionally not paths in this checkout.
#: Add `(file, literal)` with a comment saying why.
STALE_REFERENCE_EXCLUSIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # Paths inside the candidate repository (vllm), not in this one.
        ("docs/cli-flags.md", "examples/deployment/service.py"),
        ("docs/running-vibesys.md", "examples/deployment/service.py"),
        # Genuinely stale: no examples/model-serving/llama-mi300 exists.
        # NEEDS AN OWNER: point the Slurm walkthrough at a current example.
        ("docs/contributing/remote-slurm-execution.md", "examples/model-serving/llama-mi300"),
    }
)

_SCANNED = (
    ":(glob).github/workflows/*",
    ":(glob)scripts/**",
    ":(glob)docs/**",
    ":(glob)*.md",
    ":(glob)examples/**/*.md",
)
_EXAMPLE_LITERAL = re.compile(r"(?<![\w/.-])examples/[\w.@+*/-]+")


def _tracked_files() -> list[str]:
    result = subprocess.run(  # noqa: S603
        ["git", "ls-files", "--", *_SCANNED],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(set(result.stdout.split()))


def _resolves(literal: str) -> bool:
    if "*" in literal:
        return any(REPO_ROOT.glob(literal))
    return (REPO_ROOT / literal).exists() or (REPO_ROOT / literal.rstrip("/")).exists()


def _inside_unfetched_submodule(literal: str) -> bool:
    return any(
        literal.startswith(f"{path}/") and not (REPO_ROOT / path / ".git").exists()
        for path in submodule_example_paths()
    )


def test_example_paths_referenced_outside_examples_exist() -> None:
    stale = []
    for name in _tracked_files():
        if not (REPO_ROOT / name).is_file():
            continue
        text = (REPO_ROOT / name).read_text(errors="replace")
        for match in _EXAMPLE_LITERAL.finditer(text):
            literal = match.group(0).rstrip(".,:;)/`*")
            if (
                (name, literal) in STALE_REFERENCE_EXCLUSIONS
                or _resolves(literal)
                or _inside_unfetched_submodule(literal)
            ):
                continue
            stale.append(f"{name}: {literal}")
    assert not stale, "stale example path references:\n" + "\n".join(sorted(set(stale)))


def test_stale_reference_exclusions_are_current() -> None:
    dead = [
        (name, literal)
        for name, literal in STALE_REFERENCE_EXCLUSIONS
        if _resolves(literal) or literal not in (REPO_ROOT / name).read_text(errors="replace")
    ]
    assert not dead, f"remove obsolete STALE_REFERENCE_EXCLUSIONS entries: {dead}"


@pytest.mark.parametrize(
    "fields",
    [
        {"layout": "legacy", "tasks": ["a"]},
        {"surprise": 1},
        {"live": "none"},  # stale fields must fail loudly
        {"requires": ["docker"]},
        {"known_failing": [{"check": "validate", "reason": "x"}]},
        {
            "skips": [{"check": "path-refs", "reason": "x"}],
            "known_failing": [{"check": "path-refs", "reason": "x", "tracking": "y"}],
        },
    ],
)
def test_registry_entry_model_rejects_inconsistent_entries(fields: dict[str, object]) -> None:
    base = {"path": "examples/x", "layout": "task"}
    with pytest.raises(ValueError, match=r"examples/x|surprise|Extra|live|tracking|forbidden"):
        ExampleEntry.model_validate({**base, **fields})
