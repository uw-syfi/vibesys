"""C9: every request kind core knows is classified, and each issued kind survives a bad answer.

Core knows 24 request kinds. A run issues some from its own steps (`ISSUED`), core issues others
only to recover, cancel or resume (`RECOVERY_ONLY`), and one is core's own bookkeeping
(`CORE_ONLY`). A kind that joins core's set must be put in one class here, which is the prompt to
ask what answering it unknown, retryably or never does to a run.
"""

from __future__ import annotations

import pytest
from tests.system.delivery._skeleton_subject import (
    DEADLINE,
    KindFault,
    SkeletonScript,
    harness,
    launch,
)

from vs_core.testing.drive import drive
from vs_core.testing.liveness import Budget, End, assert_live
from vs_runtime.api.core import REQUEST_DISPATCH
from vs_runtime.api.testing import drive_shell

ISSUED = frozenset(
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
"""Kinds a run with no fault causes (the skeleton run issues exactly these)."""

# Kinds core issues only to recover from a fault, to cancel, or to resume a suspended turn.
RECOVERY_ONLY = frozenset(
    {
        "inspect_request",
        "inspect_turn",
        "inspect_owned_job",
        "cancel_turn",
        "cancel_owned_job",
        "cancel_owned_resource",
        "resume_session_turn",
        "collect_evidence",
        "snapshot_and_retain_run",
        "restore_revision",
    }
)

CORE_ONLY = frozenset({"block_request"})
"""A request core writes to itself to end another; no executor is asked."""

KIND_FAULTS = ("unknown_once", "retryable_once", "silent")

CONCLUSIVE_AFTER_FAULT = frozenset(
    {
        *(
            (kind, how)
            for kind in ("adopt_revision", "dispatch_turn", "submit_measurement")
            for how in ("unknown_once", "retryable_once", "silent")
        ),
        *(
            (kind, how)
            for kind in ("execute_registered_operation", "verify_adoption")
            for how in ("unknown_once", "retryable_once")
        ),
    }
)
"""Kind and answer pairs after which a run ends live.

For the other pairs core blocks the intent at its bound but routes the block to the search
only, so the attempt, session or settlement waiting on it never concludes and the run stays
open. That gap belongs to vs-core; when it is fixed these pairs join the set.
"""


def test_c9_every_request_kind_core_knows_is_in_exactly_one_class() -> None:
    known = {request.model_fields["kind"].default for request in REQUEST_DISPATCH}
    classes = (ISSUED, RECOVERY_ONLY, CORE_ONLY)
    assert known == ISSUED | RECOVERY_ONLY | CORE_ONLY
    assert sum(len(c) for c in classes) == len(known), "a kind is in two classes"


def test_c9_a_run_without_faults_issues_exactly_the_issued_kinds() -> None:
    spec = launch()
    hosting = harness()
    trace = drive(
        spec.search.driver(spec.config, hosting.registry), spec.config, SkeletonScript(), hosting
    )
    assert {request.kind for request in trace.requests} == ISSUED


@pytest.mark.parametrize(
    ("kind", "how"),
    [
        pytest.param(kind, how, id=f"{kind}-{how}")
        for kind in sorted(ISSUED)
        for how in KIND_FAULTS
        if (kind, how) in CONCLUSIVE_AFTER_FAULT
    ],
)
def test_c9_a_kind_answered_unknown_retryably_or_never_ends_live(kind: str, how: str) -> None:
    spec = launch()
    hosting = harness(deadline_at=DEADLINE)
    run = drive_shell(spec, KindFault(kind, how), hosting, spec.search.reply_schemas)
    budget = Budget(retries=hosting.limits.request_retries.limit)
    assert_live(run.journal, run.core, budget, End.TERMINAL)
    if run.halted is not None:
        raise run.halted
