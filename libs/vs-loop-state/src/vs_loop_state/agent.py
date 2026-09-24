"""Typed round records and in-memory history for the agent loop.

``hypothesis_outcome`` and ``candidate_disposition`` are plain strings on
``RoundRecord`` rather than the ``HypothesisOutcome``/``CandidateDisposition``
enums defined below, so that a legacy record carrying a retired value still
loads. Callers validate those values against their own enums and pass in the
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

``judge_verdict`` is the single source of truth for a round's review state.
The on-disk key ``reviewed`` predates it and is kept only so legacy records
still load; ``RoundRecord.reviewed`` derives the boolean instead of storing a
second copy that can disagree with the verdict.

``perf_comparison`` records how the round's headline reading compared with
its baseline. The framework decides that once, when the round is written,
using the run's declared measurement tolerance; every later reader consumes
the stored answer instead of recomputing it from a tolerance it would have to
be handed.

``RoundHistory`` owns only in-memory collection behavior and rollback-base
resolution. The application-level project-state library owns persistence and
the on-disk layout.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field, TypeAdapter, model_validator
from pydantic.dataclasses import dataclass

if TYPE_CHECKING:
    from collections.abc import Container


# lint-waiver: LW-007011 [TC001]; Pydantic resolves this dataclass field at runtime
from vs_loop_state.metrics import MetricComparison  # noqa: TC001

#: A round's review state. ``deferred`` means no independent judge ran, so it
#: is the only value compatible with an unreviewed round.
JudgeVerdict = Literal["pass", "fail", "deferred"]

#: Who produced a round's headline ``perf_metric``. ``framework`` means a
#: framework-owned benchmark result contract measured it; ``implementer`` means
#: the agent reported it about its own work. Only the framework may write
#: ``framework``, so this is the trust boundary every consumer branches on.
PerfProvenance = Literal["framework", "implementer"]


class HypothesisOutcome(StrEnum):
    """Implementer-owned status for the active experimental hypothesis.

    ``SUPPORTED`` and ``NOMINATED`` are deliberately distinct from
    ``PROVEN``: an implementer may submit evidence for independent review,
    but only the judge can establish that the scoped hypothesis held.
    ``NOMINATED`` additionally asks the framework to run its global gates for
    the current candidate checkpoint. It does not imply that the overall
    objective or terminal target has been achieved.
    """

    CONTINUE = "continue"
    SUPPORTED = "supported"
    NOMINATED = "nominated"
    DISPROVEN = "disproven"
    IMPLEMENTATION_FAILED = "implementation_failed"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"


class CandidateDisposition(StrEnum):
    """How a measured candidate should be retained independently of its hypothesis.

    Hypothesis truth and checkpoint utility are different questions. A causal
    forecast can be disproven while its implementation still establishes a
    useful throughput/latency tradeoff. These values keep that distinction
    explicit without promoting provisional evidence to an official result.
    """

    UNASSESSED = "unassessed"
    DISCARD = "discard"
    PREREQUISITE = "prerequisite"
    PARETO_FRONTIER = "pareto_frontier"


class PerfDeltaReason(StrEnum):
    """Why a headline measurement carries no causal delta.

    Always re-derived from round evidence (``perf_provenance`` and the
    baseline fields), never stored on the round record, so it cannot drift
    from them. Absent entirely for records that predate provenance tracking:
    a legacy absolute number keeps reading as a deliberate absolute rather
    than being relabelled as unresolved.
    """

    # No trusted official measurement of the metric existed yet, so there was
    # legitimately nothing to compare against.
    NO_BASELINE_YET = "no_baseline_yet"
    # Trusted measurements existed but none was admissible as this round's
    # causal baseline: the lookup failed closed rather than inverting cause
    # and effect.
    BASELINE_UNRESOLVED = "baseline_unresolved"
    # The only headline number is the implementer's own report, which the
    # framework never orders against anything.
    NOT_FRAMEWORK_MEASURED = "not_framework_measured"


class HypothesisResolution(StrEnum):
    """Framework-owned resolution after all available evidence is known."""

    PROVEN = "proven"
    DISPROVEN = "disproven"
    INCONCLUSIVE = "inconclusive"
    IMPLEMENTATION_FAILED = "implementation_failed"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    # The review passed but no trusted framework measurement exists, so the
    # empirical claim is neither proven nor failed. Distinct from INCONCLUSIVE,
    # which reports a trusted measurement that could not decide the claim.
    UNMEASURED = "unmeasured"


@dataclass(config=ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True))
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
    # Legacy encoding of the review state, persisted under the on-disk key
    # ``reviewed``. It is authoritative only for records written before
    # ``judge_verdict`` existed; otherwise ``_normalize_review`` keeps it equal
    # to the verdict. Read it through the ``reviewed`` property, never here.
    legacy_reviewed: bool = Field(default=True, alias="reviewed")
    hypothesis_id: str | None = None
    hypothesis_declared_outcome: str | None = None
    # The round's review state, decided by its final implementer attempt.
    # ``deferred`` means sparse-review policy skipped both the independent
    # judge and the official framework gates: such a round is provisional, not
    # a failed attempt. ``None`` identifies a legacy record written before the
    # framework recorded a verdict.
    judge_verdict: JudgeVerdict | None = None
    hypothesis_outcome: str | None = None
    # Plan text carried alongside the id so a resolved hypothesis stays
    # readable after ``active_hypothesis.json`` has moved on. Only the live
    # plan is persisted there, so without this the claim and the attempted
    # change are recoverable for the active hypothesis only.
    hypothesis_claim: str | None = None
    hypothesis_task: str | None = None
    hypothesis_parent_round: int | None = None
    # Exact tree from which this hypothesis started. This can be newer than
    # the historical end-of-round checkpoint when an operator or framework
    # repair lands between rounds.
    hypothesis_parent_commit: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    evaluation_artifact: str | None = None
    # Whether the framework's own gates ran this round. Only framework-owned
    # gates can set it: a judge-approved hypothesis may be a useful provisional
    # working checkpoint without becoming the latest officially verified
    # checkpoint. It says nothing about who produced ``perf_metric`` -- the
    # gates can run in a round whose headline number is still the agent's own
    # report -- so consumers deciding whether to trust a measurement read
    # ``perf_provenance``, not this flag.
    official_evaluation: bool = False
    official_evaluation_reason: str | None = None
    # Candidate evidence is deliberately separate from official tracking. It
    # may describe one directly comparable representative point rather than a
    # full canonical evaluation and therefore never updates ``perf_metric`` or
    # plateau detection by itself.
    #
    # Mirrors ``CandidateDisposition.UNASSESSED.value``; kept as a plain
    # string default rather than the enum itself (see module docstring).
    candidate_disposition: str = "unassessed"
    candidate_metrics: dict[str, float] = Field(default_factory=dict)
    candidate_evaluation_artifact: str | None = None
    candidate_operating_point: str = ""
    candidate_retention_reason: str = ""
    # Framework-derived retention. ``None`` identifies a legacy or
    # not-yet-assessed record; presentation code must not infer it from
    # ``official_evaluation``.
    candidate_retained: bool | None = None
    perf_direction: Literal["max", "min"] | None = None
    perf_baseline_round: int | None = None
    perf_baseline_commit: str | None = None
    perf_baseline_metric: float | None = None
    perf_delta_pct: float | None = None
    # How this round's headline reading compared with ``perf_baseline_metric``
    # under the run's declared measurement tolerance, decided once when the
    # round was recorded. ``None`` identifies a record written before the
    # framework stored the comparison; readers then re-derive it from the
    # run's persisted metric space.
    perf_comparison: MetricComparison | None = None

    # Who produced ``perf_metric``. ``None`` identifies a legacy record written
    # before provenance was tracked; consumers treat those as trusted so
    # reprojection does not rewrite their historical resolutions. On every
    # record the framework writes now, ``perf_provenance`` is non-None whenever
    # ``perf_metric`` is non-None.
    perf_provenance: PerfProvenance | None = None

    # Implementer attribution: which driver/provider/model produced this round
    # attempt. The provider session ID itself is machine-local and never lives
    # on this portable record; ``vibesys.agents.session_store`` owns it, keyed
    # by the round's hypothesis ID. These three fields describe the *latest*
    # attempt at the round: a resumed run that re-attempts the same round
    # overwrites the manifest entry, so they are not an append-only audit log.
    implementer_driver: str | None = None
    implementer_provider: str | None = None
    implementer_model: str | None = None

    @model_validator(mode="after")
    def _normalize_review(self) -> RoundRecord:
        """Keep the review state expressible only through ``judge_verdict``.

        A ``pass``/``fail`` verdict on an unreviewed record is contradictory:
        it can only have come from an earlier implementer attempt of the same
        round (issue #503), because the judge that would own it never ran for
        the attempt this record describes. Normalize such a record to
        ``deferred``, which is what the framework should have written, so a
        resumed run and the server read path both project it as unreviewed
        rather than rejecting a still-active hypothesis.
        """
        if self.judge_verdict is None:
            # Legacy record: the boolean is all the review state there is.
            return self
        if self.judge_verdict != "deferred" and not self.legacy_reviewed:
            self.judge_verdict = "deferred"
        self.legacy_reviewed = self.judge_verdict != "deferred"
        return self

    @property
    def reviewed(self) -> bool:
        """Whether an independent judge ruled on this round's final attempt."""
        if self.judge_verdict is None:
            return self.legacy_reviewed
        return self.judge_verdict != "deferred"


_ROUND_RECORD_ADAPTER: TypeAdapter[RoundRecord] = TypeAdapter(RoundRecord)


def serialize_round_record(record: RoundRecord) -> dict[str, Any]:
    """Return the stable JSON-compatible representation of *record*."""
    dumped = _ROUND_RECORD_ADAPTER.dump_python(record, mode="json")
    round_number = dumped.pop("round_number")
    return {"round": round_number, **dumped}


def parse_round_record(data: dict[str, Any]) -> RoundRecord:
    """Parse one JSON-compatible round record from the portable schema."""
    if "round_number" not in data and "round" in data:
        data = dict(data)
        data["round_number"] = data.pop("round")
    return _ROUND_RECORD_ADAPTER.validate_python(data)


class RoundHistory:
    """The in-memory round-by-round history of an agent-loop run."""

    def __init__(self, records: list[RoundRecord] | None = None) -> None:
        """Wrap *records*, starting with an empty history by default."""
        self.records: list[RoundRecord] = records if records is not None else []

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
