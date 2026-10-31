"""A schema or input change that left a wire golden stale is named from file bytes alone."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.native_goldens import report_stale
from tests.support.wire_goldens import (
    CONVERSION,
    FINGERPRINT,
    REGENERATE,
    SKELETON_WIRE,
    conversion_inputs,
    fingerprint,
    record,
    stale_wire_goldens,
)

_PROTO = "proto/vibesys/api/v1/api.proto"
_PROJECTOR = "src/vibesys/records/projector.py"
_RUNLOG = "libs/vs-runlog/src/vs_runlog/writer.py"
_GENERATOR = f"{CONVERSION}/generate.py"
_USAGE = "tests/conformance/fixtures/usage-records/codex-ten-rounds-trimmed.json"
_CONVERTED = f"{CONVERSION}/run.log.gz"


def _tree(root: Path) -> None:
    """The inputs of both sets, each with a byte, and the directories the sets live in."""
    for name in (_PROTO, _PROJECTOR, _RUNLOG, _GENERATOR, _USAGE, _CONVERTED):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"0")
    (root / SKELETON_WIRE).mkdir(parents=True, exist_ok=True)


def _edit(root: Path, name: str, payload: bytes) -> None:
    (root / name).write_bytes(payload)


def test_goldens_recorded_from_the_current_inputs_are_not_stale(tmp_path: Path) -> None:
    _tree(tmp_path)

    assert record(tmp_path) == [f"{CONVERSION}/{FINGERPRINT}", f"{SKELETON_WIRE}/{FINGERPRINT}"]

    assert stale_wire_goldens(tmp_path) == []


def test_goldens_never_recorded_are_named(tmp_path: Path) -> None:
    _tree(tmp_path)

    assert stale_wire_goldens(tmp_path) == [
        f"{CONVERSION}/{FINGERPRINT} is missing",
        f"{SKELETON_WIRE}/{FINGERPRINT} is missing",
    ]


@given(payload=st.binary(min_size=1, max_size=8))
def test_a_changed_schema_makes_both_sets_stale(payload: bytes) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _tree(root)
        record(root)
        _edit(root, _PROTO, b"0" + payload)

        stale = stale_wire_goldens(root)

    assert [problem.split("/source")[0] for problem in stale] == [CONVERSION, SKELETON_WIRE]
    assert all("which now hash to" in problem for problem in stale)


@pytest.mark.parametrize(
    ("changed", "stale_sets"),
    [
        pytest.param(_PROTO, [CONVERSION, SKELETON_WIRE], id="schema"),
        pytest.param(_PROJECTOR, [CONVERSION], id="projector"),
        pytest.param(_RUNLOG, [CONVERSION], id="run-log-package"),
        pytest.param(_GENERATOR, [CONVERSION], id="generator"),
        pytest.param(_USAGE, [CONVERSION], id="usage-capture"),
        # The converted run is the Skeleton reply's input and, being an output, nobody else's.
        pytest.param(_CONVERTED, [SKELETON_WIRE], id="converted-run"),
    ],
)
def test_each_input_makes_only_the_sets_that_derive_from_it_stale(
    tmp_path: Path, changed: str, stale_sets: list[str]
) -> None:
    _tree(tmp_path)
    record(tmp_path)
    _edit(tmp_path, changed, b"changed")

    assert [problem.split("/source")[0] for problem in stale_wire_goldens(tmp_path)] == stale_sets


def test_the_recorded_hash_of_the_conversion_set_is_not_an_input_of_the_skeleton_set(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    record(tmp_path)
    (tmp_path / CONVERSION / FINGERPRINT).write_text("rewritten\n")

    assert stale_wire_goldens(tmp_path) == [
        f"{CONVERSION}/{FINGERPRINT} records golden inputs rewritten, "
        f"which now hash to {_hash_of_conversion(tmp_path)}"
    ]


def _hash_of_conversion(root: Path) -> str:
    return fingerprint(conversion_inputs(root), root)[:12]


def test_the_report_names_the_regeneration_command(tmp_path: Path) -> None:
    _tree(tmp_path)

    assert report_stale(stale_wire_goldens(tmp_path)).endswith(
        f"Regenerate and commit them: {REGENERATE}"
    )
