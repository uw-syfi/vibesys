"""A run saved by an older VibeSys resumes: its envelope migrates to the current version.

``tests/envelopes`` holds one saved fake run per supported old version (``v<version>-<case>.json``),
written by the code of that version (a small run built through the kernel's own test lab and
encoded by that version's registry), so a migration is held to what a real old save looks like
and not to what a later model would write. Every one is brought to the current version by the
chain and decoded strictly. The registry's rule is checked here too: every version from the
oldest supported one to the one before the current has a registered migration and a fixture, so
raising ``ENVELOPE_SCHEMA_VERSION`` without them fails (`vs_core._envelope_migrations`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FAKE_REGISTRY

from .test_comparison_statistic import VALUES, compared

_FIXTURES = Path(__file__).parent / "envelopes"
_VERSIONS = Path(__file__).parent / "envelope_schema_versions.txt"
_SUPPORTED_SINCE = 34
"""The oldest version a run was promised to resume from; lowering support edits this line."""
_FILES = sorted(_FIXTURES.glob("v*-*.json"))
_Envelope = core.RunEnvelope[core.FlowState]


def _version(path: Path) -> int:
    return int(path.name.removeprefix("v").split("-", 1)[0])


def _upgraded(source: str) -> dict[str, object]:
    return json.loads(core.upgrade_envelope(source, ("schema_version",)))


def _comparisons(envelope: dict[str, object]) -> list[dict[str, object]]:
    core_state = envelope["core"]
    assert isinstance(core_state, dict)
    return core_state["evaluation"]["comparisons"]


@pytest.mark.parametrize("path", _FILES, ids=lambda path: path.stem)
def test_a_saved_envelope_of_each_supported_version_decodes_at_the_current_version(
    path: Path,
) -> None:
    decoded = FAKE_REGISTRY.decode_envelope(_Envelope, path.read_text(encoding="utf-8"))
    assert decoded.schema_version == core.ENVELOPE_SCHEMA_VERSION
    assert decoded.core.run.run_id == core.initial_state().run.run_id


def test_a_comparison_with_no_estimate_of_noise_reads_inconclusive_after_the_migration() -> None:
    """The 34 save without a floor held ``within_noise`` of one to three runs a side."""
    source = (_FIXTURES / "v34-no-floor.json").read_text(encoding="utf-8")
    before, after = _comparisons(json.loads(source)), _comparisons(_upgraded(source))
    assert {c["verdict"] for c in before} == {"within_noise"}
    assert {c["verdict"] for c in after} == {"inconclusive"}
    assert [{**c, "verdict": "x"} for c in before] == [{**c, "verdict": "x"} for c in after]


def test_a_measured_within_noise_is_kept_and_nothing_else_of_the_envelope_changes() -> None:
    source = (_FIXTURES / "v34-floor.json").read_text(encoding="utf-8")
    stored = json.loads(source)
    assert {c["verdict"] for c in _comparisons(stored)} == {"within_noise"}
    assert _upgraded(source) == {**stored, "schema_version": core.ENVELOPE_SCHEMA_VERSION}


def test_an_envelope_of_the_current_version_is_returned_as_it_is() -> None:
    source = FAKE_REGISTRY.encode_envelope(
        FAKE_REGISTRY.decode_envelope(
            _Envelope, (_FIXTURES / "v34-floor.json").read_text(encoding="utf-8")
        )
    )
    assert core.upgrade_envelope(source, ("schema_version",)) == source


def test_an_envelope_nested_in_a_record_is_migrated_in_place() -> None:
    source = (_FIXTURES / "v34-no-floor.json").read_text(encoding="utf-8")
    record = json.dumps({"schema_version": 1, "envelope": json.loads(source), "wakes": []})
    migrated = json.loads(core.upgrade_envelope(record, ("envelope", "schema_version")))
    assert migrated["wakes"] == []
    assert migrated["envelope"] == _upgraded(source)


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    floor=st.sampled_from((0.0, 0.01, 0.05)),
    direction=st.sampled_from(("max", "min")),
)
def test_the_migration_gives_the_verdict_a_current_run_stores_for_the_same_samples(
    mine: list[float], theirs: list[float], floor: float, direction: Literal["max", "min"]
) -> None:
    """Version 34 called every comparison without an estimate ``within_noise``, and nothing else differs."""
    current = compared(mine, theirs, floor=floor, direction=direction)
    old_verdict = (
        "within_noise"
        if current.verdict is core.ComparisonVerdict.INCONCLUSIVE
        else current.verdict
    )
    stored = json.loads((_FIXTURES / "v34-no-floor.json").read_text(encoding="utf-8"))
    stored["core"]["run"]["facts"]["task"]["relative_noise"] = floor
    stored["core"]["evaluation"]["comparisons"] = [
        {**current.model_dump(mode="json"), "verdict": str(old_verdict)}
    ]
    migrated = _upgraded(json.dumps(stored))
    assert _comparisons(migrated) == [current.model_dump(mode="json")]


def test_every_version_from_the_oldest_supported_has_a_migration_and_a_fixture() -> None:
    """Raising ``ENVELOPE_SCHEMA_VERSION`` fails here until the old version has both."""
    assert core.OLDEST_SUPPORTED_ENVELOPE_VERSION <= _SUPPORTED_SINCE
    chain = range(core.OLDEST_SUPPORTED_ENVELOPE_VERSION, core.ENVELOPE_SCHEMA_VERSION)
    missing = [v for v in chain if v not in core.ENVELOPE_MIGRATIONS]
    assert not missing, f"register the migration of envelope version {missing} to the next"
    assert set(core.ENVELOPE_MIGRATIONS) == set(chain), "a migration for a version not in the chain"
    uncovered = [v for v in chain if not any(_version(p) == v for p in _FILES)]
    assert not uncovered, (
        f"save a fixture envelope of version {uncovered} with the code of that version "
        f"(tests/envelopes/v<version>-<case>.json)"
    )


def test_every_supported_version_has_its_shape_pinned() -> None:
    pinned = {
        int(line.split()[0])
        for line in _VERSIONS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    assert (
        set(range(core.OLDEST_SUPPORTED_ENVELOPE_VERSION, core.ENVELOPE_SCHEMA_VERSION + 1))
        <= pinned
    )
