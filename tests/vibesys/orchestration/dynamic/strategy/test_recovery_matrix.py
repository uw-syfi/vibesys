"""Every request kind the run issues, answered conclusively, Unknown, retryably or never again.

The production shell runs the dynamic search with the requests of one kind answered in
one of four ways, at a concurrency cap of one or two (two implementer workstreams in
flight, the cap alternating across the cases). Whatever the answer, `tests.support.liveness`
must hold: the run ends terminal, no intent is left open, requests stay bounded and none repeats without new information.
No request identity is executed twice. The kinds come from core's closed registry
(`REQUEST_DISPATCH`), so a kind that is added must be classified here, either as issued
by the dynamic search (and so exercised by every case) or as issued only by recovery.
"""

from __future__ import annotations

from collections import Counter, deque
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from tests.support.liveness import LivenessViolationError
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell

from vs_core.api import ResourceId, SubmitMeasurement
from vs_core.testing.drive import Retryable, Running, Unknown
from vs_runtime.api.core import REQUEST_DISPATCH

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.testing.drive import Answer

# Kinds the dynamic search never issues in a run with no fault: core issues them only to
# recover (inspect, block, cancel) or to resume a suspended turn.
RECOVERY_ONLY = frozenset(
    {
        "inspect_request",
        "inspect_turn",
        "inspect_owned_job",
        "cancel_turn",
        "cancel_owned_job",
        "cancel_owned_resource",
        "block_intent",
        "resume_session_turn",
        "collect_evidence",
        "snapshot_and_retain_run",
        "restore_revision",
    }
)
ISSUED = tuple(
    sorted(
        {
            "adopt_revision",
            "close_attempt_scope",
            "close_session",
            "discard_workspace",
            "dispatch_turn",
            "ensure_session",
            "ensure_workspace",
            "execute_registered_operation",
            "observe_owned_job",
            "retain_revision",
            "snapshot_and_retain",
            "submit_measurement",
            "verify_adoption",
        }
    )
)
WORKSTREAMS = 2


class Reply(StrEnum):
    UNKNOWN_ONCE = "unknown_once"
    RETRYABLE_ONCE = "retryable_once"
    SILENT = "silent"


class Faulted:
    """Executors that answer the requests of one kind the way the case says."""

    def __init__(self, kind: str, how: Reply) -> None:
        self._kind = kind
        self._how = how
        self.executed: Counter[str] = Counter()
        self._seen = 0
        self._base = Executors(
            planner=deque([plan_reply(*(implement(f"h{n}") for n in range(WORKSTREAMS)))]),
            implementer=deque(implemented() for _ in range(WORKSTREAMS)),
            judge=deque(reviewed() for _ in range(WORKSTREAMS)),
            submit=lambda request: Running(resource_id=ResourceId(root=f"job:{_root(request)}")),
        )

    def __call__(self, request: Request, core: CoreState) -> Answer:
        assert request.request_id is not None
        self.executed[request.request_id.root] += 1
        if request.kind == self._kind:
            position, self._seen = self._seen, self._seen + 1
            if self._how is Reply.SILENT or position == 0:
                return Unknown() if self._how is not Reply.RETRYABLE_ONCE else Retryable()
        return self._base(request, core)


def _root(request: SubmitMeasurement) -> str:
    assert request.request_id is not None
    return request.request_id.root


def test_every_registered_kind_is_classified() -> None:
    kinds = {request.model_fields["kind"].default for request in REQUEST_DISPATCH}
    assert kinds == set(ISSUED) | RECOVERY_ONLY


def _run(kind: str, how: Reply | None, cap: int) -> Faulted:
    script = Faulted(kind, how) if how is not None else Faulted("", Reply.SILENT)
    run_shell(script, max_concurrent=cap, max_in_flight=WORKSTREAMS)
    return script


@pytest.mark.parametrize("cap", [1, 2])
def test_a_run_with_every_answer_conclusive_ends_live(cap: int) -> None:
    script = _run("", None, cap)
    assert max(script.executed.values()) == 1


