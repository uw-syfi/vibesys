"""Round-record state for the agent loop: typed model, history, rollback resolution.

``hypothesis_outcome`` and ``candidate_disposition`` are plain strings rather
than enums: this library intentionally does not depend on ``vibesys``, which
owns the actual ``HypothesisOutcome``/``CandidateDisposition`` vocabularies.
Callers validate those values against their own enums and pass in the
classification (e.g. which outcome strings count as a failure) rather than
this module hard-coding it.

``RoundRecord`` is a ``pydantic.dataclasses.dataclass`` rather than a
``BaseModel``: callers construct it positionally/by-keyword exactly like a
plain dataclass (matching how the framework's own tests build fixtures),
while still getting field validation and ``extra="forbid"`` on load. On disk,
``round_number`` is persisted under the key ``"round"``; that rename is
handled at the JSON boundary rather than via a pydantic field alias, since an
aliased first field without a plain default conflicts with dataclass
field-ordering rules (a later required field would "follow a default
argument").

``RoundHistory`` is the public entry point for reading, mutating, and
persisting a run's round history. How it's stored (JSON, atomic writes) is
deliberately not part of its API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, Field, TypeAdapter
from pydantic.dataclasses import dataclass

from vs_loop_state.core import atomic_write_json, read_json

if TYPE_CHECKING:
    from collections.abc import Container
    from pathlib import Path


@dataclass(config=ConfigDict(extra="forbid"))
class RoundRecord:
    """One completed round of the agent loop."""

    round_number: int
    commit: str | None
    perf_metric: float | None
    perf_unit: str | None
    passed: bool
    # True when the orchestrator chose to skip profiling this round; the
    # perf_metric (if any) was reused / inherited from a prior measurement
    # rather than freshly measured this round. Plateau detection ignores
    # these so a chain of skipped-profile rounds doesn't masquerade as a
    # real plateau.
    profile_skipped: bool = False
    # False means the implementer was still investigating and sparse-review
    # policy intentionally deferred both the independent judge and official
    # framework gates. Such a round is provisional, not a failed attempt.
    reviewed: bool = True
    hypothesis_id: str | None = None
    hypothesis_outcome: str | None = None
    hypothesis_parent_round: int | None = None
    # Exact tree from which this hypothesis started. This can be newer than
    # the historical end-of-round checkpoint when an operator or framework
    # repair lands between rounds.
    hypothesis_parent_commit: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    evaluation_artifact: str | None = None
    # Only framework-owned gates can set this. A judge-approved hypothesis may
    # be a useful provisional working checkpoint without becoming the latest
    # officially verified checkpoint.
    official_evaluation: bool = False
    official_evaluation_reason: str | None = None
    # Candidate evidence is deliberately separate from official tracking. It
    # may describe one directly comparable representative point rather than a
    # full canonical evaluation and therefore never updates ``perf_metric`` or
    # plateau detection by itself.
    #
    # Mirrors ``vibesys.schemas.CandidateDisposition.UNASSESSED.value``; kept
    # as a plain string default rather than importing the enum (see module
    # docstring).
    candidate_disposition: str = "unassessed"
    candidate_metrics: dict[str, float] = Field(default_factory=dict)
    candidate_evaluation_artifact: str | None = None
    candidate_operating_point: str = ""
    candidate_retention_reason: str = ""


_ROUND_RECORD_ADAPTER: TypeAdapter[RoundRecord] = TypeAdapter(RoundRecord)


def _round_record_to_json(record: RoundRecord) -> dict[str, Any]:
    dumped = _ROUND_RECORD_ADAPTER.dump_python(record, mode="json")
    round_number = dumped.pop("round_number")
    return {"round": round_number, **dumped}


def _parse_round_record(data: dict[str, Any]) -> RoundRecord:
    """Parse one raw JSON object (e.g. one entry of a persisted history) into a `RoundRecord`."""
    if "round_number" not in data and "round" in data:
        data = dict(data)
        data["round_number"] = data.pop("round")
    if "official_evaluation" not in data:
        # Runs written before sparse official evaluation executed global
        # gates for every proven candidate. Preserve that history when
        # resuming rather than relabeling old checkpoints provisional.
        data = dict(data)
        data["official_evaluation"] = (
            bool(data.get("passed", False)) and data.get("hypothesis_outcome") == "proven"
        )
    return _ROUND_RECORD_ADAPTER.validate_python(data)


class RoundHistory:
    """The round-by-round history of an agent-loop run.

    Wraps the list of round records produced by a run together with where
    (if anywhere) it's persisted. Construct with ``records=`` for a purely
    in-memory history (e.g. in tests); use `RoundHistory.load` to read one
    back from wherever it was previously saved.
    """

    def __init__(self, path: Path | None = None, records: list[RoundRecord] | None = None) -> None:
        """Wrap *records* (empty by default) together with the *path* to persist to."""
        self.path = path
        self.records: list[RoundRecord] = records if records is not None else []

    @classmethod
    def load(cls, path: Path) -> RoundHistory:
        """Load the round history from *path*, or start empty if it doesn't exist yet."""
        data = read_json(path)
        records = [] if data is None else [_parse_round_record(entry) for entry in data]
        return cls(path, records)

    def save(self) -> None:
        """Persist the current history, atomically, to the path it was loaded/created with."""
        if self.path is None:
            raise ValueError("RoundHistory has no path to save to")  # noqa: TRY003  # tracked: #288
        atomic_write_json(self.path, [_round_record_to_json(record) for record in self.records])

    def append(self, record: RoundRecord) -> None:
        """Add *record* to the end of the history."""
        self.records.append(record)

    def resolve_rollback_commit(
        self,
        target: RoundRecord,
        failed_outcomes: Container[str],
    ) -> tuple[str | None, int | None]:
        """Resolve a requested parent round to the actual failed-child base tree.

        A round record names the historical tree at the end of that round. An
        immediately following hypothesis can start from a newer tree after a
        validated operator or framework repair. If that child later fails,
        restore its recorded pre-hypothesis tree instead of erasing those
        independent repairs along with the failed implementation.

        *failed_outcomes* names which ``hypothesis_outcome`` values count as
        a failed hypothesis; the caller derives this from its own outcome
        enum.

        The second return value identifies the failed child whose base was
        chosen; ``None`` means the historical target commit is used
        unchanged.
        """
        if not self.records:
            return target.commit, None
        latest = self.records[-1]
        if (
            latest.round_number > target.round_number
            and latest.hypothesis_parent_round == target.round_number
            and latest.hypothesis_parent_commit is not None
            and latest.hypothesis_outcome is not None
            and latest.hypothesis_outcome in failed_outcomes
        ):
            return latest.hypothesis_parent_commit, latest.round_number
        return target.commit, None
