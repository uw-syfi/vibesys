from collections.abc import Iterator
from pathlib import Path

import pytest

from vibesys.input_manifest import MANIFEST_NAME
from vibesys.render import HeadlessRenderer, output_sink


@pytest.fixture(autouse=True)
def headless_renderer() -> Iterator[HeadlessRenderer]:
    """Compose a headless renderer for every test, mirroring production.

    In production ``create_run_context`` subscribes the renderer exactly once
    per headless run; tests get the same composition so code that emits
    events through the output sink still produces observable terminal output
    (e.g. for ``capsys`` assertions).
    """
    renderer = HeadlessRenderer()
    unsubscribe = output_sink().subscribe(renderer.handle)
    try:
        yield renderer
    finally:
        unsubscribe()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parents[1]


@pytest.fixture(scope="session")
def example_input_bundles(repo_root: Path) -> tuple[Path, ...]:
    bundles = tuple(
        sorted(manifest.parent for manifest in (repo_root / "examples").glob(f"**/{MANIFEST_NAME}"))
    )
    assert bundles, f"No example input bundles found under {repo_root / 'examples'}"
    return bundles
