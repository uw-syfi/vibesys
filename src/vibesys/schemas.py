"""Shared review verdict and loop-state contract names.

Agent reply schemas and hypothesis-plan policy live with their owning
orchestration modules. The loop-state names below retain their authoritative
definitions in the dependency-free ``vs_loop_state`` library.
"""

from enum import StrEnum

import vs_loop_state.api as _loop_state_api

CandidateDisposition = _loop_state_api.CandidateDisposition
HypothesisOutcome = _loop_state_api.HypothesisOutcome
PerfDeltaReason = _loop_state_api.PerfDeltaReason

# HypothesisOutcome, CandidateDisposition, and PerfDeltaReason live in
# vs_loop_state so that server code can import them without deep-importing
# vibesys internals. Re-exported here so existing call sites outside this
# refactor's scope keep working unchanged.


class Verdict(StrEnum):
    """Binary outcome returned by a policy review or validation stage."""

    PASS = "pass"  # noqa: S105  # lint-waiver: LW-010203 [S105]; this is a public result enum value, not a credential.
    FAIL = "fail"
