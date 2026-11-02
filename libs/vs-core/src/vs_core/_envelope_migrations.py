"""The chain that brings a stored run envelope of an older version to the current one.

A run is resumed from the envelope it stored, however long ago and whatever version of
VibeSys wrote it: updating the code never forces a fresh run. This module is the one
registry of the steps. ``MIGRATIONS[n]`` is the pure rewrite of an envelope of version ``n``
into the one a run at version ``n + 1`` would have stored, and the chain is every step in
order (`upgrade_envelope_document`). Loading strictly validates the result at the current
version (`vs_core._registry.OperationRegistry.decode_envelope`).

The rule, enforced by ``tests/test_envelope_migrations.py``: every version from the oldest
supported one up to the one before ``ENVELOPE_SCHEMA_VERSION`` has exactly one registered
migration and one fixture envelope (``tests/envelopes/v<n>-*.json``) that the chain brings to
the current version and decodes. A change to ``ENVELOPE_SCHEMA_VERSION`` without the previous
version's migration and fixture fails there. The fixtures are saved by the code of their own
version, never derived from a later one. Since version 42 a compatible shape change keeps the
version (``scripts/check_persisted_schema_compat.py``), so every step from 42 on rewrites data
its fixture holds; the number-only steps below predate that rule.

A migration is frozen at the version it targets, so it states what it needs of the document
itself and imports nothing that a later change can move (not the models, not the comparison
statistic). It invents nothing: a field that has no honest value for old data takes the value
the new code itself gives "unknown". It leaves history alone (receipts and events record what
was told and done, as it was).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import JsonValue

type JsonObject = dict[str, JsonValue]
type Migration = Callable[[JsonObject], JsonObject]

_MIN_RUNS_WITHOUT_FLOOR: Final = 3
"""Runs a side that version 35 needs for an estimate of the noise when no floor is declared."""


def _v34_to_v35(envelope: JsonObject) -> JsonObject:
    """Comparisons held with no estimate of their noise read ``inconclusive``, not ``within_noise``.

    Version 35 added the verdict ``inconclusive`` for exactly the comparisons that have no
    estimate of the noise (a task that declares no floor, and fewer than three runs a side or
    runs that show no spread). Version 34 called each of them ``within_noise``. The envelope
    holds the run's comparisons as they stand in ``core.evaluation.comparisons``, with the
    counts and noise that decide it, and the task's declared floor in
    ``core.run.facts.task.relative_noise``; a held ``within_noise`` is rewritten exactly when
    the 35 rule finds no estimate. A ``within_noise`` that was measured (a declared floor, or
    three runs a side with a spread) stays, and so do ``better`` and ``worse``. Every other
    part of the envelope is the same shape in both versions.

    The comparisons recorded in receipts are what an agent was told then, and stay as told.
    """
    core = _object(envelope.get("core"))
    evaluation = _object(core.get("evaluation"))
    comparisons = evaluation.get("comparisons")
    if isinstance(comparisons, list) and not _floor_declared(core):
        rewritten: list[JsonValue] = [
            item | {"verdict": "inconclusive"} if _is_unmeasured(item) else item
            for item in comparisons
        ]
        evaluation = evaluation | {"comparisons": rewritten}
        core = core | {"evaluation": evaluation}
    return envelope | {"core": core, "schema_version": 35}


def _v35_to_v36(envelope: JsonObject) -> JsonObject:
    """Version 36 gave ``EvidenceRef`` an optional ``unmet_requirement`` and nothing else.

    Evidence saved at 35 has none: the evaluator could not state one then, and the field's value
    for "none stated" is absent (it is not serialized when unset), which is exactly what a
    version 35 document holds. Every version 35 document is a valid version 36 one with its
    number changed.
    """
    return envelope | {"schema_version": 36}


def _v36_to_v37(envelope: JsonObject) -> JsonObject:
    """Version 37 added the events ``stop_lifted`` and ``stop_lift_recorded`` and nothing else.

    A stop could not be lifted before, so no stored envelope holds either: the shape only grew
    (the flow journal's event union and the strategy events core publishes), and every
    version 36 document is a valid version 37 one with its number changed.
    """
    return envelope | {"schema_version": 37}


def _v37_to_v38(envelope: JsonObject) -> JsonObject:
    """Version 38 added the capacity hold of a turn and the failure kind of a turn lost to it.

    A request record may carry ``capacity_hold`` (not serialized when unset), the records
    events gained ``turn_held``, and a turn result may say ``provider_unavailable``. A version
    37 run had no capacity gate reporting holds, so no stored record holds one and no turn was
    lost to its provider: the shape only grew, and every version 37 document is a valid
    version 38 one with its number changed.
    """
    return envelope | {"schema_version": 38}


def _v38_to_v39(envelope: JsonObject) -> JsonObject:
    """Version 39 added the recovery check resolution ``unknown`` and nothing else.

    Recovery settles a check whose target's result is discarded whatever it was (a cancel of
    a job) as ``unknown`` when its executor could not say. A version 38 run had no such
    resolution, so no stored check holds it: the shape only grew, and every version 38
    document is a valid version 39 one with its number changed.
    """
    return envelope | {"schema_version": 39}


def _v39_to_v40(envelope: JsonObject) -> JsonObject:
    """Version 40 gave ``MeasurementResult`` an optional ``decision_id`` and nothing else.

    A result saved at 39 answers no decision of its own: it names none, and the field's value
    for "none" is absent (it is not serialized when unset), which is exactly what a version 39
    document holds. Every version 39 document is a valid version 40 one with its number changed.
    """
    return envelope | {"schema_version": 40}


def _v40_to_v41(envelope: JsonObject) -> JsonObject:
    """Version 41 gave ``RunResultProposal`` an optional ``uncompared`` and nothing else.

    A result saved at 40 says nothing about an adoption never compared with the input, and the
    field's value for "compared" is absent (it is not serialized when unset), which is exactly
    what a version 40 document holds. Every version 40 document is a valid version 41 one with
    its number changed.
    """
    return envelope | {"schema_version": 41}


def _v41_to_v42(envelope: JsonObject) -> JsonObject:
    """Version 42 added the measurement failure ``run_environment_unavailable`` and nothing else.

    No run could end a measurement that way before, so no stored envelope holds it: the closed
    set only grew, and every version 41 document is a valid version 42 one with its number
    changed.
    """
    return envelope | {"schema_version": 42}


def _v42_to_v43(envelope: JsonObject) -> JsonObject:
    """Version 43 gave ``RunPark`` a drain deadline, and a Pause now always has one.

    A park saved at 42 by a Pause now (``immediate``) names no deadline, because that pause did
    not wait for the requests still running: it parked at once. Version 43's Pause now waits for
    them until the run's cleanup bound after the pause, so the park gets the deadline the new
    code gives a Pause now that names none: the cleanup bound from the last time the envelope
    recorded (the pause itself was recorded at or before it, and no earlier time is stored). A
    park that is not immediate waits for its turns' own limits and keeps having no deadline, as
    does a park whose run holds no cleanup bound to count from.
    """
    core = _object(envelope.get("core"))
    run = _object(core.get("run"))
    park = _object(run.get("park"))
    now_at = _number(run.get("now_at"))
    bound = _number(_object(run.get("limits")).get("cleanup_bound"))
    if (
        park.get("immediate") is True
        and park.get("deadline_at") is None
        and now_at is not None
        and bound is not None
        and bound > 0
    ):
        core = core | {"run": run | {"park": park | {"deadline_at": now_at + bound}}}
    return envelope | {"core": core, "schema_version": 43}


def _v43_to_v44(envelope: JsonObject) -> JsonObject:
    """Version 44 dropped ``hosts``, ``children`` and ``recovery`` of the records and kept the epoch.

    No run wrote those three after the step loop replaced the restart reconciliation (#3475 R5):
    ``hosts`` stayed empty, ``children`` stayed empty and ``recovery`` stayed ready with no
    check, so a save of that kind holds nothing they could name. Only ``recovery.epoch`` moved
    (it follows the log's epoch), and it becomes ``records.epoch``. A save written before the
    step loop may hold a host log, a lease or a barrier that was not ready; none of them can
    resolve now (nothing reads them), so they are dropped with the rest.
    """
    core = _object(envelope.get("core"))
    kept = records_at_44(_object(core.get("records")))
    return envelope | {"core": core | {"records": kept}, "schema_version": 44}


REMOVED_AT_44: Final = ("hosts", "children", "recovery")
"""The keys of the records state that version 44 dropped."""


def without_removed_records_keys(state: JsonValue) -> JsonValue:
    """``state``, a core state, with the records state of version 44; the very object when it holds none."""
    records = _object(state).get("records")
    if not isinstance(records, dict) or not any(key in records for key in REMOVED_AT_44):
        return state
    return _object(state) | {"records": records_at_44(records)}


def records_at_44(records: JsonObject) -> JsonObject:
    """The records state ``records`` of version 43 as version 44 holds it (see `_v43_to_v44`)."""
    epoch = _object(records.get("recovery")).get("epoch")
    kept = {name: value for name, value in records.items() if name not in REMOVED_AT_44}
    if isinstance(epoch, int) and not isinstance(epoch, bool):
        kept["epoch"] = epoch
    return kept


def _floor_declared(core: JsonObject) -> bool:
    run = _object(core.get("run"))
    task = _object(_object(run.get("facts")).get("task"))
    return _positive(task.get("relative_noise"))


def _is_unmeasured(item: JsonValue) -> bool:
    """Whether a held comparison called ``within_noise`` has no estimate of noise (no floor)."""
    if not isinstance(item, dict) or item.get("verdict") != "within_noise":
        return False
    candidate, base = item.get("candidate_samples"), item.get("base_samples")
    if not (isinstance(candidate, int) and isinstance(base, int)):
        return False
    return not (min(candidate, base) >= _MIN_RUNS_WITHOUT_FLOOR and _positive(item.get("noise")))


def _number(value: JsonValue) -> float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _positive(value: JsonValue) -> bool:
    number = _number(value)
    return number is not None and number > 0


def _object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


MIGRATIONS: Final[Mapping[int, Migration]] = {
    34: _v34_to_v35,
    35: _v35_to_v36,
    36: _v36_to_v37,
    37: _v37_to_v38,
    38: _v38_to_v39,
    39: _v39_to_v40,
    40: _v40_to_v41,
    41: _v41_to_v42,
    42: _v42_to_v43,
    43: _v43_to_v44,
}
"""Version ``n`` to ``n + 1``, one entry per version, contiguous from the oldest supported."""

OLDEST_SUPPORTED_ENVELOPE_VERSION: Final = min(MIGRATIONS)
"""The oldest envelope version a run can still be resumed from."""


def upgrade_envelope_document(envelope: JsonObject, *, to: int) -> JsonObject:
    """``envelope`` (a version in the chain) rewritten step by step into version ``to``.

    The caller has checked that the version is supported. A missing step, or a step that
    leaves another version than it targets, is a defect of this registry (its test catches
    it) and raises here rather than being skipped.
    """
    version = envelope.get("schema_version")
    while isinstance(version, int) and version < to:
        step = MIGRATIONS.get(version)
        if step is None:
            message = f"no migration registered for envelope version {version}"
            raise LookupError(message)
        envelope = step(envelope)
        version += 1
        if envelope.get("schema_version") != version:
            message = f"the migration of envelope version {version - 1} did not set {version}"
            raise LookupError(message)
    return envelope
