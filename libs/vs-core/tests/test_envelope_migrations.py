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
from typing import Any, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FAKE_REGISTRY

from .test_comparison_statistic import VALUES, compared

_FIXTURES = Path(__file__).parent / "envelopes"
_SUPPORTED_SINCE = 34
"""The oldest version a run was promised to resume from; lowering support edits this line."""
_REWRITES_SINCE = 42
"""From this version on a compatible shape change keeps the version
(``scripts/check_persisted_schema_compat.py``), so every later migration rewrites data."""
_FILES = sorted(_FIXTURES.glob("v*-*.json"))
_Envelope = core.RunEnvelope[core.FlowState]


def _version(path: Path) -> int:
    return int(path.name.removeprefix("v").split("-", 1)[0])


def _upgraded(source: str) -> dict[str, Any]:
    return json.loads(core.upgrade_envelope(source, ("schema_version",)))


def _at_44(stored: dict[str, Any]) -> dict[str, Any]:
    """``stored`` (a version 43 or older save) as version 44 holds the records state.

    Version 44 dropped ``hosts``, ``children`` and ``recovery`` and kept the epoch the barrier
    counted as ``records.epoch``; version 46 dropped the ``fence``; the rest is the one stored.
    """
    document = json.loads(json.dumps(stored))
    records = document["core"]["records"]
    epoch = records.pop("recovery")["epoch"]
    del records["hosts"], records["children"]
    records["epoch"] = epoch
    document["schema_version"] = core.ENVELOPE_SCHEMA_VERSION
    del document["fence"]
    return document


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
    assert (
        FAKE_REGISTRY.decode_envelope(_Envelope, FAKE_REGISTRY.encode_envelope(decoded)) == decoded
    )


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
    assert _upgraded(source) == _at_44(stored)


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


def test_every_migration_since_compatible_changes_keep_the_version_rewrites_its_fixture() -> None:
    """A step that only renumbers is a compatible change that should have kept the version.

    A meaning change (the same shape read differently) is no exception: its fixture holds the
    data the step rewrites.
    """
    renumbering = []
    for version, step in core.ENVELOPE_MIGRATIONS.items():
        if version < _REWRITES_SINCE:
            continue
        fixtures = [
            json.loads(p.read_text(encoding="utf-8")) for p in _FILES if _version(p) == version
        ]
        if not any({**step(dict(f)), "schema_version": version} != f for f in fixtures):
            renumbering.append(version)
    assert not renumbering, (
        f"the migrations of envelope versions {renumbering} rewrite none of their fixtures: drop "
        "the version raise if every saved envelope decodes as is, or save a fixture holding the "
        "data the step rewrites"
    )


@given(
    immediate=st.booleans(),
    now_at=st.floats(min_value=0.0, max_value=1e6),
    cleanup_bound=st.floats(min_value=0.5, max_value=3600.0),
)
def test_a_pause_now_saved_before_drain_deadlines_gets_the_cleanup_bound_and_nothing_else_changes(
    *, immediate: bool, now_at: float, cleanup_bound: float
) -> None:
    """A park saved at 42 by a Pause now has no deadline; version 43's Pause now always has one."""
    stored = json.loads((_FIXTURES / "v42-pause-now.json").read_text(encoding="utf-8"))
    run = stored["core"]["run"]
    run["park"]["immediate"] = immediate
    run["now_at"] = now_at
    run["limits"]["cleanup_bound"] = cleanup_bound
    assert "deadline_at" not in run["park"]
    expected = _at_44(stored)
    if immediate:
        expected["core"]["run"]["park"]["deadline_at"] = now_at + cleanup_bound
    assert _upgraded(json.dumps(stored)) == expected


def test_a_run_with_no_park_migrates_to_the_next_version_unchanged() -> None:
    stored = json.loads((_FIXTURES / "v42-floor.json").read_text(encoding="utf-8"))
    assert "park" not in stored["core"]["run"]
    assert _upgraded(json.dumps(stored)) == _at_44({**stored, "schema_version": 43})


def test_the_migrated_pause_now_decodes_with_its_deadline() -> None:
    source = (_FIXTURES / "v42-pause-now.json").read_text(encoding="utf-8")
    decoded = FAKE_REGISTRY.decode_envelope(_Envelope, source)
    park = decoded.core.run.park
    assert park is not None
    assert park.immediate
    assert park.deadline_at == decoded.core.run.now_at + decoded.core.run.limits.cleanup_bound


