"""Public surface of what the serial hypothesis searches (single, multi) share.

The shared designer role and its prompts, the round and run records every serial search keeps,
the flows both call (attribution capture, asking for a plan, reading and writing the roadmap,
the trusted gates) and the pure judgments over a finished round. ``ROADMAP`` is the roadmap's
operations, which each serial search declares as owned.
"""

from pathlib import Path

from vibesys.serial_search import prompts as _prompts
from vibesys.serial_search._attribution import (
    OUTPUT_ARGUMENT,
    AttributionConfig,
    attributed,
    attribution_of,
    bottlenecks_of,
    capture_failure,
    focus_of,
    focus_policy,
    observe,
    record_round,
)
from vibesys.serial_search._closing import (
    FinishedRound,
    accuracy_passed,
    beats_input,
    benchmark_outcome,
    failed_gate,
    settle,
    turned_down,
)
from vibesys.serial_search._design import (
    PLAN_CORRECTIONS,
    AcceptedPlan,
    Advise,
    Designed,
    NoDesign,
    ask_plan,
    capture_attribution,
    design,
)
from vibesys.serial_search._ledger import LedgerJson
from vibesys.serial_search._measure import Gated, GateResult, NotMeasured, Unreadable, measure_gates
from vibesys.serial_search._plan_view import (
    Bottleneck,
    FrontierRow,
    LastProfile,
    MeasurementNoise,
    PassedIdea,
    PendingClaim,
    PlanEvidence,
    RoundRow,
    plan_evidence,
)
from vibesys.serial_search._plans import (
    Base,
    accept_plan,
    base_commit,
    continued_base,
    revision_of,
)
from vibesys.serial_search._prompts import (
    PlanCorrection,
    PlanPrompt,
    PlanTemplate,
    correction_of,
    plan_prompt,
)
from vibesys.serial_search._resume import Saved, open_run, rebuilt
from vibesys.serial_search._roadmap import (
    ROADMAP,
    ROADMAP_TRIES,
    RoadmapUnavailable,
    put_roadmap,
    read_roadmap,
)
from vibesys.serial_search._roles import LOST_TURN_BACKOFF, ORCHESTRATOR
from vibesys.serial_search._run import (
    Closed,
    Retained,
    RoundDone,
    RoundStart,
    SerialRun,
    SerialState,
    round_done,
)
from vibesys.serial_search._spans import round_passed
from vibesys.serial_search._work import Attempting, Room, Turn, work

PROMPTS_DIR = Path(_prompts.__file__).parent
"""Where the shared `plan.j2` lives; a search lists it as a fallback root of its core policy."""

__all__ = [
    "LOST_TURN_BACKOFF",
    "ORCHESTRATOR",
    "OUTPUT_ARGUMENT",
    "PLAN_CORRECTIONS",
    "PROMPTS_DIR",
    "ROADMAP",
    "ROADMAP_TRIES",
    "AcceptedPlan",
    "Advise",
    "Attempting",
    "AttributionConfig",
    "Base",
    "Bottleneck",
    "Closed",
    "Designed",
    "FinishedRound",
    "FrontierRow",
    "GateResult",
    "Gated",
    "LastProfile",
    "LedgerJson",
    "MeasurementNoise",
    "NoDesign",
    "NotMeasured",
    "PassedIdea",
    "PendingClaim",
    "PlanCorrection",
    "PlanEvidence",
    "PlanPrompt",
    "PlanTemplate",
    "Retained",
    "RoadmapUnavailable",
    "Room",
    "RoundDone",
    "RoundRow",
    "RoundStart",
    "Saved",
    "SerialRun",
    "SerialState",
    "Turn",
    "Unreadable",
    "accept_plan",
    "accuracy_passed",
    "ask_plan",
    "attributed",
    "attribution_of",
    "base_commit",
    "beats_input",
    "benchmark_outcome",
    "bottlenecks_of",
    "capture_attribution",
    "capture_failure",
    "continued_base",
    "correction_of",
    "design",
    "failed_gate",
    "focus_of",
    "focus_policy",
    "measure_gates",
    "observe",
    "open_run",
    "plan_evidence",
    "plan_prompt",
    "put_roadmap",
    "read_roadmap",
    "rebuilt",
    "record_round",
    "revision_of",
    "round_done",
    "round_passed",
    "settle",
    "turned_down",
    "work",
]
