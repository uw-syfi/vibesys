"""``vibesys example``: list the catalog, and copy an example into a new project."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.example_materializer import (
    FakeExampleMaterializer,
    FakeRepository,
    check_copy_tracked_contract,
    check_repository_root_contract,
)

from entrypoints import launcher
from entrypoints.example_catalog import ExampleList, ExampleSummary
from entrypoints.examples import run
from vs_sim.api.testing import FakeForegroundLauncher, ForegroundScript, examples

_NAMES = st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)

EXTERNAL = PurePosixPath("examples/serving/repositories/engine")


def _task(project: Path, name: str) -> None:
    task = project / ".vibesys" / "tasks" / name
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("version = 1\n", encoding="utf-8")


def _catalog(checkout: Path, examples: dict[str, tuple[str, ...]]) -> None:
    """Write a checkout whose catalog holds ``examples`` (path below examples/ -> tasks)."""
    entries = []
    for name, tasks in examples.items():
        root = checkout / "examples" / name
        root.mkdir(parents=True)
        (root / "src.txt").write_text(f"candidate {name}\n", encoding="utf-8")
        for task in tasks:
            _task(root, task)
        entries.append(f'[[example]]\npath = "examples/{name}"\n')
    entries.append(f'[[example]]\npath = "{EXTERNAL}"\nexternal_repo = true\n')
    (checkout / "examples" / "registry.toml").write_text(
        "version = 1\n\n" + "\n".join(entries), encoding="utf-8"
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "vibesys"
    _catalog(root, {"structures/stack": ("spsc", "mpmc"), "database/kv": ("default",)})
    return root


def _pinned() -> dict[PurePosixPath, dict[str, str]]:
    return {
        EXTERNAL: {
            "README.md": "engine\n",
            ".vibesys/tasks/serve/OBJECTIVE.md": "Serve faster.\n",
            ".vibesys/tasks/serve/vibesys.input.toml": "version = 1\n",
        }
    }


# --- list -----------------------------------------------------------------------


@settings(
    max_examples=examples(pr=10, nightly=25),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    examples=st.dictionaries(
        st.tuples(_NAMES, _NAMES).map("/".join),
        st.sets(_NAMES, max_size=3).map(lambda tasks: tuple(sorted(tasks))),
        min_size=1,
        max_size=5,
    )
)
def test_json_lists_every_example_by_category_with_its_tasks(
    tmp_path_factory: pytest.TempPathFactory, examples: dict[str, tuple[str, ...]]
) -> None:
    checkout = tmp_path_factory.mktemp("vibesys")
    _catalog(checkout, examples)

    code, stdout, stderr = run(["list", "--json"], cwd=checkout, checkout=checkout)

    assert (code, stderr) == (0, "")
    expected = [
        ExampleSummary(
            name=name,
            category=name.split("/")[0],
            tasks=tasks,
            external_repo=False,
            task_details=(),
        )
        for name, tasks in examples.items()
    ] + [
        ExampleSummary(
            name="serving/repositories/engine",
            category="serving",
            tasks=(),
            external_repo=True,
            task_details=(),
        )
    ]
    listing = ExampleList.model_validate_json(stdout)
    assert listing.checkout == str(checkout.resolve())
    assert listing.examples == tuple(sorted(expected, key=lambda e: (e.category, e.name)))


def test_human_list_groups_by_category(checkout: Path) -> None:
    code, stdout, _ = run(["list"], cwd=checkout, checkout=checkout)

    assert code == 0
    assert stdout.splitlines() == [
        "database:",
        "  database/kv                  default",
        "serving:",
        "  serving/repositories/engine  (external repository, not fetched)",
        "structures:",
        "  structures/stack             mpmc, spsc",
    ]


def test_missing_catalog_exits_1_naming_the_registry(tmp_path: Path) -> None:
    code, stdout, stderr = run(["list"], cwd=tmp_path, checkout=tmp_path)

    assert (code, stdout) == (1, "")
    assert str(tmp_path / "examples" / "registry.toml") in stderr


def test_registry_rejects_unknown_keys_naming_them(checkout: Path) -> None:
    registry = checkout / "examples" / "registry.toml"
    registry.write_text(registry.read_text() + "surprise = 1\n", encoding="utf-8")

    code, _, stderr = run(["list"], cwd=checkout, checkout=checkout)

    assert code == 1
    assert "surprise" in stderr


# --- new ------------------------------------------------------------------------


def test_new_copies_the_example_commits_it_and_prints_the_run_line(
    checkout: Path, tmp_path: Path
) -> None:
    fake = FakeExampleMaterializer()
    destination = tmp_path / "work" / "stack"

    code, stdout, stderr = run(
        ["new", "structures/stack", str(destination)],
        cwd=tmp_path,
        checkout=checkout,
        materializer=fake,
    )

    assert (code, stderr) == (0, "")
    assert stdout.splitlines() == [
        f"Created {destination} from example structures/stack.",
        "Run it with:",
        f"  cd {destination}",
        "  vibesys --task mpmc",
        "Other tasks: spsc",
    ]
    assert fake.repositories == {
        destination: FakeRepository(
            "Start from VibeSys example structures/stack",
            frozenset(
                {
                    "src.txt",
                    ".vibesys/tasks/spsc/OBJECTIVE.md",
                    ".vibesys/tasks/spsc/vibesys.input.toml",
                    ".vibesys/tasks/mpmc/OBJECTIVE.md",
                    ".vibesys/tasks/mpmc/vibesys.input.toml",
                }
            ),
        )
    }


def test_new_accepts_a_unique_short_name_and_a_relative_destination(
    checkout: Path, tmp_path: Path
) -> None:
    fake = FakeExampleMaterializer()

    code, stdout, _ = run(
        ["new", "kv", "kv-project"], cwd=tmp_path, checkout=checkout, materializer=fake
    )

    assert code == 0
    assert "  vibesys --task default" in stdout.splitlines()
    assert set(fake.repositories) == {tmp_path / "kv-project"}


def test_new_clones_an_external_example_at_its_pinned_commit(
    checkout: Path, tmp_path: Path
) -> None:
    fake = FakeExampleMaterializer(pinned=_pinned())
    destination = tmp_path / "engine"

    code, stdout, _ = run(
        ["new", "serving/repositories/engine", str(destination)],
        cwd=tmp_path,
        checkout=checkout,
        materializer=fake,
    )

    assert code == 0
    assert "  vibesys --task serve" in stdout.splitlines()
    assert fake.repositories[destination].origin == f"clone {EXTERNAL}"


def test_new_into_an_existing_empty_directory_is_allowed(checkout: Path, tmp_path: Path) -> None:
    destination = tmp_path / "empty"
    destination.mkdir()

    code, _, _ = run(
        ["new", "kv", str(destination)],
        cwd=tmp_path,
        checkout=checkout,
        materializer=FakeExampleMaterializer(),
    )

    assert code == 0
    assert (destination / "src.txt").is_file()


@settings(
    max_examples=examples(pr=10, nightly=25),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(existing=st.sets(_NAMES, min_size=1, max_size=4))
def test_new_refuses_a_non_empty_destination_and_leaves_it_untouched(
    tmp_path_factory: pytest.TempPathFactory, existing: set[str]
) -> None:
    root = tmp_path_factory.mktemp("case")
    checkout = root / "vibesys"
    _catalog(checkout, {"structures/stack": ("spsc",)})
    destination = root / "taken"
    destination.mkdir()
    for name in existing:
        (destination / name).write_text("mine\n", encoding="utf-8")
    fake = FakeExampleMaterializer()

    code, stdout, stderr = run(
        ["new", "stack", str(destination)], cwd=root, checkout=checkout, materializer=fake
    )

    assert (code, stdout) == (1, "")
    assert "not empty" in stderr
    assert str(destination) in stderr
    assert sorted(path.name for path in destination.iterdir()) == sorted(existing)
    assert fake.repositories == {}


def test_new_refuses_a_destination_inside_the_checkout(checkout: Path) -> None:
    fake = FakeExampleMaterializer()

    code, _, stderr = run(
        ["new", "kv", str(checkout / "scratch")],
        cwd=checkout,
        checkout=checkout,
        materializer=fake,
    )

    assert code == 1
    assert "inside the VibeSys checkout" in stderr
    assert not (checkout / "scratch").exists()


@pytest.mark.parametrize("name", ["nope", "structures"])
def test_new_names_an_unknown_example(checkout: Path, tmp_path: Path, name: str) -> None:
    code, _, stderr = run(
        ["new", name, str(tmp_path / "x")],
        cwd=tmp_path,
        checkout=checkout,
        materializer=FakeExampleMaterializer(),
    )

    assert code == 1
    assert repr(name) in stderr
    assert "vibesys example list" in stderr


def test_an_ambiguous_short_name_lists_the_candidates(tmp_path: Path) -> None:
    checkout = tmp_path / "vibesys"
    _catalog(checkout, {"a/stack": ("x",), "b/stack": ("y",)})

    code, _, stderr = run(
        ["new", "stack", str(tmp_path / "x")],
        cwd=tmp_path,
        checkout=checkout,
        materializer=FakeExampleMaterializer(),
    )

    assert code == 1
    assert "a/stack, b/stack" in stderr


@settings(
    max_examples=examples(pr=10, nightly=20),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(fail_after=st.integers(min_value=0, max_value=4), preexisting=st.booleans())
def test_a_failed_copy_leaves_the_destination_as_it_was(
    tmp_path_factory: pytest.TempPathFactory, fail_after: int, *, preexisting: bool
) -> None:
    root = tmp_path_factory.mktemp("case")
    checkout = root / "vibesys"
    _catalog(checkout, {"structures/stack": ("spsc", "mpmc")})
    destination = root / "work"
    if preexisting:
        destination.mkdir()

    code, _, stderr = run(
        ["new", "stack", str(destination)],
        cwd=root,
        checkout=checkout,
        materializer=FakeExampleMaterializer(fail_after=fail_after),
    )

    assert code == 1
    assert "injected failure" in stderr
    assert destination.exists() is preexisting
    if preexisting:
        assert list(destination.iterdir()) == []


def test_an_external_example_the_checkout_does_not_pin_fails_cleanly(
    checkout: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "engine"

    code, _, stderr = run(
        ["new", "engine", str(destination)],
        cwd=tmp_path,
        checkout=checkout,
        materializer=FakeExampleMaterializer(),
    )

    assert code == 1
    assert "declares no submodule" in stderr
    assert not destination.exists()


def test_the_fake_passes_the_copy_contract(checkout: Path, tmp_path: Path) -> None:
    check_copy_tracked_contract(
        FakeExampleMaterializer(), checkout, PurePosixPath("examples/structures/stack"), tmp_path
    )


def test_the_fake_passes_the_repository_root_contract(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / "nested").mkdir()
    (tmp_path / "outside").mkdir()

    check_repository_root_contract(FakeExampleMaterializer(), repository, tmp_path / "outside")


def test_launcher_runs_example_in_process_without_a_child(
    checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    children = FakeForegroundLauncher(lambda _argv: ForegroundScript(exits_immediately=True))

    code = launcher.main(
        ["example", "--checkout", str(checkout), "list", "--json"],
        launcher.LauncherHost(children=children),
    )

    assert code == 0
    assert children.children == []
    listing = ExampleList.model_validate_json(capsys.readouterr().out)
    assert listing.examples[0].name == "database/kv"
