"""Previous-writer heads reopen without requiring their writer to run again."""

import json
from pathlib import Path

import pytest

from vs_runlog.api import read_run_head

CORPUS = Path(__file__).resolve().parents[3] / "proto/testdata/retained"
CASES = json.loads((CORPUS / "manifest.json").read_text())


@pytest.mark.regression
@pytest.mark.parametrize("version", CASES)
def test_retained_head_has_the_previous_writers_input(version: str) -> None:
    directory = CORPUS / version
    original = (directory / "run.head").read_bytes()
    head = read_run_head(directory / "run.log")
    assert head.objective is not None
    assert head.objective.input_value == CASES[version]["input"]
    assert (directory / "run.head").read_bytes() == original
