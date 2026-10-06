"""Whose fault a failed measurement stage is, decided by one table.

A stage that ends without a pass is the candidate's fault (``WORKLOAD``), the
machinery's (``INFRASTRUCTURE``), or cannot be told (``AMBIGUOUS``). The first
is final, the second is measured again up to the submission bound, and the third
is measured once more and then counts against the candidate (see
``vs_core.api.may_resubmit``). Every executor reads its evidence into the same two
facts, how the stage ended (``TerminalSignal``) and what the evaluator left behind
(``RecordState``), and asks ``classify``. No executor branches on the cause itself.

Decision table (rows: how the stage ended, columns: what the evaluator wrote):

=================================  ==========  ==========  ==========
signal                             absent      declared    outcome
=================================  ==========  ==========  ==========
no exit status (node loss,         infra       infra       infra
preemption, boot failure, job
time limit, collection failure)
service never ready, log shows     candidate   candidate   candidate
a candidate cause (out of memory,
KV cache too small, bad flags)
service never ready, any other     ambiguous   ambiguous   ambiguous
log (node or ROCm fault, I/O
error on shared storage, a hang)
exit 124 (stage time limit)        ambiguous   candidate   candidate
exit 137 (SIGKILL: out-of-memory   ambiguous   ambiguous   candidate
kill or timeout kill-after)
other nonzero exit                 ambiguous   candidate   candidate
exit 0                             ambiguous   candidate   candidate
=================================  ==========  ==========  ==========

"absent" means no result file between the trusted wrapper's markers, "declared"
means only the evaluator's ``hello`` record (it started and stated its schema),
"outcome" means a ``result`` or ``error`` record, even a malformed one.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_core.api import MeasurementFailure
from vs_runtime.contracts import BenchmarkFailureKind

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "RecordState",
    "TerminalSignal",
    "classify",
    "is_unsettled",
    "job_failure",
    "signal_of",
]

# GNU timeout's status when it stopped the command on its limit, and the status of a
# process that was SIGKILLed (a follow-up kill, or the kernel's out-of-memory killer).
_STAGE_TIMEOUT_EXIT = 124
_SIGKILL_EXIT = 137


class TerminalSignal(StrEnum):
    """How a stage ended, as far as its evidence shows."""

    NO_EXIT_STATUS = "no_exit_status"
    SERVICE_NOT_READY = "service_not_ready"
    SERVICE_CANDIDATE_FAULT = "service_candidate_fault"
    STAGE_TIMEOUT = "stage_timeout"
    SIGKILLED = "sigkilled"
    NONZERO_EXIT = "nonzero_exit"
    CLEAN_EXIT = "clean_exit"


class RecordState(StrEnum):
    """What the evaluator left in its result file."""

    ABSENT = "absent"
    DECLARED = "declared"
    OUTCOME = "outcome"


_CANDIDATE = BenchmarkFailureKind.WORKLOAD
_INFRA = BenchmarkFailureKind.INFRASTRUCTURE
_AMBIGUOUS = BenchmarkFailureKind.AMBIGUOUS

# Rows are signals, entries are (absent, declared, outcome), in `RecordState` order.
_TABLE: dict[
    TerminalSignal, tuple[BenchmarkFailureKind, BenchmarkFailureKind, BenchmarkFailureKind]
] = {
    TerminalSignal.NO_EXIT_STATUS: (_INFRA, _INFRA, _INFRA),
    TerminalSignal.SERVICE_NOT_READY: (_AMBIGUOUS, _AMBIGUOUS, _AMBIGUOUS),
    TerminalSignal.SERVICE_CANDIDATE_FAULT: (_CANDIDATE, _CANDIDATE, _CANDIDATE),
    TerminalSignal.STAGE_TIMEOUT: (_AMBIGUOUS, _CANDIDATE, _CANDIDATE),
    TerminalSignal.SIGKILLED: (_AMBIGUOUS, _AMBIGUOUS, _CANDIDATE),
    TerminalSignal.NONZERO_EXIT: (_AMBIGUOUS, _CANDIDATE, _CANDIDATE),
    TerminalSignal.CLEAN_EXIT: (_AMBIGUOUS, _CANDIDATE, _CANDIDATE),
}
_COLUMNS = tuple(RecordState)

# What a server prints, as data, when it dies at startup (the log tail is the only evidence).
# A signature of the candidate's own configuration makes the failure the candidate's, unless
# a signature of a fault in the node or its storage shows too: that one proves nothing, so
# the failure stays ambiguous.
_CANDIDATE_STARTUP_CAUSES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"out[ _-]?of[ _-]?memory",
        r"\bOOM\b",
        r"cannot allocate memory",
        r"no available memory for the cache blocks",
        r"larger than the maximum number of tokens that can be stored in kv cache",
        r"unrecognized arguments",
    )
)
_NODE_FAULT_SIGNATURES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"HSA_STATUS_ERROR",
        r"hipErrorNoDevice",
        r"no (?:hip|rocm|gpu|cuda)[- ]?(?:capable )?devices?",
        r"input/output error",
        r"no space left on device",
        r"stale file handle",
        r"transport endpoint is not connected",
    )
)


def classify(signal: TerminalSignal, record: RecordState) -> BenchmarkFailureKind:
    """The failure class of a stage that ended this way and left this record."""
    return _TABLE[signal][_COLUMNS.index(record)]


def _startup_signal(service_log: str) -> TerminalSignal:
    """Whether a server that never became ready says the candidate made it die."""
    if any(p.search(service_log) for p in _NODE_FAULT_SIGNATURES):
        return TerminalSignal.SERVICE_NOT_READY
    if any(p.search(service_log) for p in _CANDIDATE_STARTUP_CAUSES):
        return TerminalSignal.SERVICE_CANDIDATE_FAULT
    return TerminalSignal.SERVICE_NOT_READY


def signal_of(
    exit_code: int | None, *, service_not_ready: bool = False, service_log: str = ""
) -> TerminalSignal:
    """How a stage ended, from its exit status (None when it never reported one).

    A negative status is the sandbox's own cancellation or timeout, which proves nothing
    about the candidate, like a missing one. ``service_not_ready`` says the allocation's
    server never answered its readiness probe, so the stage never ran; ``service_log`` is
    the tail of that server's log, which says whether the candidate caused it.
    """
    if exit_code is None or exit_code < 0:
        return _startup_signal(service_log) if service_not_ready else TerminalSignal.NO_EXIT_STATUS
    if exit_code == 0:
        return TerminalSignal.CLEAN_EXIT
    if exit_code == _STAGE_TIMEOUT_EXIT:
        return TerminalSignal.STAGE_TIMEOUT
    if exit_code == _SIGKILL_EXIT:
        return TerminalSignal.SIGKILLED
    return TerminalSignal.NONZERO_EXIT


def job_failure(kinds: Iterable[BenchmarkFailureKind | None]) -> MeasurementFailure:
    """The class of a failed job from the classes of its failed stages.

    No failed stage means the executor died before measuring anything. Otherwise the
    machinery's fault outranks the rest, an unclassified stage proves nothing, an
    ambiguous one makes the job ambiguous, and only all-candidate stages blame the candidate.
    """
    seen = set(kinds)
    if not seen or _INFRA in seen:
        return MeasurementFailure.INFRASTRUCTURE
    if None in seen:
        return MeasurementFailure.UNKNOWN
    if _AMBIGUOUS in seen:
        return MeasurementFailure.AMBIGUOUS
    return MeasurementFailure.WORKLOAD


def is_unsettled(kind: BenchmarkFailureKind | None) -> bool:
    """Whether a stage of this class ends its job as failed so that it may be measured again.

    Derived from the class the job gets (`job_failure`) and core's resubmission rule, so
    no executor keeps its own list of retryable kinds.
    """
    return kind is not None and job_failure([kind]).retryable
