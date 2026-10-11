"""`EvolveStrategyState`: only the search state of an evolutionary run.

Lifecycle and accounting facts (attempt phases, slot use, charges, session phases, the
evidence ledger, settlements) are read from `RunView`, never copied. What lives here is policy
memory core cannot know: the immutable population (individuals and the seeded generator state),
the population as it stood when the open generation began, the slots of that generation and
which settlement each retained individual rests on.

Every model is a strict, frozen, tuple-only `Value`, so the state round-trips through the
envelope codec (`validate_immutable_schema`) unchanged.
"""

from enum import StrEnum

from pydantic import Field

from vibesys.candidates.api import RunPhase, Winner
from vibesys.metrics import AcceptedReading
from vibesys.orchestration.evolve.population import Metrics, PopulationState
from vibesys.orchestration.evolve.strategy._prompts import PromptContext
from vs_core.api import (
    AttemptTrack,
    EvidenceKey,
    InvocationRef,
    RevisionRef,
    SchemaRef,
    SettlementId,
    StrategyState,
    TurnChain,
    Value,
)

STATE_SCHEMA = SchemaRef(name="evolve.strategy-state", version=1)

# Advisory failures the state keeps for an operator to read.
NOTE_LIMIT = 20


class Role(StrEnum):
    """Agent roles; values match the `evolve-<role>` role ids."""

    MUTATOR = "mutator"
    JUDGE = "judge"
    PROFILER = "profiler"


class Stage(StrEnum):
    """Where the search is: still looking for its first passing individual, or evolving."""

    BOOTSTRAP = "bootstrap"
    GENERATION = "generation"


class WorkPhase(StrEnum):
    """Search phase of one slot; core owns the attempt lifecycle."""

    START = "start"
    MUTATE = "mutate"
    JUDGE = "judge"
    MEASURE = "measure"
    INTERPRET = "interpret"
    PROFILE = "profile"
    EXPORT = "export"
    SETTLE = "settle"
    DONE = "done"


class UnreachableReason(StrEnum):
    """Typed reason a decision the strategy wants cannot be proposed yet."""

    RESUME_CAPABILITY_NOT_OFFERED = "resume-capability-not-offered"


class Unreachable(Value):
    """A decision kept unproposed, with the missing core contract named."""

    reason: UnreachableReason
    subject: str


class TurnRecord(TurnChain[PromptContext]):
    """One role's turn chain: free or paid, corrections and resumes."""

    role: Role


class SlotProposal(Value):
    """The selector's choice for one slot of a generation, by individual id."""

    parent_id: int = Field(gt=0)
    inspiration_ids: tuple[int, ...] = ()
    policy_parent_id: str | None = None
    target_island: int | None = None


class ProfileReading(Value):
    """What the advisory profiler reported, the part that can become fitness."""

    perf_metric: float | None = Field(default=None, allow_inf_nan=False)
    perf_unit: str | None = None
    metrics: Metrics = ()


class SettledSlot(Value):
    """What core settled for a slot's attempt."""

    settlement: SettlementId
    retained: RevisionRef | None = None
    eligible: bool = False


class SlotState(AttemptTrack[TurnRecord]):
    """Search progress of one slot (one child, one attempt, at most one individual)."""

    # Stable raw subject: `bootstrap.<k>` for the k-th cold start, `g<generation>.c<slot>`.
    key: str = Field(min_length=1)
    slot: int = Field(gt=0)
    # The search generation of the child; 0 for a cold start.
    search_generation: int = Field(ge=0)
    proposal: SlotProposal | None = None
    base: RevisionRef
    repair_seed: bool = False
    phase: WorkPhase = WorkPhase.START
    candidate: RevisionRef | None = None
    summary: str = ""
    # The feedback the individual records: the judge's, a failed gate's, or a framework note.
    feedback: str = ""
    judge_pass: bool | None = None
    judge_invocation: InvocationRef | None = None
    evidence: tuple[EvidenceKey, ...] = ()
    readings: tuple[AcceptedReading, ...] = ()
    profile: ProfileReading | None = None
    # The candidate's patch text, exported for a selector that needs code (OpenEvolve).
    patch: str | None = None
    passed: bool = False
    # The candidate was never measured (the machinery failed), so it earns no individual.
    unmeasured: bool = False
    measurements: int = Field(default=0, ge=0)
    next_serial: int = Field(default=0, ge=0)
    settled: SettledSlot | None = None
    # The slot's outcome is now part of the population (or was dropped), in slot order.
    recorded: bool = False


class Retained(Value):
    """The settlement a retained individual's revision rests on."""

    individual: int = Field(gt=0)
    settlement: SettlementId
    revision: RevisionRef


class EvolveStrategyState(StrategyState):
    """Persisted search state of the evolve strategy."""

    schema_version: int = Field(default=STATE_SCHEMA.version, ge=1)
    phase: RunPhase = RunPhase.SEARCHING
    stage: Stage = Stage.BOOTSTRAP
    stopping: bool = False
    # None before the first `decide`, which seeds it from the run's generator seed.
    population: PopulationState | None = None
    # The population as it stood when the open generation began; proposals are a pure
    # function of it. None between generations and during the cold start.
    generation_start: PopulationState | None = None
    slots: tuple[SlotState, ...] = ()
    # Cold-start attempts that have ended, with or without an individual.
    bootstrap_spent: int = Field(default=0, ge=0)
    retained: tuple[Retained, ...] = ()
    winner: Winner | None = None
    ending: str | None = None
    unreachable: tuple[Unreachable, ...] = ()
    notes: tuple[str, ...] = ()
