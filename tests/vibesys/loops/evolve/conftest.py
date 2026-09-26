"""Fixtures shared by the evolve strategy tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def ref_file(tmp_path: Path) -> str:
    """Reference *file* + sibling OBJECTIVE.md.

    A single-file reference avoids the model-weight resolution that a
    reference *directory* triggers, keeping the fixture independent of any
    developer-host HF cache state.
    """
    model_dir = tmp_path / "input_model"
    model_dir.mkdir()
    ref = model_dir / "ref.py"
    ref.write_text("def predict(x): return x * 2\n")
    (model_dir / "OBJECTIVE.md").write_text("Maximize tok/s throughput.\n")
    (model_dir / "vibesys.input.toml").write_text(
        """version = 1

[agent]
domain = "llm-serving"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('ok')"]
""",
        encoding="utf-8",
    )
    return str(ref)