@given(epoch=st.integers(min_value=0, max_value=1000))
def test_a_save_of_version_43_keeps_its_epoch_and_drops_the_state_nothing_reads_now(
    epoch: int,
) -> None:
    """The three constant records fields go; ``recovery.epoch`` moves to ``records.epoch``."""
    stored = json.loads((_FIXTURES / "v43-resumed-pause-now.json").read_text(encoding="utf-8"))
    stored["core"]["records"]["recovery"]["epoch"] = epoch
    migrated = _upgraded(json.dumps(stored))
    assert migrated["schema_version"] == core.ENVELOPE_SCHEMA_VERSION
    records = migrated["core"]["records"]
    assert set(records) == {"records", "epoch"}
    assert records["epoch"] == epoch
    assert records["records"] == stored["core"]["records"]["records"]
    assert {**migrated, "core": {**migrated["core"], "records": None}} == {
        **{key: value for key, value in stored.items() if key != "fence"},
        "schema_version": core.ENVELOPE_SCHEMA_VERSION,
        "core": {**stored["core"], "records": None},
    }


@pytest.mark.parametrize("phase", ["ready", "required", "recovering", "blocked"])
def test_a_save_that_holds_a_host_log_a_lease_or_a_barrier_that_was_not_ready_loses_them(
    phase: str,
) -> None:
    """A save from before the step loop may hold them; nothing reads them, so they are dropped."""
    stored = json.loads((_FIXTURES / "v43-floor.json").read_text(encoding="utf-8"))
    records = stored["core"]["records"]
    records["hosts"] = [{"epoch": 3, "started_at": 1.0, "alive_at": 2.0}]
    records["recovery"] = {"checks": [], "epoch": 3, "phase": phase}
    records["children"] = [{"resource_id": {"kind": "resource", "root": "child"}}]
    migrated = _upgraded(json.dumps(stored))
    assert set(migrated["core"]["records"]) == {"records", "epoch"}
    assert migrated["core"]["records"]["epoch"] == 3


def test_the_migrated_resumed_pause_now_decodes_with_its_epoch() -> None:
    source = (_FIXTURES / "v43-resumed-pause-now.json").read_text(encoding="utf-8")
    decoded = FAKE_REGISTRY.decode_envelope(_Envelope, source)
    assert decoded.core.records.epoch == 2


def _waits(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["core"]["evaluation"]["continuations"]


def test_a_save_of_version_44_loses_the_downtime_credit_of_its_wait_and_nothing_else() -> None:
    stored = json.loads((_FIXTURES / "v44-waiting.json").read_text(encoding="utf-8"))
    (wait,) = _waits(stored)
    assert wait["downtime_credit"] == 0
    migrated = _upgraded(json.dumps(stored))
    assert migrated["schema_version"] == core.ENVELOPE_SCHEMA_VERSION
    (kept,) = _waits(migrated)
    assert kept == {key: value for key, value in wait.items() if key != "downtime_credit"}
    expected = {**stored, "schema_version": core.ENVELOPE_SCHEMA_VERSION, "core": None}
    del expected["fence"]
    assert {**migrated, "core": None} == expected


@given(
    credit=st.floats(min_value=0.0, max_value=1e4),
    deadline=st.floats(min_value=0.0, max_value=1e4),
    run_deadline=st.floats(min_value=0.0, max_value=2e4),
)
def test_a_credit_a_save_before_the_run_clock_holds_moves_the_deadline_as_the_old_limit_did(
    credit: float, deadline: float, run_deadline: float
) -> None:
    """The limit was the deadline moved by the credit, capped at the run's when within it."""
    stored = json.loads((_FIXTURES / "v44-waiting.json").read_text(encoding="utf-8"))
    stored["core"]["run"]["deadline_at"] = run_deadline
    (wait,) = _waits(stored)
    wait["deadline_at"], wait["downtime_credit"] = deadline, credit
    (kept,) = _waits(_upgraded(json.dumps(stored)))
    moved = deadline + credit
    assert kept["deadline_at"] == (min(moved, run_deadline) if deadline <= run_deadline else moved)


def test_the_migrated_waiting_save_decodes_with_its_wait() -> None:
    source = (_FIXTURES / "v44-waiting.json").read_text(encoding="utf-8")
    decoded = FAKE_REGISTRY.decode_envelope(_Envelope, source)
    (wait,) = decoded.core.evaluation.continuations
    assert (
        wait.deadline_at
        == json.loads(source)["core"]["evaluation"]["continuations"][0]["deadline_at"]
    )


def test_a_save_of_version_45_loses_its_fence_and_nothing_else() -> None:
    stored = json.loads((_FIXTURES / "v45-waiting.json").read_text(encoding="utf-8"))
    assert stored["fence"]
    migrated = _upgraded(json.dumps(stored))
    assert migrated == {
        **{key: value for key, value in stored.items() if key != "fence"},
        "schema_version": 46,
    }
    FAKE_REGISTRY.decode_envelope(_Envelope, json.dumps(stored))
