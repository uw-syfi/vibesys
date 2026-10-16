"""The host helper's ``examples``: the catalog, a project from an example, and copying one out."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.example_materializer import FakeExampleMaterializer

from entrypoints.examples import HelperExampleProjects
from server.host_api import HelperErrorCode
from server.host_examples import CreatedProject, ExampleList, ExampleSummary
from server.host_helper_requests import ProjectLookupError
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path


def _task(project: Path, name: str) -> None:
    task = project / ".vibesys" / "tasks" / name
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("version = 1\n", encoding="utf-8")


def _checkout(root: Path) -> Path:
    example = root / "examples" / "structures" / "stack"
    example.mkdir(parents=True)
    (example / "src.txt").write_text("candidate\n", encoding="utf-8")
    _task(example, "spsc")
    (root / "examples" / "registry.toml").write_text(
        'version = 1\n\n[[example]]\npath = "examples/structures/stack"\n', encoding="utf-8"
    )
    return root


def _projects(checkout: Path | None, *, fail_after: int | None = None) -> HelperExampleProjects:
    return HelperExampleProjects(checkout, FakeExampleMaterializer(fail_after=fail_after))


def _code(error: pytest.ExceptionInfo[ProjectLookupError]) -> tuple[HelperErrorCode, str]:
    return error.value.code, error.value.path


def test_the_catalog_lists_each_example_with_its_tasks(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "vibesys")

    assert _projects(checkout).list() == ExampleList(
        checkout=str(checkout.resolve()),
        examples=(
            ExampleSummary(
                name="structures/stack", category="structures", tasks=("spsc",), external_repo=False
            ),
        ),
    )


def test_an_example_becomes_its_own_project_with_its_tasks(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "vibesys")
    destination = tmp_path / "work" / "stack"

    created = _projects(checkout).create("stack", destination)

    assert created == CreatedProject(project=str(destination.resolve()), tasks=("spsc",))
    assert (destination / "src.txt").read_text(encoding="utf-8") == "candidate\n"


@pytest.mark.parametrize(
    ("example", "destination", "code"),
    [
        ("nope", "work/x", HelperErrorCode.UNKNOWN_EXAMPLE),
        ("stack", "occupied", HelperErrorCode.DESTINATION_EXISTS),
        ("stack", "vibesys/inside", HelperErrorCode.DESTINATION_NESTED),
    ],
)
def test_a_refused_example_says_why(
    tmp_path: Path, example: str, destination: str, code: HelperErrorCode
) -> None:
    checkout = _checkout(tmp_path / "vibesys")
    (tmp_path / "occupied").mkdir()
    (tmp_path / "occupied" / "file").write_text("x", encoding="utf-8")

    with pytest.raises(ProjectLookupError) as error:
        _projects(checkout).create(example, tmp_path / destination)

    assert error.value.code is code


def test_a_helper_without_a_checkout_has_no_examples(tmp_path: Path) -> None:
    with pytest.raises(ProjectLookupError) as listing:
        _projects(None).list()
    with pytest.raises(ProjectLookupError) as creating:
        _projects(None).create("stack", tmp_path / "x")

    assert listing.value.code is HelperErrorCode.EXAMPLES_UNAVAILABLE
    assert creating.value.code is HelperErrorCode.EXAMPLES_UNAVAILABLE


def test_a_failed_copy_leaves_nothing_behind(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path / "vibesys")
    destination = tmp_path / "work" / "stack"

    with pytest.raises(ProjectLookupError) as error:
        _projects(checkout, fail_after=1).create("stack", destination)

    assert _code(error) == (HelperErrorCode.PROJECT_CREATE_FAILED, str(destination))
    assert not destination.exists()


def _monorepo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    folder = root / "services" / "queue"
    folder.mkdir(parents=True)
    (folder / "queue.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    _task(folder, "spsc")
    return folder


def test_a_nested_folder_is_copied_out_into_its_own_project(tmp_path: Path) -> None:
    folder = _monorepo(tmp_path / "mono")
    destination = tmp_path / "queue"
    materializer = FakeExampleMaterializer()

    created = HelperExampleProjects(None, materializer).copy_out(folder, destination)

    assert created == CreatedProject(project=str(destination.resolve()), tasks=("spsc",))
    assert (destination / "queue.c").exists()
    assert destination.resolve() in materializer.repositories


@pytest.mark.parametrize(
    ("source", "destination", "code", "named"),
    [
        ("mono/services/queue", "existing", HelperErrorCode.DESTINATION_EXISTS, "existing"),
        ("mono/services/queue", "mono/copy", HelperErrorCode.DESTINATION_NESTED, "mono/copy"),
        ("mono", "copy", HelperErrorCode.NOT_NESTED, "mono"),
        ("loose", "copy", HelperErrorCode.NOT_NESTED, "loose"),
        ("missing", "copy", HelperErrorCode.PATH_MISSING, "missing"),
    ],
)
def test_a_refused_copy_out_names_the_path(
    tmp_path: Path, source: str, destination: str, code: HelperErrorCode, named: str
) -> None:
    _monorepo(tmp_path / "mono")
    (tmp_path / "existing").mkdir()
    (tmp_path / "loose").mkdir()

    with pytest.raises(ProjectLookupError) as error:
        HelperExampleProjects(None, FakeExampleMaterializer()).copy_out(
            tmp_path / source, tmp_path / destination
        )

    assert _code(error) == (code, str(tmp_path / named))
    assert named in str(error.value)


_NAMES = st.from_regex(r"[a-z]{1,6}", fullmatch=True)


@settings(
    max_examples=examples(pr=20, nightly=200),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    files=st.dictionaries(
        st.lists(_NAMES, min_size=1, max_size=3).map("/".join),
        st.text(max_size=20),
        min_size=1,
        max_size=6,
    )
)
def test_copying_out_any_tree_reproduces_it(
    tmp_path_factory: pytest.TempPathFactory, files: dict[str, str]
) -> None:
    root = tmp_path_factory.mktemp("tree")
    (root / "mono" / ".git").mkdir(parents=True)
    folder = root / "mono" / "app"
    written: dict[str, str] = {}
    for relative, text in files.items():
        target = folder / relative
        if any(parent.is_file() for parent in target.parents) or target.is_dir():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written[relative] = text
    folder.mkdir(parents=True, exist_ok=True)

    HelperExampleProjects(None, FakeExampleMaterializer()).copy_out(folder, root / "copy")

    copied = {
        path.relative_to(root / "copy").as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "copy").rglob("*")
        if path.is_file()
    }
    assert copied == written
