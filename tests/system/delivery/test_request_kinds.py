"""C9: every request kind core knows is classified.

Core knows 24 request kinds. A run issues some from its own steps (`ISSUED`), core issues others
only to recover, cancel or resume (`RECOVERY_ONLY`), and one is core's own bookkeeping
(`CORE_ONLY`). A kind that joins core's set must be put in one class here, which is the prompt to
ask what answering it unknown, retryably or never does to a run (the step loop's answer to a
request nobody answers is `libs/vs-dst/tests/test_unanswered_requests.py`).
"""

from __future__ import annotations

from tests.system.delivery._skeleton_subject import (
    SkeletonScript,
    harness,
    launch,
)

from vs_core.testing.drive import drive
from vs_runtime.api.core import REQUEST_DISPATCH

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
