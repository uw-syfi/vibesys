import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vibesys.inputs import (
    MANIFEST_NAME,
    InputBundle,
    load_input_bundle,
    load_project_task,
)
from vs_project.api import Project, ProjectNotInitializedError


@pytest.fixture(autouse=True)
def isolated_vibesys_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep machine-local project state inside each test's temporary directory."""
    monkeypatch.setenv(
        "VIBESYS_STATE_HOME",
        str(tmp_path.parent / f".vibesys-state-{tmp_path.name}"),
    )


@pytest.fixture
def loose_git_objects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every Git object loose while a test runs.

    A test that deletes one commit object from ``.git/objects`` to corrupt a
    rollback target needs that object to be a loose file. Git's automatic
    maintenance (run after ``git commit``) packs loose objects once a
    repository crosses a version-dependent object count, and then the file is
    gone, or only a copy of it is. Disabling automatic maintenance makes the
    object layout independent of the Git version and of the object count.
    """
    config = tmp_path.parent / f".gitconfig-{tmp_path.name}"
    config.write_text("[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A directory shallow enough to hold a bindable Unix socket.

    ``tmp_path`` is not usable for this. It nests a per-user, per-session and
    per-test directory under the platform temp root, and on macOS that root is
    already a ~50-byte ``/var/folders/...`` path, so the result overruns
    ``sockaddr_un.sun_path`` (104 bytes there, 108 on Linux) before a socket
    name is even appended and ``bind`` fails with "AF_UNIX path too long".

    Rooting the directory at ``/tmp`` keeps every path it yields far under the
    limit on both platforms.
    """
    directory = Path(tempfile.mkdtemp(prefix="vibesys-sock-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parents[1]


@pytest.fixture(scope="session")
def example_input_bundles(repo_root: Path) -> tuple[InputBundle, ...]:
    manifests = sorted((repo_root / "examples").glob(f"**/{MANIFEST_NAME}"))
    bundles: list[InputBundle] = []
    for manifest in manifests:
        try:
            project = Project.discover(manifest)
        except ProjectNotInitializedError:
            bundles.append(load_input_bundle(manifest.parent))
            continue
        task = next(
            task for task in project.discover_tasks() if task.manifest_path == manifest.resolve()
        )
        bundles.append(load_project_task(project, task))

    assert bundles, f"No example input bundles found under {repo_root / 'examples'}"
    return tuple(bundles)
