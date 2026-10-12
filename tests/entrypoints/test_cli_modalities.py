"""The CLI offers exactly the modalities the registered domains declare."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from entrypoints import cli
from vibesys.domains.registry import DOMAINS
from vibesys.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

_DECLARED = sorted({m for definition in DOMAINS.values() for m in definition.modalities})
_LOOPS = ("agent", "evolve")


def _argv(tmp_path: Path, loop: str, modality: str) -> list[str]:
    return [
        "--outer-loop",
        loop,
        "--modality",
        modality,
        "--runs-dir",
        str(tmp_path / "runs"),
        "--input-objective",
        "Serve fast.",
        "--input-domain",
        "llm-serving",
        "--input-accuracy-command",
        "python checker.py",
        "--input-benchmark-command",
        "python benchmark.py",
        "--no-skills",
    ]


@pytest.mark.parametrize("loop", _LOOPS)
@pytest.mark.parametrize("modality", _DECLARED)
def test_every_declared_modality_is_accepted(tmp_path: Path, loop: str, modality: str) -> None:
    assert cli.parse_cli_invocation(_argv(tmp_path, loop, modality)).args.modality == modality


@pytest.mark.parametrize("loop", _LOOPS)
@pytest.mark.parametrize("modality", ["kv_store", "not_a_modality"])
def test_an_undeclared_modality_is_rejected_naming_the_declared_ones(
    tmp_path: Path, loop: str, modality: str
) -> None:
    with pytest.raises(ConfigurationError) as error:
        cli.parse_cli_invocation(_argv(tmp_path, loop, modality))

    for declared in _DECLARED:
        assert declared in str(error.value)
