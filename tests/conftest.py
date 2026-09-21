import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from entrypoints.headless_render import HeadlessRenderer
from vibesys.evaluators.input_manifest import (
    MANIFEST_NAME,
    InputBundle,
    load_input_bundle,
    load_project_task,
)
from vibesys.render import output_sink
from vs_project import Project, ProjectNotInitializedError


@pytest.fixture(autouse=True)
def isolated_vibesys_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep machine-local project state inside each test's temporary directory."""
    monkeypatch.setenv(
        "VIBESYS_STATE_HOME",
        str(tmp_path.parent / f".vibesys-state-{tmp_path.name}"),
    )


@pytest.fixture(autouse=True)
def headless_renderer() -> Iterator[HeadlessRenderer]:
    """Compose a headless renderer for every test, mirroring production.

    In production the headless entrypoint installs this subscriber. Tests get
    the same presentation composition so direct output-sink emissions remain
    observable to ``capsys`` assertions.
    """
    renderer = HeadlessRenderer()
    unsubscribe = output_sink().subscribe(renderer.handle)
    try:
        yield renderer
    finally:
        unsubscribe()


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
