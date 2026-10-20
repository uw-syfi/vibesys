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
version, never derived from a later one.

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


def _positive(value: JsonValue) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


MIGRATIONS: Final[Mapping[int, Migration]] = {
    34: _v34_to_v35,
    35: _v35_to_v36,
    36: _v36_to_v37,
    37: _v37_to_v38,
    38: _v38_to_v39,
    39: _v39_to_v40,
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
