from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys_evaluator._private import core

from .k8s_world import FakeCore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCore:
    root = tmp_path / "core"
    root.mkdir()
    fake = FakeCore(root)
    monkeypatch.setattr(core, "_BUNDLED_DIR", tmp_path / "no-bin")
    monkeypatch.setenv(core.BIN_ENV, str(fake.binary))
    return fake
