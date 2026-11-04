"""The qwen3.5-9b-mi210 input ships a measurable starting candidate.

The operator's service command runs `python -m engine.server` from the candidate root, so
an input without `engine/` can never be measured: the round-0 baseline dies at import.
The seed `engine/` is the reference engine, so the baseline is the reference's own number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_BUNDLE = Path(__file__).parents[2] / "examples" / "model-serving" / "qwen3.5-9b-mi210"
_REFERENCE = _BUNDLE / ".vibesys" / "tasks" / "default" / "reference"
_SEED = _BUNDLE / "engine"
_SOURCES = sorted(path.name for path in _REFERENCE.glob("*.py"))


def test_the_seed_engine_has_the_server_entry_point() -> None:
    assert (_SEED / "server.py").is_file()
    assert (_SEED / "__init__.py").is_file()


@pytest.mark.parametrize("name", _SOURCES)
def test_the_seed_engine_is_the_reference_source(name: str) -> None:
    assert (_SEED / name).read_bytes() == (_REFERENCE / name).read_bytes()


def test_the_seed_engine_has_no_source_the_reference_lacks() -> None:
    assert sorted(path.name for path in _SEED.glob("*.py")) == _SOURCES