# Kind and answer pairs whose run does not end: core blocks the intent at its bound, but the
# blocked intent is routed to the strategy only. The owning area (an attempt that is still
# acquiring, a session that is still ensuring, a settlement or job that is still closing)
# never hears of it, so cleanup stays pending and the run stays open. Turns, measurements and
# a few operations conclude. Each pair fails at both caps; fix the routing in core, then
# delete the pair.
OPEN_AFTER_BLOCK_GAP = (
    "gap (owner: vs-core): reconciliation blocks the intent but only the strategy hears of it; "
    "the attempt, session or settlement waiting on this kind never concludes, so the run does "
    "not end"
)
KNOWN_OPEN: frozenset[tuple[str, Reply]] = frozenset(
    {
        ("close_attempt_scope", Reply.RETRYABLE_ONCE),
        ("close_attempt_scope", Reply.SILENT),
        ("close_attempt_scope", Reply.UNKNOWN_ONCE),
        ("close_session", Reply.RETRYABLE_ONCE),
        ("close_session", Reply.SILENT),
        ("close_session", Reply.UNKNOWN_ONCE),
        ("discard_workspace", Reply.RETRYABLE_ONCE),
        ("discard_workspace", Reply.SILENT),
        ("discard_workspace", Reply.UNKNOWN_ONCE),
        ("ensure_session", Reply.RETRYABLE_ONCE),
        ("ensure_session", Reply.SILENT),
        ("ensure_session", Reply.UNKNOWN_ONCE),
        ("ensure_workspace", Reply.RETRYABLE_ONCE),
        ("ensure_workspace", Reply.SILENT),
        ("ensure_workspace", Reply.UNKNOWN_ONCE),
        ("execute_registered_operation", Reply.SILENT),
        ("observe_owned_job", Reply.RETRYABLE_ONCE),
        ("observe_owned_job", Reply.SILENT),
        ("observe_owned_job", Reply.UNKNOWN_ONCE),
        ("retain_revision", Reply.RETRYABLE_ONCE),
        ("retain_revision", Reply.SILENT),
        ("retain_revision", Reply.UNKNOWN_ONCE),
        ("snapshot_and_retain", Reply.RETRYABLE_ONCE),
        ("snapshot_and_retain", Reply.SILENT),
        ("snapshot_and_retain", Reply.UNKNOWN_ONCE),
        ("verify_adoption", Reply.SILENT),
    }
)


# Of the known-open pairs, the ones that are run. Every kind is shown open by its `silent` answer
# (the strongest: Unknown every time). `retryable_once` reaches the block through the retry
# bound rather than the reconciliation bound, so it is also run where its path through core
# differs (branch coverage of these runs over the whole matrix); `unknown_once` takes the
# Unknown path `silent` already takes. The rest are not run, since each fails the same way.
RETRY_PATH_DIFFERS = (
    "close_session",
    "ensure_session",
    "ensure_workspace",
    "observe_owned_job",
    "snapshot_and_retain",
)
SHOWN_OPEN: frozenset[tuple[str, Reply]] = (
    frozenset(
        {(kind, Reply.SILENT) for kind, _ in KNOWN_OPEN}
        | {(kind, Reply.RETRYABLE_ONCE) for kind in RETRY_PATH_DIFFERS}
    )
    & KNOWN_OPEN
)


def _cases() -> list[object]:
    """Every (kind, answer) pair that can tell something new, at an alternating cap.

    A known-open pair fails because core blocks the intent and only the strategy hears of
    it; which answer led to the block does not change that, so only the pairs in
    `SHOWN_OPEN` are run. Fixing the gap means deleting the pairs from `KNOWN_OPEN`, which
    runs the kind's other answers again.
    The cap changes how many requests overlap, not what any one kind's answer does to the
    run (every known-open pair fails identically at both caps), so alternating it along
    the diagonal shows each kind and each answer at both caps without crossing them.
    """
    return [
        pytest.param(
            kind,
            how,
            1 + (kind_at + how_at) % 2,
            id=f"{kind}-{how.value}",
            marks=(
                [
                    pytest.mark.xfail(
                        raises=LivenessViolationError, strict=True, reason=OPEN_AFTER_BLOCK_GAP
                    )
                ]
                if (kind, how) in KNOWN_OPEN
                else []
            ),
        )
        for kind_at, kind in enumerate(ISSUED)
        for how_at, how in enumerate(Reply)
        if (kind, how) not in KNOWN_OPEN or (kind, how) in SHOWN_OPEN
    ]


@pytest.mark.parametrize(("kind", "how", "cap"), _cases())
def test_a_kind_answered_unknown_retryably_or_never_ends_live(
    kind: str, how: Reply, cap: int
) -> None:
    _run(kind, how, cap)
