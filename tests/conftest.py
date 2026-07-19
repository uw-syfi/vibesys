from pathlib import Path

import pytest

from vibesys.input_manifest import MANIFEST_NAME


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
