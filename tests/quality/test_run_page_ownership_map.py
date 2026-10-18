"""The Run screen's ownership map names only files and folders that exist.

`docs/contributing/clients-architecture.md` says which module of `clients/features/run-page`
owns what. A path in that section that was renamed or deleted sends a reader to nothing, so each
backticked path in it must resolve under the package's `src/`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/contributing/clients-architecture.md"
SRC = ROOT / "clients/features/run-page/src"
HEADING = "## Where a Run screen change goes"
PATH = re.compile(r"`((?:state|renderer)/[A-Za-z0-9_./-]*)`")


def _section() -> str:
    text = DOC.read_text()
    start = text.index(HEADING)
    end = text.find("\n## ", start + len(HEADING))
    return text[start:end]


def test_every_path_the_ownership_map_names_exists() -> None:
    paths = PATH.findall(_section())

    assert paths, "the ownership map names no paths"
    assert [path for path in paths if not (SRC / path).exists()] == []


def test_every_view_folder_is_in_the_map() -> None:
    named = set(PATH.findall(_section()))

    views = {
        f"renderer/ui/views/{p.name}/" for p in (SRC / "renderer/ui/views").iterdir() if p.is_dir()
    }

    assert views <= named
